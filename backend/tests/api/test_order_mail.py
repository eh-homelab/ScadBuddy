"""The shop reads an order's customer mail (#5283, plan Task 7): ``GET
/api/v1/orders/{id}/mail`` lists the order's outbox rows, newest first, with the event,
the address and the state, never the body, the context, a code, a link or a token."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_order_customer_link import back_office_order, link_url
from tests.orders.storefront.test_drafts import _state
from tests.orders.storefront.test_submit import api, rows

__all__ = ["api"]

pytestmark = pytest.mark.requires_postgres

#: The only fields a row may have. A new one fails this test: add it only when the
#: shop needs it and it holds no body, context, code, link, token or link id.
ALLOWED = {
    "event",
    "to",
    "created_at",
    "sent_at",
    "failed_at",
    "attempts",
    "last_error",
    "next_attempt_at",
}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def mail_url(order_id: str) -> str:
    return f"/api/v1/orders/{order_id}/mail"


def insert(api: TestClient, sql: str, *args: Any) -> None:
    with _state(api).projection.pool.connection() as conn:
        conn.execute(sql, args)


def test_a_new_order_has_no_mail(api: TestClient) -> None:
    order = back_office_order(api)
    shown = api.get(mail_url(order["id"]))
    assert shown.status_code == 200, shown.text
    assert shown.json() == []


def test_an_unknown_order_is_404(api: TestClient) -> None:
    assert api.get(mail_url("nope")).status_code == 404


def test_the_list_is_the_orders_rows_newest_first_with_each_state(api: TestClient) -> None:
    order = back_office_order(api)
    other = back_office_order(api)
    assert api.post(link_url(order["id"])).status_code == 200
    assert api.post(link_url(other["id"])).status_code == 200
    [queued] = rows(api, "SELECT id FROM storefront_mail WHERE order_id = %s", order["id"])
    insert(
        api,
        "UPDATE storefront_mail SET created_at = '2026-11-01T10:00:00Z' WHERE id = %s",
        queued["id"],
    )
    insert(
        api,
        "INSERT INTO storefront_mail (order_id, to_address, template, created_at, sent_at)"
        " VALUES (%s, 'ada@example.test', 'shipped', '2026-11-02T10:00:00Z',"
        " '2026-11-02T10:01:00Z')",
        order["id"],
    )
    insert(
        api,
        "INSERT INTO storefront_mail (order_id, to_address, template, created_at, failed_at,"
        " attempts, last_error) VALUES (%s, 'ada@example.test', 'price_ready',"
        " '2026-11-03T10:00:00Z', '2026-11-03T10:05:00Z', 5, 'mailbox full')",
        order["id"],
    )
    insert(
        api,
        "INSERT INTO storefront_mail (order_id, to_address, template, created_at, attempts,"
        " next_attempt_at, last_error) VALUES (%s, 'ada@example.test', 'revision_ready',"
        " '2026-11-04T10:00:00Z', 1, '2026-11-04T11:00:00Z', 'try later')",
        order["id"],
    )
    # A sign-in code has no order: it never shows.
    insert(
        api,
        "INSERT INTO storefront_mail (order_id, to_address, template, context)"
        " VALUES (NULL, 'ada@example.test', 'sign_in_code', '{\"code\": \"123456\"}')",
    )
    shown = api.get(mail_url(order["id"]))
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert [m["event"] for m in body] == ["revision_ready", "price_ready", "shipped", "order_link"]
    assert all(set(m) == ALLOWED for m in body)
    waiting, failed, sent, first = body
    assert (waiting["sent_at"], waiting["failed_at"], waiting["attempts"]) == (None, None, 1)
    assert (waiting["last_error"], at(waiting["next_attempt_at"])) == (
        "try later",
        at("2026-11-04T11:00:00Z"),
    )
    assert (at(failed["failed_at"]), failed["attempts"], failed["last_error"]) == (
        at("2026-11-03T10:05:00Z"),
        5,
        "mailbox full",
    )
    assert at(sent["sent_at"]) == at("2026-11-02T10:01:00Z")
    assert (first["to"], first["sent_at"], at(first["created_at"])) == (
        "ada@example.test",
        None,
        at("2026-11-01T10:00:00Z"),
    )
    # The other order's mail stays out.
    assert len(api.get(mail_url(other["id"])).json()) == 1


def test_the_list_never_shows_a_body_a_link_a_token_or_a_link_id(api: TestClient) -> None:
    order = back_office_order(api)
    assert api.post(link_url(order["id"])).status_code == 200
    [link] = rows(api, "SELECT id, token_sha256 FROM storefront_order_links")
    [mail] = rows(api, "SELECT id, context FROM storefront_mail")
    insert(
        api,
        'UPDATE storefront_mail SET context = context || \'{"code": "987654",'
        ' "link": "https://shop.example/o/abc"}\'::jsonb WHERE id = %s',
        mail["id"],
    )
    text = api.get(mail_url(order["id"])).text
    for secret in (link["id"], link["token_sha256"], mail["id"], "987654", "https://shop"):
        assert secret not in text
    assert "context" not in text
