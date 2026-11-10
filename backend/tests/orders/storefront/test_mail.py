"""The storefront's mail outbox (#5283, spec 2026-11-02 §5.7): a row written in the
transaction of the change that causes it, sent after the commit by the CRUD service, the
SMTP password sealed under its KEK and bound to the connection it was saved for."""

from __future__ import annotations

import base64
import smtplib
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from email.message import EmailMessage
from typing import Any

import pytest

from scadbuddy.core.secrets import Kek, kek_from_base64
from scadbuddy.orders.lifecycle import ManualQuoteInput
from scadbuddy.orders.models import Order
from scadbuddy.orders.store import MAIL_QUEUED, OrderStore
from scadbuddy.orders.storefront.accounts import CODE_TTL
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
HOST = MailConfig(host="smtp.example", port=587, user="shop", sender="shop@example.com")
NO_HOST = MailConfig(host=None, port=587, user=None, sender=None)


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
        self.starttls_error: Exception | None = None
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
        if self.starttls_error is not None:
            raise self.starttls_error
        verifying(context)
        self.log.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        # smtplib's AUTH PLAIN and LOGIN encode the user and password as ASCII.
        f"\0{user}\0{password}".encode("ascii")
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


CODE = "ABCD-EFGH-JKLM-NPQR"


def sign_in(value: str = CODE) -> dict[str, str]:
    """A sign-in code's context: the code and its link (the accounts caller builds both)."""
    return {"code": value, "link": f"{PUBLIC}/account/sign-in#code={value}"}


def code(rows: OrderStore, to: str = "ada@example.com", value: str = CODE) -> str:
    with rows.transaction() as conn:
        return queue_mail(conn, "sign_in_code", to, sign_in(value), None)


def sends(log: list[Any]) -> list[EmailMessage]:
    return [entry[1] for entry in log if entry[0] == "send"]


def due_now(rows: OrderStore) -> None:
    """Every row's backoff over."""
    with rows.transaction() as conn:
        conn.execute("UPDATE storefront_mail SET next_attempt_at = clock_timestamp()")


# -- the outbox ---------------------------------------------------------------------------


def test_a_queued_row_in_a_transaction_that_rolls_back_is_gone(rows: OrderStore) -> None:
    with pytest.raises(RuntimeError), rows.transaction() as conn:
        queue_mail(conn, "sign_in_code", "ada@example.com", sign_in(), None)
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
        queue_mail(conn, "sign_in_code", to, sign_in(), None)
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
    relay = MailConfig(host="smtp.example", port=25, user=None, sender="s@example.com", tls="none")
    assert outbox(rows, relay, log, failures).send_queued() == 1
    assert ("starttls",) not in log and not [e for e in log if e[0] == "login"]


def test_a_login_is_never_sent_without_tls(
    rows: OrderStore, log: list[Any], failures: list[Exception], clock: Clock
) -> None:
    code(rows)
    plain = MailConfig(
        host="smtp.example", port=25, user="shop", sender="s@example.com", tls="none"
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
    assert row["failed_at"] is not None and not {"code", "link"} & row["context"].keys()
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


def test_a_certificate_the_server_cannot_prove_is_a_server_failure(
    rows: OrderStore, log: list[Any], failures: list[Exception], clock: Clock
) -> None:
    """`ssl.SSLCertVerificationError` is also a `ValueError`: it never fails a row."""
    code(rows, "a@example.com", "1")
    code(rows, "b@example.com", "2")
    fake = FakeSMTP(log, failures)
    fake.starttls_error = ssl.SSLCertVerificationError(1, "certificate has expired")
    mail = outbox(rows, HOST, log, failures, clock, fake=fake)
    mail.set_password("pw")
    assert mail.send_queued() == 0
    the_server_failed(rows, mail, log, clock, "SSLCertVerificationError")


def test_a_password_that_is_not_ascii_is_a_server_failure(
    rows: OrderStore, log: list[Any], failures: list[Exception], clock: Clock
) -> None:
    """smtplib's login raises `UnicodeEncodeError` (a `ValueError`): it never fails a row."""
    code(rows, "a@example.com", "1")
    code(rows, "b@example.com", "2")
    mail = outbox(rows, HOST, log, failures, clock)
    mail.set_password("pässwort")
    assert mail.send_queued() == 0
    the_server_failed(rows, mail, log, clock, "UnicodeEncodeError")


def the_server_failed(
    rows: OrderStore, mail: StorefrontMail, log: list[Any], clock: Clock, error: str
) -> None:
    """Both rows let go and uncharged, the error kept for the shop, and the process in
    its backoff."""
    for row in mail_rows(rows):
        assert row["failed_at"] is None and row["lease_until"] is None
        assert row["attempts"] == 0 and row["last_error"] is None
        assert {"code", "link"} <= row["context"].keys()
    assert error in (mail.status().server_error or "")
    tried = len(log)
    clock.now += 59
    assert mail.send_queued() == 0 and len(log) == tried


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPDataError(550, b"5.1.1 <ada@example.com>: mailbox unavailable"),
        smtplib.SMTPDataError(552, b"5.2.2 mailbox full"),
        smtplib.SMTPDataError(553, b"mailbox name not allowed"),
        smtplib.SMTPDataError(554, b"5.6.0 message content rejected"),
        smtplib.SMTPDataError(554, b"Transaction failed"),
    ],
)
def test_a_data_refusal_of_this_mail_is_charged_to_the_row(
    rows: OrderStore, log: list[Any], failures: list[Exception], error: Exception
) -> None:
    code(rows, "a@example.com", "1")
    code(rows, "b@example.com", "2")
    failures.append(error)
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    # The first row is charged; the next is sent in the same pass.
    assert mail.send_queued() == 1
    first, second = mail_rows(rows)
    assert first["attempts"] == 1 and "SMTPDataError" in first["last_error"]
    assert second["sent_at"] is not None
    assert mail.status().server_error is None


@pytest.mark.parametrize(
    "error",
    [
        # Amazon SES's answer for an unverified sender: the sender named, no status code.
        smtplib.SMTPDataError(
            554,
            b"Message rejected: Email address is not verified. The following identities"
            b" failed the check in region US-EAST-1: shop@example.com",
        ),
        smtplib.SMTPDataError(554, b"5.7.1 Sender address rejected: not owned by user"),
        smtplib.SMTPDataError(550, b"5.1.8 Bad sender's mailbox address syntax"),
        smtplib.SMTPDataError(554, b"5.3.0 mail system misconfigured"),
        smtplib.SMTPDataError(554, b"The sender is not authorised to send from this domain"),
        smtplib.SMTPDataError(451, b"try again later"),
    ],
)
def test_a_data_refusal_of_the_sender_or_setup_is_a_server_failure(
    rows: OrderStore, log: list[Any], failures: list[Exception], clock: Clock, error: Exception
) -> None:
    code(rows, "a@example.com", "1")
    code(rows, "b@example.com", "2")
    failures.append(error)
    mail = outbox(rows, HOST, log, failures, clock)
    mail.set_password("pw")
    assert mail.send_queued() == 0
    the_server_failed(rows, mail, log, clock, "SMTPDataError")


def charged_and_passed(
    rows: OrderStore, log: list[Any], failures: list[Exception], to: str, error: Exception
) -> StorefrontMail:
    """``to``'s row refused with ``error``: charged to it, and the next row sent in the
    same pass with no server failure."""
    code(rows, to, "1")
    code(rows, "b@example.com", "2")
    failures.append(error)
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    assert mail.send_queued() == 1
    first, second = mail_rows(rows)
    assert first["attempts"] == 1 and first["failed_at"] is None
    assert second["sent_at"] is not None
    assert mail.status().server_error is None
    return mail


@pytest.mark.parametrize("to", ["relay-verify@example.com", "Verified.Relay@example.com"])
def test_a_refusal_naming_a_recipient_with_setup_words_is_charged_to_the_row(
    rows: OrderStore, log: list[Any], failures: list[Exception], to: str
) -> None:
    """The recipient is the customer's to choose: words in it never stall the sender."""
    error = smtplib.SMTPDataError(550, f"{to.lower()}: no such user".encode())
    charged_and_passed(rows, log, failures, to, error)


@pytest.mark.parametrize(
    "reply",
    [
        b"5.1.1 <workshop@example.com>: no such user",
        b"workshop@example.com: mailbox name not allowed",
        b"mailbox unavailable for xshop@example.com.invalid",
    ],
)
def test_a_recipient_that_contains_the_sender_is_charged_to_the_row(
    rows: OrderStore, log: list[Any], failures: list[Exception], reply: bytes
) -> None:
    """The sender (shop@example.com) is matched as a whole address, never inside one."""
    error = smtplib.SMTPDataError(553, reply)
    charged_and_passed(rows, log, failures, "workshop@example.com", error)


@pytest.mark.parametrize(
    "reply",
    [
        b"5.7.1 <ada@example.com>: Recipient address rejected: Access denied",
        b"5.7.1 Message rejected as spam",
    ],
)
def test_a_policy_refusal_that_does_not_name_the_sender_is_charged_to_the_row(
    rows: OrderStore, log: list[Any], failures: list[Exception], reply: bytes
) -> None:
    charged_and_passed(rows, log, failures, "ada@example.com", smtplib.SMTPDataError(554, reply))


def test_a_row_in_three_server_failures_in_a_row_is_charged_and_backed_off(
    rows: OrderStore, log: list[Any], failures: list[Exception], clock: Clock
) -> None:
    """A server failure the server answered to this row's mail counts on the row: at the
    third in a row the row is charged one attempt and backed off, so it no longer holds
    the head of the queue."""
    code(rows, "a@example.com", "1")
    code(rows, "b@example.com", "2")
    mail = outbox(rows, HOST, log, failures, clock)
    mail.set_password("pw")
    for failure in range(1, 4):
        failures.append(smtplib.SMTPDataError(554, b"5.3.0 mail system misconfigured"))
        assert mail.send_queued() == 0
        first, second = mail_rows(rows)
        assert second["sent_at"] is None and second["attempts"] == 0
        assert "SMTPDataError" in (mail.status().server_error or "")
        if failure < 3:
            assert (first["attempts"], first["server_failures"]) == (0, failure)
            assert first["failed_at"] is None and first["lease_until"] is None
        clock.now += 3600
    first, second = mail_rows(rows)
    assert (first["attempts"], first["server_failures"]) == (1, 0)
    assert first["failed_at"] is None and "SMTPDataError" in first["last_error"]
    with rows.transaction() as conn:
        wait = conn.execute(
            "SELECT extract(epoch FROM next_attempt_at - clock_timestamp()) AS s"
            " FROM storefront_mail WHERE id = %s",
            (first["id"],),
        ).fetchone()
    assert wait is not None and round(float(wait["s"]) / 60) == 1
    # The next row is sent while the first waits.
    assert mail.send_queued() == 1
    assert [m["To"] for m in sends(log)] == ["b@example.com"]


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


def test_a_pass_asked_for_meanwhile_gets_only_what_is_left_of_the_budget(
    rows: OrderStore, log: list[Any], clock: Clock
) -> None:
    for i in range(3):
        code(rows, f"c{i}@example.com", str(i))
    kicked: list[int] = []

    class Slow(FakeSMTP):
        def send_message(self, message: EmailMessage) -> None:
            clock.now += 100
            if not kicked:
                # A kick while this pass runs: it finds the pass running and asks for one
                # more after it.
                kicked.append(mail.send_queued(budget=150))
            super().send_message(message)

    mail = outbox(rows, HOST, log, [], clock, fake=Slow(log, []))
    mail.set_password("pw")
    # Two rows spend the budget; the pass asked for meanwhile does not get a fresh one.
    assert mail.send_queued(budget=150) == 2
    assert kicked == [0]
    # It stays asked for: the next call sends the last row.
    assert mail._rerun
    assert mail.send_queued(budget=150) == 1
    assert len(sends(log)) == 3


class KickAtRelease:
    """The pass lock, with a kick that lands after the holder's last look for a pass
    asked for meanwhile and before its release."""

    def __init__(self, kick: Callable[[], None]) -> None:
        self._lock = threading.Lock()
        self._kick: Callable[[], None] | None = kick

    def acquire(self, blocking: bool = True) -> bool:
        return self._lock.acquire(blocking)

    def release(self) -> None:
        kick, self._kick = self._kick, None
        if kick is not None:
            kick()
        self._lock.release()


def test_a_kick_at_the_release_is_not_lost(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    code(rows, "a@example.com", "1")
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    kicked: list[int] = []

    def kick() -> None:
        # A transition commits a mail and kicks while the pass still holds the lock.
        code(rows, "b@example.com", "2")
        kicked.append(mail.send_queued())

    mail._pass = KickAtRelease(kick)  # type: ignore[assignment]
    assert mail.send_queued() == 2
    assert kicked == [0]
    assert [m["To"] for m in sends(log)] == ["a@example.com", "b@example.com"]


# -- the sealed password ------------------------------------------------------------------


def test_the_password_is_sealed_and_reported_only_as_set(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    mail = outbox(rows, HOST, log, failures)
    assert mail.status().password_set is False
    mail.set_password("hunter2")
    mail.set_password("hunter3")
    status = mail.status()
    assert status.password_set is True and status.password_for == "shop@smtp.example:587 (starttls)"
    with rows.transaction() as conn:
        [row] = conn.execute("SELECT * FROM storefront_mail_secret").fetchall()
    assert b"hunter3" not in bytes(row["secret_sealed"])
    assert (row["host"], row["port"], row["username"], row["tls"]) == (
        "smtp.example",
        587,
        "shop",
        "starttls",
    )
    assert mail.clear_password() is True
    assert mail.status().password_set is False
    assert mail.clear_password() is False


def test_the_password_needs_the_kek(rows: OrderStore) -> None:
    mail = StorefrontMail(rows, None, lambda: HOST, lambda: PUBLIC)
    assert mail.can_seal is False
    with pytest.raises(KekMissingError):
        mail.set_password("pw")


def test_the_password_needs_a_host_and_a_user(rows: OrderStore) -> None:
    for config in (NO_HOST, MailConfig("smtp.example", 587, None, "s@example.com")):
        with pytest.raises(MailConnectionUnsetError):
            outbox(rows, config, [], []).set_password("pw")


@pytest.mark.parametrize(
    "moved",
    [
        MailConfig("evil.example", 587, "shop", "shop@example.com"),
        MailConfig("smtp.example", 2525, "shop", "shop@example.com"),
        MailConfig("smtp.example", 587, "attacker", "shop@example.com"),
        MailConfig("smtp.example", 587, "shop", "shop@example.com", tls="implicit"),
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
    moved = outbox(rows, MailConfig("evil.example", 587, "shop", "s@example.com"), log, [])
    assert moved.send_queued() == 0
    assert not [e for e in log if e[0] == "login"]


def test_a_moved_tls_mode_does_not_open(rows: OrderStore, log: list[Any]) -> None:
    """The TLS mode is in the AAD: editing the stored mode does not move the password."""
    code(rows)
    outbox(rows, HOST, log, []).set_password("hunter2")
    with rows.transaction() as conn:
        conn.execute("UPDATE storefront_mail_secret SET tls = 'implicit'")
    implicit = MailConfig("smtp.example", 587, "shop", "s@example.com", tls="implicit")
    assert outbox(rows, implicit, log, []).send_queued() == 0
    assert not [e for e in log if e[0] in ("connect", "login")]


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


def test_a_sign_in_code_carries_its_link_and_its_code() -> None:
    rendered = render("sign_in_code", sign_in(), None)
    assert f"{PUBLIC}/account/sign-in#code={CODE}" in rendered.body
    assert CODE in rendered.body.replace(f"#code={CODE}", "")
    assert f"{PUBLIC}/o#" not in rendered.body


def test_a_sign_in_code_mail_names_the_codes_real_life() -> None:
    """The code works for `accounts.CODE_TTL` (an hour), not for the outbox row's life."""
    assert timedelta(hours=1) == CODE_TTL
    assert "It works for one hour." in render("sign_in_code", sign_in(), None).body


@pytest.mark.parametrize("missing", ["code", "link"])
def test_a_sign_in_code_refuses_a_missing_field(missing: str) -> None:
    context = sign_in()
    del context[missing]
    with pytest.raises(ValueError, match=missing):
        render("sign_in_code", context, None)


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
        queue_mail(conn, "sign_in_code", "ada@example.com", sign_in(), None)
        shop.lifecycle._lock(conn, order.id)
    shop.lifecycle.told(shop.get(order))
    assert mailing == [1]


def test_a_sign_in_code_not_sent_within_ten_minutes_expires(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    late = code(rows, "late@example.com", "AAAA-AAAA-AAAA-AAAA")
    code(rows, "fresh@example.com", "BBBB-BBBB-BBBB-BBBB")
    with rows.transaction() as conn:
        conn.execute(
            "UPDATE storefront_mail SET created_at = clock_timestamp() - interval '11 minutes'"
            " WHERE id = %s",
            (late,),
        )
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    assert mail.send_queued() == 1
    assert [m["To"] for m in sends(log)] == ["fresh@example.com"]
    expired = next(row for row in mail_rows(rows) if row["id"] == late)
    assert expired["failed_at"] is not None and expired["last_error"] == "expired"
    assert "code" not in expired["context"] and "link" not in expired["context"]
    assert expired["sent_at"] is None


def test_a_late_code_expires_even_with_no_host(rows: OrderStore) -> None:
    late = code(rows)
    with rows.transaction() as conn:
        conn.execute(
            "UPDATE storefront_mail"
            " SET created_at = clock_timestamp() - interval '10 minutes 1 second'"
        )
    outbox(rows, NO_HOST, [], []).send_queued()
    [row] = mail_rows(rows)
    assert row["id"] == late and row["last_error"] == "expired"
    assert "code" not in row["context"] and "link" not in row["context"]


def test_a_code_that_expires_while_leased_is_not_sent(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    """A leased row past its ten minutes (its sender lost) is not claimed again."""
    code(rows)
    with rows.transaction() as conn:
        conn.execute(
            "UPDATE storefront_mail SET created_at = clock_timestamp() - interval '11 minutes',"
            " lease_until = clock_timestamp() - interval '1 second'"
        )
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    assert mail.send_queued() == 0 and sends(log) == []
    [row] = mail_rows(rows)
    assert row["last_error"] == "expired"
