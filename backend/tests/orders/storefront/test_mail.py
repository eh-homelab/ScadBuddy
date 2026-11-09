"""The storefront's mail outbox (#5283, spec 2026-11-02 §5.7): a row written in the
transaction of the change that causes it, sent after the commit by the CRUD service, the
SMTP password sealed under its KEK and bound to the connection it was saved for."""

from __future__ import annotations

import base64
import smtplib
import ssl
import threading
import time
from collections.abc import Iterator
from email.message import EmailMessage
from typing import Any

import pytest

from scadbuddy.core.secrets import Kek, kek_from_base64
from scadbuddy.orders.lifecycle import ManualQuoteInput
from scadbuddy.orders.models import Order
from scadbuddy.orders.store import MAIL_QUEUED, OrderStore
from scadbuddy.orders.storefront.links import create_order_link, revoke_order_links
from scadbuddy.orders.storefront.mail import (
    MAX_ATTEMPTS,
    EmailInvalidError,
    MailConfig,
    MailConnectionUnsetError,
    StorefrontMail,
    queue_mail,
    storefront_mail_covers,
)
from scadbuddy.orders.storefront.mail_templates import render
from scadbuddy.payments.credentials import KekMissingError
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop
from tests.orders.test_revisions import attach_render

pytestmark = pytest.mark.requires_postgres

PUBLIC = "https://shop.example"
LINK = f"{PUBLIC}/o#"
HOST = MailConfig(
    host="smtp.example", port=587, user="shop", sender="shop@example.com", starttls=True
)
NO_HOST = MailConfig(host=None, port=587, user=None, sender=None, starttls=True)


def verifying(context: Any) -> None:
    """The TLS context checks the server's chain and name."""
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


class FakeSMTP:
    """`smtplib.SMTP`'s (and `SMTP_SSL`'s) surface that `StorefrontMail` uses, recording
    each call. ``fail`` is raised by the next send; ``no_starttls`` refuses STARTTLS as a
    server that does not offer it does."""

    def __init__(self, log: list[Any], fail: list[Exception], *, delay: float = 0.0) -> None:
        self.log = log
        self.fail = fail
        self.delay = delay
        self.no_starttls = False
        self.connect_error: Exception | None = None

    def __call__(self, host: str, port: int, timeout: float) -> FakeSMTP:
        if self.connect_error is not None:
            raise self.connect_error
        self.log.append(("connect", host, port, timeout))
        return self

    def implicit(self, host: str, port: int, timeout: float, context: Any) -> FakeSMTP:
        verifying(context)
        self.log.append(("connect_tls", host, port, timeout))
        return self

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.log.append(("quit",))

    def starttls(self, context: Any = None) -> None:
        if self.no_starttls:
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
        verifying(context)
        self.log.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        self.log.append(("login", user, password))

    def send_message(self, message: EmailMessage) -> None:
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail.pop(0)
        self.log.append(("send", message))


def kek() -> Kek:
    return kek_from_base64(base64.b64encode(b"k" * 32).decode())


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _no_stale_kick() -> Iterator[None]:
    """Each test starts with no kick pending, as each request does (its own context)."""
    MAIL_QUEUED.set(False)
    yield


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


@pytest.fixture
def log() -> list[Any]:
    return []


@pytest.fixture
def failures() -> list[Exception]:
    return []


@pytest.fixture
def clock() -> Clock:
    return Clock()


def outbox(
    rows: OrderStore,
    config: MailConfig,
    log: list[Any],
    failures: list[Exception],
    clock: Clock | None = None,
    *,
    fake: FakeSMTP | None = None,
    public_url: str | None = PUBLIC,
) -> StorefrontMail:
    fake = fake or FakeSMTP(log, failures)
    return StorefrontMail(
        rows,
        kek(),
        lambda: config,
        lambda: public_url,
        smtp=fake,
        smtp_ssl=fake.implicit,
        clock=clock or Clock(),
    )


def mail_rows(rows: OrderStore) -> list[dict[str, Any]]:
    with rows.transaction() as conn:
        return list(
            conn.execute("SELECT * FROM storefront_mail ORDER BY created_at, id").fetchall()
        )


def link(rows: OrderStore, order: Order, *, revoked: bool = False) -> tuple[str, str]:
    """A live order link to ada@example.com: its id and its token."""
    with rows.transaction() as conn:
        made = create_order_link(conn, kek(), order.id, "ada@example.com")
        if revoked:
            revoke_order_links(conn, order.id)
    return made


def code(rows: OrderStore, to: str = "ada@example.com", value: str = "123456") -> str:
    with rows.transaction() as conn:
        return queue_mail(conn, "sign_in_code", to, {"code": value}, None)


def sends(log: list[Any]) -> list[EmailMessage]:
    return [entry[1] for entry in log if entry[0] == "send"]


def due_now(rows: OrderStore) -> None:
    """Every row's backoff over."""
    with rows.transaction() as conn:
        conn.execute("UPDATE storefront_mail SET next_attempt_at = clock_timestamp()")


# -- the outbox ---------------------------------------------------------------------------


def test_a_queued_row_in_a_transaction_that_rolls_back_is_gone(rows: OrderStore) -> None:
    with pytest.raises(RuntimeError), rows.transaction() as conn:
        queue_mail(conn, "sign_in_code", "ada@example.com", {"code": "123456"}, None)
        raise RuntimeError("the transition failed after it queued mail")
    assert mail_rows(rows) == []
    # The rollback took the kick with it.
    assert MAIL_QUEUED.get() is False


@pytest.mark.parametrize(
    "to",
    [
        "a@example.com, b@example.org",
        "a@example.com\r\nBcc: b@example.org",
        "a@example.com\nb@example.org",
        "Ada <a@example.com>",
        "not an address",
        "",
    ],
)
def test_queue_mail_takes_exactly_one_address(rows: OrderStore, to: str) -> None:
    with pytest.raises(EmailInvalidError) as refused, rows.transaction() as conn:
        queue_mail(conn, "sign_in_code", to, {"code": "1"}, None)
    assert refused.value.slug == "email_invalid"
    assert mail_rows(rows) == []


def test_an_order_link_mail_carries_the_link_and_stores_no_token(
    rows: OrderStore, shop: Shop, log: list[Any], failures: list[Exception]
) -> None:
    order = shop.order()
    with rows.transaction() as conn:
        link_id, token = create_order_link(conn, kek(), order.id, "ada@example.com")
        queue_mail(
            conn, "order_link", "ada@example.com", {"order": "SB-1", "link_id": link_id}, order.id
        )
        sealed = conn.execute(
            "SELECT token_sha256, token_sealed FROM storefront_order_links WHERE id = %s",
            (link_id,),
        ).fetchone()
    assert sealed is not None and token.encode() not in bytes(sealed["token_sealed"])
    assert token not in str(mail_rows(rows))
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("hunter2")
    assert mail.send_queued() == 1
    [message] = sends(log)
    assert (message["To"], message["From"]) == ("ada@example.com", "shop@example.com")
    assert message["Subject"] == render("order_link", {"order": "SB-1"}, LINK + token).subject
    assert LINK + token in message.get_content()
    assert message.get_content_type() == "text/plain"
    assert ("connect", "smtp.example", 587, 20.0) in log
    assert ("starttls",) in log and ("login", "shop", "hunter2") in log
    [row] = mail_rows(rows)
    assert row["sent_at"] is not None and row["last_error"] is None
    assert mail.send_queued() == 0


def test_a_revoked_links_mail_fails_at_once(
    rows: OrderStore, shop: Shop, log: list[Any], failures: list[Exception]
) -> None:
    order = shop.order()
    link_id, _ = link(rows, order)
    with rows.transaction() as conn:
        queue_mail(
            conn, "shipped", "ada@example.com", {"order": "SB-1", "link_id": link_id}, order.id
        )
        revoke_order_links(conn, order.id)
        [gone] = conn.execute(
            "SELECT token_sealed, revoked_at FROM storefront_order_links WHERE id = %s",
            (link_id,),
        ).fetchall()
    assert gone["token_sealed"] is None and gone["revoked_at"] is not None
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    assert mail.send_queued() == 0
    [row] = mail_rows(rows)
    assert row["failed_at"] is not None and row["last_error"] == "link_revoked"
    assert sends(log) == []


def test_without_a_user_no_login_is_sent(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    code(rows)
    relay = MailConfig(
        host="smtp.example", port=25, user=None, sender="s@example.com", starttls=False
    )
    assert outbox(rows, relay, log, failures).send_queued() == 1
    assert ("starttls",) not in log and not [e for e in log if e[0] == "login"]


def test_a_login_is_never_sent_without_tls(
    rows: OrderStore, log: list[Any], failures: list[Exception], clock: Clock
) -> None:
    code(rows)
    plain = MailConfig(
        host="smtp.example", port=25, user="shop", sender="s@example.com", starttls=False
    )
    mail = outbox(rows, plain, log, failures, clock)
    mail.set_password("pw")
    assert mail.send_queued() == 0
    assert not [e for e in log if e[0] in ("connect", "login")]
    [row] = mail_rows(rows)
    assert (row["sent_at"], row["attempts"], row["failed_at"]) == (None, 0, None)
    assert "TLS" in (mail.status().server_error or "")


def test_a_server_without_starttls_gets_no_login(
    rows: OrderStore, log: list[Any], failures: list[Exception], clock: Clock
) -> None:
    code(rows)
    fake = FakeSMTP(log, failures)
    fake.no_starttls = True
    mail = outbox(rows, HOST, log, failures, clock, fake=fake)
    mail.set_password("pw")
    assert mail.send_queued() == 0
    assert not [e for e in log if e[0] == "login"]
    [row] = mail_rows(rows)
    assert (row["attempts"], row["failed_at"]) == (0, None)


def test_implicit_tls_connects_over_tls_with_a_verifying_context(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    code(rows)
    implicit = MailConfig(
        host="smtp.example",
        port=465,
        user="shop",
        sender="s@example.com",
        starttls=True,
        tls="implicit",
    )
    mail = outbox(rows, implicit, log, failures)
    mail.set_password("pw")
    assert mail.send_queued() == 1
    assert ("connect_tls", "smtp.example", 465, 20.0) in log
    assert ("starttls",) not in log and ("login", "shop", "pw") in log


def test_with_no_host_nothing_is_sent_and_nothing_raises(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    code(rows)
    assert outbox(rows, NO_HOST, log, failures).send_queued() == 0
    assert log == []
    [row] = mail_rows(rows)
    assert (row["sent_at"], row["attempts"]) == (None, 0)


def test_a_refused_recipient_counts_backs_off_and_gives_up(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    code(rows)
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    waits: list[float] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        failures.append(smtplib.SMTPRecipientsRefused({"ada@example.com": (550, b"no")}))
        assert mail.send_queued() == 0
        [row] = mail_rows(rows)
        assert row["attempts"] == attempt and row["sent_at"] is None
        assert "SMTPRecipientsRefused" in row["last_error"]
        if attempt < MAX_ATTEMPTS:
            assert row["failed_at"] is None
            assert row["lease_until"] is None
            with rows.transaction() as conn:
                wait = conn.execute(
                    "SELECT extract(epoch FROM next_attempt_at - clock_timestamp()) AS s"
                    " FROM storefront_mail"
                ).fetchone()
            assert wait is not None
            waits.append(float(wait["s"]))
            # Not due yet: nothing is tried.
            tried = len(log)
            assert mail.send_queued() == 0 and len(log) == tried
            due_now(rows)
    [row] = mail_rows(rows)
    assert row["failed_at"] is not None and "code" not in row["context"]
    assert sends(log) == []
    # 1, 2, 4 ... minutes, at most an hour.
    assert [round(w / 60) for w in waits] == [1, 2, 4, 8, 16, 32, 60]


def test_a_refused_recipient_does_not_hold_back_the_next_row(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    code(rows, "bad@example.com", "1")
    code(rows, "ada@example.com", "2")
    failures.append(smtplib.SMTPRecipientsRefused({"bad@example.com": (550, b"no")}))
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    assert mail.send_queued() == 1
    assert [m["To"] for m in sends(log)] == ["ada@example.com"]


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        smtplib.SMTPAuthenticationError(535, b"bad login"),
        smtplib.SMTPSenderRefused(553, b"no such sender", "shop@example.com"),
        ConnectionRefusedError("refused"),
    ],
)
def test_a_server_failure_never_counts_and_backs_off_the_process(
    rows: OrderStore, log: list[Any], failures: list[Exception], clock: Clock, error: Exception
) -> None:
    code(rows, "a@example.com", "1")
    code(rows, "b@example.com", "2")
    failures.append(error)
    mail = outbox(rows, HOST, log, failures, clock)
    mail.set_password("pw")
    assert mail.send_queued() == 0
    first, second = mail_rows(rows)
    assert (first["attempts"], second["attempts"]) == (0, 0)
    assert first["failed_at"] is None and first["lease_until"] is None
    assert type(error).__name__ in (mail.status().server_error or "")
    # The whole process waits a minute, then two.
    connects = len(log)
    clock.now += 59
    assert mail.send_queued() == 0 and len(log) == connects
    clock.now += 2
    failures.append(TimeoutError("again"))
    assert mail.send_queued() == 0 and len(log) > connects
    clock.now += 61
    connects = len(log)
    assert mail.send_queued() == 0 and len(log) == connects
    clock.now += 60
    assert mail.send_queued() == 2
    assert mail.status().server_error is None


def test_a_fixed_failure_fails_at_once(
    rows: OrderStore, shop: Shop, log: list[Any], failures: list[Exception]
) -> None:
    order = shop.order()
    # An order mail with no link to build: the template refuses it.
    with rows.transaction() as conn:
        queue_mail(conn, "shipped", "ada@example.com", {"order": "SB-1"}, order.id)
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    assert mail.send_queued() == 0
    [row] = mail_rows(rows)
    assert row["failed_at"] is not None and row["attempts"] == 0
    assert "ValueError" in row["last_error"]


def test_an_order_mail_waits_for_the_public_url(
    rows: OrderStore, shop: Shop, log: list[Any], failures: list[Exception]
) -> None:
    order = shop.order()
    link_id, _ = link(rows, order)
    with rows.transaction() as conn:
        queue_mail(
            conn, "order_link", "a@example.com", {"order": "SB-1", "link_id": link_id}, order.id
        )
    mail = outbox(rows, HOST, log, failures, public_url=None)
    mail.set_password("pw")
    assert mail.send_queued() == 0
    [row] = mail_rows(rows)
    assert (row["sent_at"], row["attempts"], row["failed_at"]) == (None, 0, None)


def test_two_senders_at_once_send_each_row_once(rows: OrderStore, log: list[Any]) -> None:
    for i in range(6):
        code(rows, f"c{i}@example.com", str(i))
    first = outbox(rows, HOST, log, [], fake=FakeSMTP(log, [], delay=0.05))
    second = outbox(rows, HOST, log, [], fake=FakeSMTP(log, [], delay=0.05))
    first.set_password("pw")
    counts: list[int] = []
    threads = [
        threading.Thread(target=lambda m=m: counts.append(m.send_queued())) for m in (first, second)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(counts) == 6 and all(counts)
    assert sorted(m["To"] for m in sends(log)) == [f"c{i}@example.com" for i in range(6)]


def test_a_leased_row_is_left_to_its_sender(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    code(rows)
    with rows.transaction() as conn:
        conn.execute(
            "UPDATE storefront_mail SET lease_until = clock_timestamp() + interval '2 minutes'"
        )
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    assert mail.send_queued() == 0 and log == []


def test_one_pass_at_a_time_in_a_process(rows: OrderStore, log: list[Any]) -> None:
    code(rows, "a@example.com", "1")
    code(rows, "b@example.com", "2")
    fake = FakeSMTP(log, [], delay=0.2)
    mail = outbox(rows, HOST, log, [], fake=fake)
    mail.set_password("pw")
    counts: list[int] = []
    slow = threading.Thread(target=lambda: counts.append(mail.send_queued()))
    slow.start()
    time.sleep(0.05)
    # The second pass finds the first running and does nothing.
    assert mail.send_queued() == 0
    slow.join()
    assert counts == [2]


def test_a_pass_stops_at_its_budget(rows: OrderStore, log: list[Any], clock: Clock) -> None:
    code(rows, "a@example.com", "1")
    code(rows, "b@example.com", "2")

    class Slow(FakeSMTP):
        def send_message(self, message: EmailMessage) -> None:
            clock.now += 100
            super().send_message(message)

    mail = outbox(rows, HOST, log, [], clock, fake=Slow(log, []))
    mail.set_password("pw")
    assert mail.send_queued(budget=50) == 1
    assert mail.send_queued(budget=50) == 1


# -- the sealed password ------------------------------------------------------------------


def test_the_password_is_sealed_and_reported_only_as_set(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    mail = outbox(rows, HOST, log, failures)
    assert mail.status().password_set is False
    mail.set_password("hunter2")
    mail.set_password("hunter3")
    status = mail.status()
    assert status.password_set is True and status.password_for == "shop@smtp.example:587"
    with rows.transaction() as conn:
        [row] = conn.execute("SELECT * FROM storefront_mail_secret").fetchall()
    assert b"hunter3" not in bytes(row["secret_sealed"])
    assert (row["host"], row["port"], row["username"]) == ("smtp.example", 587, "shop")
    assert mail.clear_password() is True
    assert mail.status().password_set is False
    assert mail.clear_password() is False


def test_the_password_needs_the_kek(rows: OrderStore) -> None:
    mail = StorefrontMail(rows, None, lambda: HOST, lambda: PUBLIC)
    assert mail.can_seal is False
    with pytest.raises(KekMissingError):
        mail.set_password("pw")


def test_the_password_needs_a_host_and_a_user(rows: OrderStore) -> None:
    for config in (NO_HOST, MailConfig("smtp.example", 587, None, "s@example.com", True)):
        with pytest.raises(MailConnectionUnsetError):
            outbox(rows, config, [], []).set_password("pw")


@pytest.mark.parametrize(
    "moved",
    [
        MailConfig("evil.example", 587, "shop", "shop@example.com", True),
        MailConfig("smtp.example", 2525, "shop", "shop@example.com", True),
        MailConfig("smtp.example", 587, "attacker", "shop@example.com", True),
    ],
)
def test_the_password_is_never_sent_to_another_connection(
    rows: OrderStore, log: list[Any], failures: list[Exception], moved: MailConfig
) -> None:
    code(rows)
    outbox(rows, HOST, log, failures).set_password("hunter2")
    mail = outbox(rows, moved, log, failures)
    assert mail.send_queued() == 0
    assert not [e for e in log if e[0] in ("connect", "login")]
    [row] = mail_rows(rows)
    assert (row["attempts"], row["failed_at"]) == (0, None)
    assert "another" in (mail.status().server_error or "")


def test_a_moved_binding_does_not_open(rows: OrderStore, log: list[Any]) -> None:
    """The binding is in the seal's AAD too: editing the stored host does not move it."""
    code(rows)
    mail = outbox(rows, HOST, log, [])
    mail.set_password("hunter2")
    with rows.transaction() as conn:
        conn.execute("UPDATE storefront_mail_secret SET host = 'evil.example'")
    moved = outbox(rows, MailConfig("evil.example", 587, "shop", "s@example.com", True), log, [])
    assert moved.send_queued() == 0
    assert not [e for e in log if e[0] == "login"]


def test_a_user_with_no_password_sends_nothing_and_counts_nothing(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    code(rows)
    mail = outbox(rows, HOST, log, failures)
    assert mail.send_queued() == 0 and log == []
    [row] = mail_rows(rows)
    assert row["attempts"] == 0


# -- the templates ------------------------------------------------------------------------


def test_the_template_renders_customer_text_as_plain_text() -> None:
    title = "<b>Bracket</b> & <script>x()</script>"
    rendered = render("revision_ready", {"order": "SB-1", "item": title}, PUBLIC)
    assert title in rendered.body
    assert "&lt;" not in rendered.body and "<html" not in rendered.body.lower()


def test_a_subject_never_carries_a_line_break() -> None:
    rendered = render("shipped", {"order": "SB-1\r\nBcc: x@example.com"}, PUBLIC)
    assert "\r" not in rendered.subject and "\n" not in rendered.subject


def test_a_sign_in_code_carries_the_code_and_no_link() -> None:
    rendered = render("sign_in_code", {"code": "123456"}, PUBLIC)
    assert "123456" in rendered.body and PUBLIC not in rendered.body


def test_a_customer_title_is_sent_as_plain_text(
    rows: OrderStore, shop: Shop, log: list[Any], failures: list[Exception]
) -> None:
    order = shop.order()
    link_id, token = link(rows, order)
    context = {"order": "SB-1", "item": "<i>x</i>", "link_id": link_id}
    with rows.transaction() as conn:
        queue_mail(conn, "price_ready", "a@example.com", context, order.id)
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    mail.send_queued()
    [message] = sends(log)
    assert message.get_content_type() == "text/plain" and "<i>x</i>" in message.get_content()
    # Every order mail carries the order link (ruling R1).
    assert LINK + token in message.get_content()


# -- covers and the four hooks ------------------------------------------------------------


def test_covers_needs_a_live_link_and_a_host(rows: OrderStore, shop: Shop) -> None:
    order = shop.order()
    with rows.transaction() as conn:
        assert storefront_mail_covers(conn, order.id, "shipped", host="smtp.example") is False
    link(rows, order, revoked=True)
    with rows.transaction() as conn:
        assert storefront_mail_covers(conn, order.id, "shipped", host="smtp.example") is False
    link(rows, order)
    with rows.transaction() as conn:
        assert storefront_mail_covers(conn, order.id, "shipped", host="smtp.example") is True
        assert storefront_mail_covers(conn, order.id, "shipped", host=None) is False


@pytest.fixture
def mailing(shop: Shop) -> Iterator[list[int]]:
    """The shop's lifecycle with a mail host set; each after-commit kick counted."""
    kicks: list[int] = []
    shop.lifecycle.mail_host = lambda: "smtp.example"
    shop.lifecycle.on_mail = lambda: kicks.append(1)
    yield kicks


def queued(rows: OrderStore, template: str) -> list[dict[str, Any]]:
    return [row for row in mail_rows(rows) if row["template"] == template]


def _quote(shop: Shop, order: Order) -> None:
    item = shop.item(order)
    shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=1000, label="typed"), BY
    )


def _revision(shop: Shop, order: Order) -> None:
    item = shop.item(order)
    shop.lifecycle.update_item(order.id, item.id, BY, approval="design_first")
    shop.lifecycle.ensure_revision(order.id, item.id, BY)
    rev = shop.rows.revisions(item.id)[-1]
    attach_render(shop, order, shop.get_item(item), rev)
    shop.lifecycle.send_revision(order.id, item.id, rev.id, BY)


def _custom(shop: Shop, order: Order) -> None:
    shop.lifecycle.add_custom_item(
        order.id, BY, title="Stand", quantity=1, unit_price_minor=500, accept=False
    )


def _payment(shop: Shop, order: Order) -> None:
    shop.lifecycle.open_payment_request(order.id, BY, amount_minor=500, provider=None)


def _shipment(shop: Shop, order: Order) -> None:
    item = shop.item(order)
    shop.quoted(order, item)
    shop.lifecycle.accept_order(order.id, BY)
    shop.fulfilled(order, shop.get_item(item))
    shop.shipped(order, shop.get_item(item))


HOOKS = [
    ("revision_ready", _revision),
    ("price_ready", _quote),
    ("price_ready", _custom),
    ("payment_requested", _payment),
    ("shipped", _shipment),
]


@pytest.mark.parametrize(("template", "act"), HOOKS)
def test_each_hook_queues_one_row_with_a_live_link(
    shop: Shop, mailing: list[int], template: str, act: Any
) -> None:
    order = shop.order()
    link_id, _ = link(shop.rows, order)
    act(shop, order)
    [row] = queued(shop.rows, template)
    assert (row["order_id"], row["to_address"]) == (order.id, "ada@example.com")
    assert row["context"]["link_id"] == link_id and "token" not in row["context"]
    assert row["context"]["order"] == shop.get(order).label
    assert mailing, "the commit kicks the sender"


@pytest.mark.parametrize(("template", "act"), HOOKS)
def test_each_hook_queues_nothing_without_a_live_link(
    shop: Shop, mailing: list[int], template: str, act: Any
) -> None:
    order = shop.order()
    link(shop.rows, order, revoked=True)
    act(shop, order)
    assert mail_rows(shop.rows) == []
    assert mailing == []


@pytest.mark.parametrize(("template", "act"), HOOKS)
def test_each_hook_queues_nothing_without_a_host(shop: Shop, template: str, act: Any) -> None:
    order = shop.order()
    link(shop.rows, order)
    act(shop, order)
    assert mail_rows(shop.rows) == []


def test_an_accepted_price_queues_no_price_ready(shop: Shop, mailing: list[int]) -> None:
    order = shop.order()
    link(shop.rows, order)
    shop.lifecycle.add_custom_item(
        order.id, BY, title="Stand", quantity=1, unit_price_minor=500, accept=True
    )
    assert queued(shop.rows, "price_ready") == []


def test_a_transition_that_fails_after_it_queued_leaves_no_row_and_no_kick(
    shop: Shop, mailing: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Focus 3: the shipment queued its mail, then the transition failed."""
    order = shop.order()
    link(shop.rows, order)
    item = shop.item(order)
    shop.quoted(order, item)
    shop.lifecycle.accept_order(order.id, BY)
    shop.fulfilled(order, shop.get_item(item))
    before = mail_rows(shop.rows)  # the price's mail, from the quote
    mailing.clear()

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("after the mail was queued")

    with monkeypatch.context() as patched:
        patched.setattr(shop.lifecycle, "_rederive", broken)
        with pytest.raises(RuntimeError):
            shop.shipped(order, shop.get_item(item))
    assert mail_rows(shop.rows) == before and queued(shop.rows, "shipped") == []
    # A later transition that mails nothing kicks nothing: the rollback took the flag.
    shop.lifecycle.hold(order.id, BY)
    assert mailing == []


def test_a_second_lock_in_the_transaction_keeps_the_kick(shop: Shop, mailing: list[int]) -> None:
    order = shop.order()
    with shop.rows.transaction() as conn:
        shop.lifecycle._lock(conn, order.id)
        queue_mail(conn, "sign_in_code", "ada@example.com", {"code": "1"}, None)
        shop.lifecycle._lock(conn, order.id)
    shop.lifecycle.told(shop.get(order))
    assert mailing == [1]
