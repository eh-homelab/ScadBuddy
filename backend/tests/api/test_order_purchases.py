"""Purchases over the routes (#6133, spec 2026-11-13 §10, §11): the back office lists an
order's purchase links, records what it paid, changes it and marks a link not needed; the
`purchase_pending` gate answers 409 naming the links."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_orders import AGENT, create_order
from tests.api.test_orders_lifecycle import add_item, problem

pytestmark = pytest.mark.requires_postgres

LINK = "https://parts.example.com/magnets"


def quoted_order(client: TestClient, pg_conninfo: str) -> tuple[str, str, str]:
    """A `quoted` order with one custom item and one pending purchase link on it."""
    order = create_order(client, create_customer(client)["id"])
    item = add_item(
        client, order["id"], kind="custom", title="Box", quantity=1, unit_price_minor=1000
    )
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO order_item_sources (order_id, item_id, position, kind, purpose, url,"
            " link_type, note, status) VALUES (%s, %s, 0, 'link', 'purchase', %s, 'manual',"
            " 'magnets', 'pending') RETURNING id",
            (order["id"], item["id"], LINK),
        ).fetchone()
    assert row is not None
    return order["id"], item["id"], row[0]


def purchases(client: TestClient, order_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/orders/{order_id}/purchases")
    assert response.status_code == 200, response.text
    body: list[dict[str, Any]] = response.json()
    return body


def test_a_purchase_is_listed_recorded_changed_and_declined(
    client: TestClient, pg_conninfo: str
) -> None:
    assert (
        client.put(
            "/api/v1/settings",
            json={"orders_purchase_fee_flat_minor": 100, "orders_purchase_fee_percent": "10"},
        ).status_code
        == 200
    )
    order_id, item_id, source_id = quoted_order(client, pg_conninfo)
    [listed] = purchases(client, order_id)
    assert (listed["id"], listed["item_id"], listed["url"], listed["status"]) == (
        source_id,
        item_id,
        LINK,
        "pending",
    )

    path = f"/api/v1/orders/{order_id}/purchases/{source_id}"
    body = {"amount_minor": 500, "cost_kind": "material", "note": "order 77"}
    made = client.post(f"{path}/record", json=body, headers=AGENT)
    again = client.post(f"{path}/record", json=body, headers=AGENT)
    assert (made.status_code, again.status_code) == (201, 200), made.text
    assert (made.json()["status"], made.json()["amount_minor"]) == ("recorded", 500)
    detail = client.get(f"/api/v1/orders/{order_id}").json()
    assert [(c["kind"], c["amount_minor"]) for c in detail["charges"]] == [
        ("purchase", 500),
        ("handling", 150),
    ]
    assert detail["charges"][0]["purchase_link_id"] == source_id
    assert detail["totals"]["total_minor"] == 1650

    changed = client.patch(path, json={"amount_minor": 800})
    assert changed.status_code == 200, changed.text
    assert changed.json()["amount_minor"] == 800
    costs = client.get(f"/api/v1/orders/{order_id}/costs").json()
    [entry] = costs["items"][0]["cost_entries"]
    assert (entry["amount_minor"], entry["source"]) == (800, f"purchase:{source_id}")
    removed = client.delete(f"/api/v1/orders/{order_id}/cost-entries/{entry['id']}")
    problem(removed, "purchase_cost_linked")

    declined = client.post(f"{path}/decline", json={"shop_note": "In stock already."})
    assert declined.status_code == 200, declined.text
    assert (declined.json()["status"], declined.json()["shop_note"]) == (
        "not_needed",
        "In stock already.",
    )
    assert client.get(f"/api/v1/orders/{order_id}").json()["charges"] == []
    kinds = [e["kind"] for e in client.get(f"/api/v1/orders/{order_id}/events").json()]
    assert kinds[-3:] == ["purchase.recorded", "purchase.updated", "purchase.declined"]


def test_a_pending_purchase_refuses_the_accept_and_names_the_link(
    client: TestClient, pg_conninfo: str
) -> None:
    order_id, item_id, source_id = quoted_order(client, pg_conninfo)
    body = problem(client.post(f"/api/v1/orders/{order_id}/accept"), "purchase_pending")
    assert (body["sources"], body["items"]) == ([source_id], [item_id])
    balance = client.post(f"/api/v1/orders/{order_id}/payment-requests", json={"provider": None})
    problem(balance, "purchase_pending")
    deposit = client.post(
        f"/api/v1/orders/{order_id}/payment-requests",
        json={"kind": "deposit", "amount_minor": 200, "provider": None},
    )
    assert deposit.status_code == 201, deposit.text
    record = client.post(
        f"/api/v1/orders/{order_id}/purchases/{source_id}/record", json={"amount_minor": 300}
    )
    assert record.status_code == 201, record.text
    accepted = client.post(f"/api/v1/orders/{order_id}/accept")
    assert accepted.status_code == 200, accepted.text
    changed = client.patch(
        f"/api/v1/orders/{order_id}/purchases/{source_id}", json={"amount_minor": 1}
    )
    problem(changed, "purchase_locked")


def test_unknown_links_and_bad_bodies_are_refused(client: TestClient, pg_conninfo: str) -> None:
    order_id, _, source_id = quoted_order(client, pg_conninfo)
    missing = client.post(
        f"/api/v1/orders/{order_id}/purchases/nope/record", json={"amount_minor": 1}
    )
    problem(missing, "source_not_found", 404)
    zero = client.post(
        f"/api/v1/orders/{order_id}/purchases/{source_id}/record", json={"amount_minor": 0}
    )
    assert zero.status_code == 422
    assert client.get("/api/v1/orders/nope/purchases").status_code == 404
