"""The storefront's mail outbox (#5283, spec 2026-11-02 §5.7): a row written in the
transaction of the change that causes it, sent after the commit by the CRUD service, the
SMTP password sealed under its KEK."""

from __future__ import annotations

import base64
import smtplib
from collections.abc import Iterator
from email.message import EmailMessage
from typing import Any

import pytest

from scadbuddy.core.secrets import Kek, kek_from_base64
from scadbuddy.orders.lifecycle import ManualQuoteInput
from scadbuddy.orders.models import Order
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.mail import (
    MAX_ATTEMPTS,
    MailConfig,
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
HOST = MailConfig(
    host="smtp.example", port=587, user="shop", sender="shop@example.com", starttls=True
)
NO_HOST = MailConfig(host=None, port=587, user=None, sender=None, starttls=True)


class FakeSMTP:
    """`smtplib.SMTP`'s surface that `StorefrontMail` uses, recording each call."""

    def __init__(self, log: list[Any], fail: list[Exception]) -> None:
        self.log = log
        self.fail = fail

    def __call__(self, host: str, port: int, timeout: float) -> FakeSMTP:
        self.log.append(("connect", host, port, timeout))
        return self

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.log.append(("quit",))

    def starttls(self, context: Any = None) -> None:
        self.log.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        self.log.append(("login", user, password))

    def send_message(self, message: EmailMessage) -> None:
        if self.fail:
            raise self.fail.pop(0)
        self.log.append(("send", message))


def kek() -> Kek:
    return kek_from_base64(base64.b64encode(b"k" * 32).decode())


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


@pytest.fixture
def log() -> list[Any]:
    return []


@pytest.fixture
def failures() -> list[Exception]:
    return []


def outbox(
    rows: OrderStore, config: MailConfig, log: list[Any], failures: list[Exception]
) -> StorefrontMail:
    mail = StorefrontMail(rows, kek(), lambda: config, lambda: PUBLIC, smtp=FakeSMTP(log, failures))
    return mail


def mail_rows(rows: OrderStore) -> list[dict[str, Any]]:
    with rows.transaction() as conn:
        return list(
            conn.execute("SELECT * FROM storefront_mail ORDER BY created_at, id").fetchall()
        )


def link(rows: OrderStore, order: Order, *, revoked: bool = False) -> None:
    with rows.transaction() as conn:
        conn.execute(
            "INSERT INTO storefront_order_links (order_id, token_sha256, sent_to, revoked_at)"
            " VALUES (%s, md5(random()::text), 'ada@example.com',"
            " CASE WHEN %s THEN clock_timestamp() END)",
            (order.id, revoked),
        )


def sends(log: list[Any]) -> list[EmailMessage]:
    return [entry[1] for entry in log if entry[0] == "send"]


# -- the outbox ---------------------------------------------------------------------------


def test_a_queued_row_in_a_transaction_that_rolls_back_is_gone(rows: OrderStore) -> None:
    with pytest.raises(RuntimeError), rows.transaction() as conn:
        queue_mail(conn, "sign_in_code", "ada@example.com", {"code": "123456"}, None)
        raise RuntimeError("the transition failed after it queued mail")
    assert mail_rows(rows) == []


def test_send_queued_sends_the_template_and_stamps_sent_at(
    rows: OrderStore, shop: Shop, log: list[Any], failures: list[Exception]
) -> None:
    order = shop.order()
    with rows.transaction() as conn:
        queue_mail(
            conn, "order_link", "ada@example.com", {"order": "SB-1", "token": "tok"}, order.id
        )
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("hunter2")
    assert mail.send_queued() == 1
    [message] = sends(log)
    assert message["To"] == "ada@example.com"
    assert message["From"] == "shop@example.com"
    assert (
        message["Subject"]
        == render("order_link", {"order": "SB-1", "token": "tok"}, PUBLIC).subject
    )
    assert f"{PUBLIC}/o#tok" in message.get_content()
    assert message.get_content_type() == "text/plain"
    assert ("connect", "smtp.example", 587, 20.0) in log
    assert ("starttls",) in log and ("login", "shop", "hunter2") in log
    [row] = mail_rows(rows)
    assert row["sent_at"] is not None and row["attempts"] == 1 and row["last_error"] is None
    # The token was only for the send: the outbox keeps no secret once it is out.
    assert "token" not in row["context"]
    assert mail.send_queued() == 0


def test_no_starttls_and_no_user_skip_both(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    with rows.transaction() as conn:
        queue_mail(conn, "sign_in_code", "ada@example.com", {"code": "123456"}, None)
    plain = MailConfig(
        host="smtp.example", port=25, user=None, sender="s@example.com", starttls=False
    )
    assert outbox(rows, plain, log, failures).send_queued() == 1
    assert ("starttls",) not in log and not [e for e in log if e[0] == "login"]


def test_with_no_host_nothing_is_sent_and_nothing_raises(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    with rows.transaction() as conn:
        queue_mail(conn, "sign_in_code", "ada@example.com", {"code": "123456"}, None)
    assert outbox(rows, NO_HOST, log, failures).send_queued() == 0
    assert log == []
    [row] = mail_rows(rows)
    assert (row["sent_at"], row["attempts"]) == (None, 0)


def test_a_failed_send_is_kept_and_given_up_after_its_attempts(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    with rows.transaction() as conn:
        queue_mail(conn, "sign_in_code", "ada@example.com", {"code": "123456"}, None)
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        failures.append(smtplib.SMTPRecipientsRefused({"ada@example.com": (550, b"no")}))
        assert mail.send_queued() == 0
        [row] = mail_rows(rows)
        assert row["attempts"] == attempt and row["sent_at"] is None
        assert "SMTPRecipientsRefused" in row["last_error"]
    # Past the last attempt the row is left alone, and its code is dropped.
    assert mail.send_queued() == 0
    [row] = mail_rows(rows)
    assert row["attempts"] == MAX_ATTEMPTS and "code" not in row["context"]
    assert sends(log) == []


def test_a_refused_recipient_does_not_hold_back_the_next_row(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    with rows.transaction() as conn:
        queue_mail(conn, "sign_in_code", "bad@example.com", {"code": "1"}, None)
        queue_mail(conn, "sign_in_code", "ada@example.com", {"code": "2"}, None)
    failures.append(smtplib.SMTPRecipientsRefused({"bad@example.com": (550, b"no")}))
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    assert mail.send_queued() == 1
    assert [m["To"] for m in sends(log)] == ["ada@example.com"]


def test_a_server_that_does_not_answer_stops_the_batch(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    with rows.transaction() as conn:
        queue_mail(conn, "sign_in_code", "a@example.com", {"code": "1"}, None)
        queue_mail(conn, "sign_in_code", "b@example.com", {"code": "2"}, None)
    failures.append(TimeoutError("timed out"))
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    assert mail.send_queued() == 0
    first, second = mail_rows(rows)
    assert (first["attempts"], second["attempts"]) == (1, 0)


def test_an_order_link_waits_for_the_public_url(
    rows: OrderStore, shop: Shop, log: list[Any], failures: list[Exception]
) -> None:
    order = shop.order()
    with rows.transaction() as conn:
        queue_mail(conn, "order_link", "a@example.com", {"order": "SB-1", "token": "t"}, order.id)
    mail = StorefrontMail(rows, kek(), lambda: HOST, lambda: None, smtp=FakeSMTP(log, failures))
    mail.set_password("pw")
    assert mail.send_queued() == 0
    [row] = mail_rows(rows)
    assert (row["sent_at"], row["attempts"]) == (None, 0)


# -- the sealed password ------------------------------------------------------------------


def test_the_password_is_sealed_and_reported_only_as_set(
    rows: OrderStore, log: list[Any], failures: list[Exception]
) -> None:
    mail = outbox(rows, HOST, log, failures)
    assert mail.has_password() is False
    mail.set_password("hunter2")
    mail.set_password("hunter3")
    assert mail.has_password() is True
    with rows.transaction() as conn:
        [row] = conn.execute("SELECT * FROM storefront_mail_secret").fetchall()
    assert b"hunter3" not in bytes(row["secret_sealed"])
    assert mail.clear_password() is True
    assert mail.has_password() is False
    assert mail.clear_password() is False


def test_the_password_needs_the_kek(rows: OrderStore) -> None:
    mail = StorefrontMail(rows, None, lambda: HOST, lambda: PUBLIC)
    assert mail.can_seal is False
    with pytest.raises(KekMissingError):
        mail.set_password("pw")


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
    with rows.transaction() as conn:
        queue_mail(
            conn, "price_ready", "a@example.com", {"order": "SB-1", "item": "<i>x</i>"}, order.id
        )
    mail = outbox(rows, HOST, log, failures)
    mail.set_password("pw")
    mail.send_queued()
    [message] = sends(log)
    assert message.get_content_type() == "text/plain" and "<i>x</i>" in message.get_content()


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
    link(shop.rows, order)
    act(shop, order)
    [row] = queued(shop.rows, template)
    assert (row["order_id"], row["to_address"]) == (order.id, "ada@example.com")
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
