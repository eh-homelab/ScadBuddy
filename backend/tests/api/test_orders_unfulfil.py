"""Undoing a fulfilment (#4952): a fulfilled item back to what its attempts give it,
its accepted attempt kept and marked not accepted, the order derived again, on the
record."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import fulfil, problem, quote_and_accept
from tests.api.test_orders_reopen import detail, item_of
from tests.api.test_versions import upload

pytestmark = pytest.mark.requires_git


def accepted_order(client: TestClient, items: int = 1) -> tuple[str, list[str]]:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    ids = [item_of(client, order["id"], title=f"Item {n}")["id"] for n in range(items)]
    for item_id in ids:
        quote_and_accept(client, order["id"], item_id)
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    return order["id"], ids


def attempt(client: TestClient, order_id: str, item_id: str, outcome: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/orders/{order_id}/items/{item_id}/fulfilments",
        json={"quantity": 1 if outcome != "failed" else 0, "outcome": outcome},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def unfulfil(client: TestClient, order_id: str, item_id: str) -> Any:
    return client.post(f"/api/v1/orders/{order_id}/items/{item_id}/unfulfil")


def attempts(client: TestClient, order_id: str, item_id: str) -> list[dict[str, Any]]:
    item = next(i for i in detail(client, order_id)["items"] if i["id"] == item_id)
    found: list[dict[str, Any]] = item["fulfilments"]
    return found


def test_an_item_with_one_attempt_goes_back_to_approved(client: TestClient) -> None:
    order_id, [item_id] = accepted_order(client)
    accepted = fulfil(client, order_id, item_id, 1)
    assert detail(client, order_id)["order"]["status"] == "ready"

    response = unfulfil(client, order_id, item_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    # The attempt stays, marked not accepted.
    [kept] = attempts(client, order_id, item_id)
    assert (kept["id"], kept["accepted_at"]) == (accepted["id"], None)
    # The order is derived again: an item with an attempt is in production.
    assert detail(client, order_id)["order"]["status"] == "in_production"
    events = client.get(f"/api/v1/orders/{order_id}/events").json()
    [undo] = [e for e in events if e["kind"] == "item.unfulfilled"]
    assert undo["detail"] == {
        "item_id": item_id,
        "fulfilment_id": accepted["id"],
        "attempt": 1,
        "to": "approved",
    }
    assert undo["by"]["kind"] == "browser"
    # Accepted again, it is fulfilled again.
    again = client.post(
        f"/api/v1/orders/{order_id}/items/{item_id}/fulfilments/{accepted['id']}/accept"
    )
    assert again.status_code == 200, again.text
    assert detail(client, order_id)["order"]["status"] == "ready"


def test_one_other_attempt_leaves_the_item_approved(client: TestClient) -> None:
    order_id, [item_id] = accepted_order(client)
    attempt(client, order_id, item_id, "failed")
    accepted = fulfil(client, order_id, item_id, 1)

    assert unfulfil(client, order_id, item_id).json()["status"] == "approved"
    found = attempts(client, order_id, item_id)
    assert [(a["attempt"], a["accepted_at"]) for a in found] == [(1, None), (2, None)]
    assert found[1]["id"] == accepted["id"]


def test_two_other_attempts_follow_the_latest_outcome(client: TestClient) -> None:
    order_id, [item_id] = accepted_order(client)
    made = attempt(client, order_id, item_id, "succeeded")
    attempt(client, order_id, item_id, "partial")
    attempt(client, order_id, item_id, "failed")
    # The first attempt accepted after the item failed: fulfilled.
    accepted = client.post(
        f"/api/v1/orders/{order_id}/items/{item_id}/fulfilments/{made['id']}/accept"
    )
    assert accepted.status_code == 200, accepted.text
    assert detail(client, order_id)["items"][0]["status"] == "fulfilled"

    # Without the accepted one, its latest attempt failed: the item is failed again.
    assert unfulfil(client, order_id, item_id).json()["status"] == "failed"
    assert all(a["accepted_at"] is None for a in attempts(client, order_id, item_id))


def test_only_the_latest_accepted_attempt_is_taken_back(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    two = item_of(client, order["id"], title="Two")
    client.patch(f"/api/v1/orders/{order['id']}/items/{two['id']}", json={"quantity": 2})
    quote_and_accept(client, order["id"], two["id"])
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    first = fulfil(client, order["id"], two["id"], 1)
    second = fulfil(client, order["id"], two["id"], 1)

    assert unfulfil(client, order["id"], two["id"]).json()["status"] == "approved"
    found = {a["id"]: a["accepted_at"] for a in attempts(client, order["id"], two["id"])}
    assert found[first["id"]] is not None
    assert found[second["id"]] is None


def test_the_last_fulfilled_item_takes_the_order_out_of_ready(client: TestClient) -> None:
    order_id, [kept, undone] = accepted_order(client, items=2)
    fulfil(client, order_id, kept, 1)
    fulfil(client, order_id, undone, 1)
    assert detail(client, order_id)["order"]["status"] == "ready"
    unfulfil(client, order_id, undone)
    statuses = {i["id"]: i["status"] for i in detail(client, order_id)["items"]}
    assert statuses == {kept: "fulfilled", undone: "approved"}
    assert detail(client, order_id)["order"]["status"] == "in_production"
    status_rows = [
        e["detail"]
        for e in client.get(f"/api/v1/orders/{order_id}/events").json()
        if e["kind"] == "order.status"
    ]
    assert (status_rows[-1]["from_status"], status_rows[-1]["to"]) == ("ready", "in_production")


def test_a_shipped_item_is_refused(client: TestClient) -> None:
    order_id, [item_id] = accepted_order(client)
    fulfil(client, order_id, item_id, 1)
    shipment = client.post(
        f"/api/v1/orders/{order_id}/shipments",
        json={"items": [{"order_item_id": item_id, "quantity": 1}]},
    )
    assert shipment.status_code == 201, shipment.text

    body = problem(unfulfil(client, order_id, item_id), "order_item_shipped")
    assert body["items"] == [item_id]
    assert body["shipments"] == [shipment.json()["id"]]
    # Taken off its shipment first, it unfulfils.
    emptied = client.patch(
        f"/api/v1/orders/{order_id}/shipments/{shipment.json()['id']}", json={"items": []}
    )
    assert emptied.status_code == 200, emptied.text
    assert unfulfil(client, order_id, item_id).json()["status"] == "approved"


def test_an_item_not_fulfilled_is_refused(client: TestClient) -> None:
    order_id, [item_id] = accepted_order(client)
    body = problem(unfulfil(client, order_id, item_id), "item_not_fulfilled")
    assert body["items"] == [item_id]
    assert unfulfil(client, order_id, "nothing").status_code == 404
    assert unfulfil(client, "nothing", item_id).status_code == 404


def test_an_order_on_hold_is_refused(client: TestClient) -> None:
    order_id, [item_id] = accepted_order(client)
    fulfil(client, order_id, item_id, 1)
    assert client.post(f"/api/v1/orders/{order_id}/hold").status_code == 200
    problem(unfulfil(client, order_id, item_id), "order_on_hold")
    assert detail(client, order_id)["items"][0]["status"] == "fulfilled"


def test_an_ended_order_is_refused(client: TestClient) -> None:
    order_id, [item_id] = accepted_order(client)
    fulfil(client, order_id, item_id, 1)
    client.post(f"/api/v1/orders/{order_id}/cancel")
    problem(unfulfil(client, order_id, item_id), "order_cancelled")
