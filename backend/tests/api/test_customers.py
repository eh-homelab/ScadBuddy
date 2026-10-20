"""`/api/v1/customers` (#4249, spec 2026-10-18 §3.1, §4; plan Rulings 9, 11, 12)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.models import Actor

ADA: dict[str, Any] = {
    "display_name": "Ada Lovelace",
    "company": "Analytical Engines",
    "notes": "prefers email",
    "tags": ["vip"],
    "contacts": [
        {"kind": "phone", "value": "+44 20 0000 0001", "label": "work", "primary": True},
        {"kind": "phone", "value": "+44 20 0000 0002", "label": "home"},
        {"kind": "email", "value": "ada@example.test", "primary": True},
    ],
    "addresses": [
        {
            "label": "home",
            "recipient": "Ada Lovelace",
            "lines": ["1 Analytical Way"],
            "locality": "London",
            "postal_code": "N1 1AA",
            "country": "GB",
            "default_shipping": True,
        }
    ],
}


def create(client: TestClient, body: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.post("/api/v1/customers", json=body or ADA)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_create_read_and_list(client: TestClient) -> None:
    customer = create(client)
    assert customer["display_name"] == "Ada Lovelace"
    assert [c["position"] for c in customer["contacts"]] == [0, 1, 2]
    assert customer["contacts"][0]["primary"] is True
    assert customer["addresses"][0]["lines"] == ["1 Analytical Way"]
    assert customer["archived_at"] is None
    assert client.get(f"/api/v1/customers/{customer['id']}").json() == customer
    assert client.get("/api/v1/customers/nobody").status_code == 404

    create(client, {**ADA, "display_name": "Bob", "contacts": [], "addresses": []})
    listed = client.get("/api/v1/customers").json()
    assert [c["display_name"] for c in listed] == ["Ada Lovelace", "Bob"]
    assert [c["id"] for c in client.get("/api/v1/customers", params={"q": "0002"}).json()] == [
        customer["id"]
    ]
    assert [
        c["display_name"] for c in client.get("/api/v1/customers", params={"q": "bo"}).json()
    ] == ["Bob"]
    assert len(client.get("/api/v1/customers", params={"limit": 1}).json()) == 1
    assert client.get("/api/v1/customers", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/customers", params={"limit": 501}).status_code == 422


def test_archive_hides_from_the_default_list(client: TestClient) -> None:
    customer = create(client)
    archived = client.post(f"/api/v1/customers/{customer['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert client.get("/api/v1/customers").json() == []
    shown = client.get("/api/v1/customers", params={"include_archived": "true"}).json()
    assert [c["id"] for c in shown] == [customer["id"]]
    assert client.post("/api/v1/customers/nobody/archive").status_code == 404


def test_a_second_primary_of_a_kind_is_422(client: TestClient) -> None:
    body = {
        **ADA,
        "contacts": [
            {"kind": "email", "value": "a@example.test", "primary": True},
            {"kind": "email", "value": "b@example.test", "primary": True},
        ],
    }
    response = client.post("/api/v1/customers", json=body)
    assert response.status_code == 422
    assert "primary" in response.text
    assert client.get("/api/v1/customers").json() == []
    customer = create(client)
    patched = client.patch(
        f"/api/v1/customers/{customer['id']}", json={"contacts": body["contacts"]}
    )
    assert patched.status_code == 422
    assert len(client.get(f"/api/v1/customers/{customer['id']}").json()["contacts"]) == 3


def test_an_unknown_query_parameter_is_400(client: TestClient) -> None:
    response = client.get("/api/v1/customers", params={"name": "Ada"})
    assert response.status_code == 400
    assert response.json()["type"] == "https://scadbuddy.dev/problems/unknown-parameter"


def test_a_body_naming_its_author_or_a_derived_field_is_422(client: TestClient) -> None:
    for extra in ({"created_by": "me"}, {"archived_at": None}, {"id": "x"}):
        response = client.post("/api/v1/customers", json={**ADA, **extra})
        assert response.status_code == 422, extra
    customer = create(client)
    assert (
        client.patch(f"/api/v1/customers/{customer['id']}", json={"updated_at": "now"}).status_code
        == 422
    )
    # A contact id on a create names a row that cannot exist yet.
    body = {**ADA, "contacts": [{"id": "r1", "kind": "email", "value": "x@example.test"}]}
    assert client.post("/api/v1/customers", json=body).status_code == 422


def test_patch_keeps_rows_by_id_and_refuses_another_customers(client: TestClient) -> None:
    customer = create(client)
    other = create(client, {**ADA, "display_name": "Bob", "contacts": [], "addresses": []})
    _work, _home, email = customer["contacts"]
    patched = client.patch(
        f"/api/v1/customers/{customer['id']}",
        json={
            "display_name": "Ada King",
            "tags": [],
            "contacts": [
                {"id": email["id"], "kind": "email", "value": "ada@lovelace.test", "primary": True},
                {"kind": "website", "value": "https://lovelace.test"},
            ],
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert (body["display_name"], body["tags"], body["company"]) == ("Ada King", [], ADA["company"])
    assert [(c["id"] == email["id"], c["value"], c["position"]) for c in body["contacts"]] == [
        (True, "ada@lovelace.test", 0),
        (False, "https://lovelace.test", 1),
    ]
    assert len(body["addresses"]) == 1  # untouched: the body did not carry addresses
    stolen = client.patch(
        f"/api/v1/customers/{other['id']}",
        json={
            "addresses": [{"id": customer["addresses"][0]["id"], "recipient": "Bob", "lines": []}]
        },
    )
    assert stolen.status_code == 422
    assert "addresses" in stolen.text
    assert (
        client.get(f"/api/v1/customers/{customer['id']}").json()["addresses"][0]["id"]
        == (customer["addresses"][0]["id"])
    )
    assert client.patch("/api/v1/customers/nobody", json={"notes": "x"}).status_code == 404


def test_editing_a_customer_keeps_a_draft_orders_address(app: FastAPI, client: TestClient) -> None:
    """Ruling 11: a draft order follows the customer's address rows by id; an address
    the body drops clears the order's pointer."""
    customer = create(client)
    home = customer["addresses"][0]
    state: AppState = getattr(app.state, STATE_ATTR)
    store = state.components.get(ORDERS).store
    with store.transaction() as conn:
        order = store.insert_order(
            conn,
            customer_id=customer["id"],
            currency="USD",
            created_by=Actor(kind="browser"),
            shipping_address_id=home["id"],
        )
    kept = client.patch(
        f"/api/v1/customers/{customer['id']}",
        json={
            "addresses": [
                {**{k: v for k, v in home.items() if k != "position"}, "recipient": "A. King"}
            ]
        },
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["addresses"][0]["id"] == home["id"]
    assert store.order(order.id).shipping_address_id == home["id"]  # type: ignore[union-attr]

    dropped = client.patch(f"/api/v1/customers/{customer['id']}", json={"addresses": []})
    assert dropped.status_code == 200
    assert store.order(order.id).shipping_address_id is None  # type: ignore[union-attr]
