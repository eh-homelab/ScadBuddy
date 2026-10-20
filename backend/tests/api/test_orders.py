"""`/api/v1/orders` reads and plain writes (#4249, spec 2026-10-18 §3.2, §4; plan
Rulings 8-12)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.api.test_customers import ADA
from tests.api.test_customers import create as create_customer

AGENT = {"X-ScadBuddy-Agent-Author": "token:t1", "X-ScadBuddy-Agent-Author-Session": "s1"}


def create_order(client: TestClient, customer_id: str, **fields: Any) -> dict[str, Any]:
    response = client.post("/api/v1/orders", json={"customer_id": customer_id, **fields})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_an_order_is_numbered_and_copies_the_currency_setting(client: TestClient) -> None:
    customer = create_customer(client)
    assert client.put("/api/v1/settings", json={"orders_currency": "EUR"}).status_code == 200
    order = create_order(client, customer["id"], notes="rush", due_at="2026-11-01T00:00:00Z")
    assert order["number"].startswith("ORD-")
    assert order["number"].endswith("-1")
    assert (order["status"], order["currency"], order["notes"]) == ("draft", "EUR", "rush")
    assert order["created_by"] == {"kind": "browser", "principal": None, "session": None}
    assert order["shipping_address_id"] == customer["addresses"][0]["id"]
    assert order["shipping_address"] is None
    # The setting applies to the next order; this one keeps its own.
    assert client.put("/api/v1/settings", json={"orders_currency": "USD"}).status_code == 200
    other = client.post("/api/v1/orders", json={"customer_id": customer["id"]}, headers=AGENT)
    assert other.status_code == 201
    assert other.json()["currency"] == "USD"
    assert other.json()["created_by"] == {"kind": "agent", "principal": "token:t1", "session": "s1"}
    assert other.json()["number"].endswith("-2")
    assert client.get(f"/api/v1/orders/{order['id']}").json()["order"]["currency"] == "EUR"


def test_an_order_needs_a_live_customer(client: TestClient) -> None:
    assert client.post("/api/v1/orders", json={"customer_id": "nobody"}).status_code == 404
    customer = create_customer(client)
    client.post(f"/api/v1/customers/{customer['id']}/archive")
    refused = client.post("/api/v1/orders", json={"customer_id": customer["id"]})
    assert refused.status_code == 409
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/orders/customer_archived"


def test_a_body_with_a_status_or_an_author_is_422(client: TestClient) -> None:
    customer = create_customer(client)
    for extra in ({"status": "accepted"}, {"created_by": {"kind": "browser"}}, {"number": "X"}):
        response = client.post("/api/v1/orders", json={"customer_id": customer["id"], **extra})
        assert response.status_code == 422, extra
    order = create_order(client, customer["id"])
    assert (
        client.patch(f"/api/v1/orders/{order['id']}", json={"status": "closed"}).status_code == 422
    )


def test_the_list_pages_newest_first_and_filters(client: TestClient) -> None:
    ada = create_customer(client)
    bob = create_customer(client, {**ADA, "display_name": "Bob", "contacts": [], "addresses": []})
    made = [create_order(client, ada["id"] if n % 2 else bob["id"]) for n in range(5)]
    ids = [o["id"] for o in made]

    page = client.get("/api/v1/orders", params={"limit": 2}).json()
    assert [o["id"] for o in page["items"]] == ids[4:2:-1]
    assert page["next_cursor"]
    page = client.get("/api/v1/orders", params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert [o["id"] for o in page["items"]] == ids[2:0:-1]
    page = client.get("/api/v1/orders", params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert [o["id"] for o in page["items"]] == ids[0:1]
    assert page["next_cursor"] is None

    mine = client.get("/api/v1/orders", params={"customer_id": ada["id"]}).json()["items"]
    assert [o["id"] for o in mine] == [ids[3], ids[1]]
    assert client.get("/api/v1/orders", params={"status": "quoted"}).json()["items"] == []
    by_q = client.get("/api/v1/orders", params={"q": "bob"}).json()["items"]
    assert [o["id"] for o in by_q] == [ids[4], ids[2], ids[0]]
    assert (
        client.get("/api/v1/orders", params={"from": "2099-01-01T00:00:00Z"}).json()["items"] == []
    )
    assert (
        len(client.get("/api/v1/orders", params={"to": "2099-01-01T00:00:00Z"}).json()["items"])
        == 5
    )

    assert client.get("/api/v1/orders", params={"limit": 101}).status_code == 422
    assert client.get("/api/v1/orders", params={"status": "nope"}).status_code == 422
    bad = client.get("/api/v1/orders", params={"cursor": "nonsense"})
    assert bad.status_code == 400
    assert bad.json()["type"] == "https://scadbuddy.dev/problems/bad-cursor"
    unknown = client.get("/api/v1/orders", params={"customer": ada["id"]})
    assert unknown.status_code == 400
    assert unknown.json()["type"] == "https://scadbuddy.dev/problems/unknown-parameter"


def test_patch_edits_notes_due_addresses_and_charges(client: TestClient) -> None:
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    patched = client.patch(
        f"/api/v1/orders/{order['id']}",
        json={
            "notes": "gift wrap",
            "due_at": "2026-12-01T00:00:00Z",
            "billing_address_id": customer["addresses"][0]["id"],
            "charges": [
                {"kind": "shipping", "label": "Post", "amount_minor": 500},
                {"kind": "discount", "label": "Friends", "amount_minor": -200},
            ],
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["notes"] == "gift wrap"
    assert patched.json()["billing_address_id"] == customer["addresses"][0]["id"]
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert [c["amount_minor"] for c in detail["charges"]] == [500, -200]
    assert detail["totals"] == {
        "subtotal_minor": 0,
        "charges_minor": 300,
        "total_minor": 300,
        "paid_minor": 0,
        "balance_minor": 300,
    }
    cleared = client.patch(f"/api/v1/orders/{order['id']}", json={"shipping_address_id": None})
    assert cleared.status_code == 200
    assert cleared.json()["shipping_address_id"] is None
    assert client.patch("/api/v1/orders/nope", json={"notes": "x"}).status_code == 404


def test_another_customers_address_is_refused(client: TestClient) -> None:
    ada = create_customer(client)
    bob = create_customer(client, {**ADA, "display_name": "Bob"})
    order = create_order(client, ada["id"])
    refused = client.patch(
        f"/api/v1/orders/{order['id']}", json={"shipping_address_id": bob["addresses"][0]["id"]}
    )
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["loc"] == ["body", "shipping_address_id"]
    assert (
        client.get(f"/api/v1/orders/{order['id']}").json()["order"]["shipping_address_id"]
        == (ada["addresses"][0]["id"])
    )


def test_payments_hold_and_the_audit_line(client: TestClient) -> None:
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    paid = client.post(
        f"/api/v1/orders/{order['id']}/payments",
        json={"amount_minor": 1500, "method": "cash", "reference": "R1"},
    )
    assert paid.status_code == 201, paid.text
    assert paid.json()["amount_minor"] == 1500
    held = client.post(f"/api/v1/orders/{order['id']}/hold")
    assert held.status_code == 200 and held.json()["on_hold"] is True
    released = client.post(f"/api/v1/orders/{order['id']}/release")
    assert released.json()["on_hold"] is False
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert [p["amount_minor"] for p in detail["payments"]] == [1500]
    assert detail["totals"]["balance_minor"] == -1500
    events = client.get(f"/api/v1/orders/{order['id']}/events").json()
    assert [e["kind"] for e in events] == [
        "order.created",
        "payment.added",
        "order.hold",
        "order.release",
    ]
    assert events[0]["by"] == {"kind": "browser", "principal": None, "session": None}
    assert client.get("/api/v1/orders/nope").status_code == 404
    assert client.get("/api/v1/orders/nope/events").status_code == 404
    assert (
        client.post(f"/api/v1/orders/{order['id']}/payments", json={"amount": 1}).status_code == 422
    )
