"""The shop sends or revokes an order's customer link (#5283, storefront spec §5.2 "Order
link", plan Task 4): ``POST /api/v1/orders/{id}/customer-link`` revokes the live links and
mails a new one (`issue_link`, the one path submit takes too), ``DELETE`` revokes, and
``GET`` shows the newest link's state, never its token."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.lifecycle import NewModelItem
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from tests.api.test_customers import ADA
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import problem
from tests.conftest import PgPool
from tests.orders.storefront.test_drafts import _orders
from tests.orders.storefront.test_submit import api, rows
from tests.orders.test_lifecycle import resolved

__all__ = ["api"]

pytestmark = pytest.mark.requires_postgres

PROBLEMS = "https://scadbuddy.dev/problems/orders/"


def link_url(order_id: str) -> str:
    return f"/api/v1/orders/{order_id}/customer-link"


def back_office_order(api: TestClient, **customer: Any) -> dict[str, Any]:
    return create_order(api, create_customer(api, {**ADA, **customer})["id"])


def events(api: TestClient, order_id: str, kind: str) -> list[dict[str, Any]]:
    listed = api.get(f"/api/v1/orders/{order_id}/events").json()
    return [e for e in listed if e["kind"] == kind]


def test_a_new_order_has_no_link(api: TestClient) -> None:
    order = back_office_order(api)
    shown = api.get(link_url(order["id"]))
    assert shown.status_code == 200, shown.text
    assert shown.json() == {
        "live": False,
        "created_at": None,
        "sent_to": None,
        "opened_at": None,
        "revoked_at": None,
    }


def test_send_mails_the_customers_email_and_never_answers_the_token(api: TestClient) -> None:
    order = back_office_order(api)
    sent = api.post(link_url(order["id"]))
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert (body["live"], body["sent_to"], body["revoked_at"]) == (True, "ada@example.test", None)
    assert body["created_at"] is not None
    assert "token" not in sent.text
    [link] = rows(api, "SELECT id, sent_to, revoked_at FROM storefront_order_links")
    [mail] = rows(api, "SELECT template, to_address, context, order_id FROM storefront_mail")
    assert mail == {
        "template": "order_link",
        "to_address": "ada@example.test",
        "context": {"order": order["number"], "link_id": link["id"]},
        "order_id": order["id"],
    }
    assert api.get(link_url(order["id"])).json() == body
    [event] = events(api, order["id"], "order.customer_link_sent")
    assert event["detail"] == {"to": "ada@example.test"}
    assert event["by"]["kind"] == "browser"


def test_a_new_link_revokes_the_old_one(api: TestClient) -> None:
    order = back_office_order(api)
    assert api.post(link_url(order["id"])).status_code == 200
    again = api.post(link_url(order["id"]), json={"to": " grace@example.org "})
    assert again.status_code == 200, again.text
    assert (again.json()["live"], again.json()["sent_to"]) == (True, "grace@example.org")
    links = rows(
        api,
        "SELECT sent_to, revoked_at, token_sealed FROM storefront_order_links ORDER BY created_at",
    )
    assert [(r["sent_to"], r["revoked_at"] is None) for r in links] == [
        ("ada@example.test", False),
        ("grace@example.org", True),
    ]
    assert links[0]["token_sealed"] is None
    mails = rows(api, "SELECT to_address FROM storefront_mail ORDER BY created_at")
    assert [m["to_address"] for m in mails] == ["ada@example.test", "grace@example.org"]
    assert len(events(api, order["id"], "order.customer_link_sent")) == 2


def test_revoke_ends_the_live_link(api: TestClient) -> None:
    order = back_office_order(api)
    assert api.post(link_url(order["id"])).status_code == 200
    revoked = api.delete(link_url(order["id"]))
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert (body["live"], body["sent_to"]) == (False, "ada@example.test")
    assert body["revoked_at"] is not None
    assert api.get(link_url(order["id"])).json() == body
    [event] = events(api, order["id"], "order.customer_link_revoked")
    assert event["detail"] == {"count": 1}
    # Nothing live: a second revoke changes nothing and writes no event.
    assert api.delete(link_url(order["id"])).json() == body
    assert len(events(api, order["id"], "order.customer_link_revoked")) == 1


def test_one_address_only(api: TestClient) -> None:
    order = back_office_order(api)
    for to in ("ada@example.com, eve@example.com", "Ada <ada@example.com>", "not-an-address"):
        refused = problem(api.post(link_url(order["id"]), json={"to": to}), "email_invalid", 422)
        assert refused["errors"][0]["loc"] == ["body", "to"]
    assert rows(api, "SELECT id FROM storefront_order_links") == []
    assert rows(api, "SELECT id FROM storefront_mail") == []
    assert events(api, order["id"], "order.customer_link_sent") == []


def test_no_email_contact_is_422_no_email(api: TestClient) -> None:
    phones = [c for c in ADA["contacts"] if c["kind"] != "email"]
    order = back_office_order(api, contacts=phones)
    refused = problem(api.post(link_url(order["id"])), "no_email", 422)
    assert refused["errors"][0]["loc"] == ["body", "to"]
    assert rows(api, "SELECT id FROM storefront_order_links") == []
    # An explicit address needs no contact.
    sent = api.post(link_url(order["id"]), json={"to": "ada@example.com"})
    assert sent.status_code == 200, sent.text


def test_a_cart_and_a_missing_order_are_refused(api: TestClient, pg_pool: PgPool) -> None:
    orders = _orders(api)
    visitor = VisitorStore(pg_pool).ensure("v" * 43)
    by = visitor_actor(visitor)
    cart = orders.lifecycle.ensure_draft(visitor.id, by, currency="USD")
    orders.lifecycle.add_items(cart.id, by, [NewModelItem(resolved=resolved(), quantity=1)])
    body = {"to": "ada@example.com"}
    problem(api.post(link_url(cart.id), json=body), "order_not_submitted")
    problem(api.get(link_url("nope")), "order_not_found", 404)
    problem(api.post(link_url("nope"), json=body), "order_not_found", 404)
    problem(api.delete(link_url("nope")), "order_not_found", 404)
    assert rows(api, "SELECT id FROM storefront_order_links") == []


def test_send_without_a_kek_is_refused(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    problem(client.post(link_url(order["id"])), "kek_missing")
