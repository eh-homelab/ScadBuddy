"""The items card's routes (#4955, order operations spec 2026-11-07 §4.2): `POST
/orders/{id}/items/{item}/duplicate` and the bulk fulfil `POST /orders/{id}/fulfil`."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from scadbuddy.orders.problems import PROBLEMS
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order


def custom(client: TestClient, order_id: str, title: str, quantity: int) -> dict[str, Any]:
    added = client.post(
        f"/api/v1/orders/{order_id}/items",
        json={"kind": "custom", "title": title, "quantity": quantity, "unit_price_minor": 500},
    )
    assert added.status_code == 201, added.text
    item: dict[str, Any] = added.json()
    return item


def test_an_item_is_duplicated(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    item = custom(client, order["id"], "Design work", 2)
    copied = client.post(f"/api/v1/orders/{order['id']}/items/{item['id']}/duplicate", json={})
    assert copied.status_code == 201, copied.text
    copy = copied.json()
    assert copy["id"] != item["id"]
    assert (copy["title"], copy["quantity"], copy["kind"]) == ("Design work", 2, "custom")
    gone = client.post(f"/api/v1/orders/{order['id']}/items/{'f' * 32}/duplicate", json={})
    assert gone.status_code == 404, gone.text


def test_items_are_fulfilled_at_once(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    first = custom(client, order["id"], "Hook", 2)
    second = custom(client, order["id"], "Tag", 3)
    path = f"/api/v1/orders/{order['id']}/fulfil"
    lines = {
        "items": [{"item_id": first["id"], "quantity": 2}, {"item_id": second["id"], "quantity": 1}]
    }
    # Not accepted yet: nothing is made.
    early = client.post(path, json=lines)
    assert early.status_code == 409, early.text
    assert early.json()["type"] == PROBLEMS + "order_not_accepted"
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    made = client.post(path, json=lines)
    assert made.status_code == 201, made.text
    assert [(a["order_item_id"], a["quantity"]) for a in made.json()] == [
        (first["id"], 2),
        (second["id"], 1),
    ]
    statuses = {
        i["id"]: i["status"] for i in client.get(f"/api/v1/orders/{order['id']}").json()["items"]
    }
    assert (statuses[first["id"]], statuses[second["id"]]) == ("fulfilled", "approved")
    twice = client.post(path, json={"items": [{"item_id": second["id"], "quantity": 1}] * 2})
    assert twice.status_code == 422, twice.text
    none = client.post(path, json={"items": []})
    assert none.status_code == 422, none.text
