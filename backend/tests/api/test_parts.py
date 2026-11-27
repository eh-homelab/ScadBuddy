"""The rule a model gives its items and an order's parts (#5543, job cost spec §11). The
`/parts` alias routes over the inventory are gone (#6949, K1 5/7)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from scadbuddy.inventory.component import INVENTORY, InventoryComponent
from scadbuddy.inventory.models import ReceiveLot
from scadbuddy.orders.models import Actor
from tests.api.test_orders_lifecycle import fulfil
from tests.api.test_orders_unfulfil import accepted_order, unfulfil
from tests.api.test_settings_runtime import _state
from tests.api.test_versions import SLUG
from tests.support.inventory_rows import insert_item

pytestmark = pytest.mark.requires_git


def _inventory(client: TestClient) -> InventoryComponent:
    return _state(client).components.get(INVENTORY)


def _on_hand(client: TestClient, item_id: str) -> Decimal:
    return sum((lot.on_hand for lot in _inventory(client).stock.lots(UUID(item_id))), Decimal(0))


def make_part(client: TestClient, qty: int = 0) -> dict[str, Any]:
    """An item of kind `hardware` (Go makes items since K1, so the row is inserted), with
    `qty` units received at 5.99 each."""
    item_id = insert_item(_state(client).refs.pool, "hardware", name="Silver hook")
    if qty:
        _inventory(client).stock.receive_lot(
            item_id,
            ReceiveLot(qty=Decimal(qty), unit_cost_minor=Decimal("5.99")),
            Actor(kind="browser"),
        )
    return {"id": str(item_id)}


def test_the_part_aliases_are_gone(client: TestClient) -> None:
    assert client.get("/api/v1/parts").status_code == 404
    assert client.post("/api/v1/parts/x/stock", json={}).status_code == 404


def test_rules_and_order_parts_still_work(client: TestClient) -> None:
    assert client.get(f"/api/v1/parts/rules/{SLUG}").json()["lines"] == []
    part = make_part(client)
    put = client.put(
        f"/api/v1/parts/rules/{SLUG}", json={"lines": [{"per_unit": 1, "item_id": part["id"]}]}
    )
    assert put.status_code == 200, put.text
    order_id, [item_id] = accepted_order(client)
    parts = client.get(f"/api/v1/orders/{order_id}/parts")
    assert parts.status_code == 200, parts.text
    assert [i["item_id"] for i in parts.json()["items"]] == [item_id]


def test_a_rule_fills_a_new_item_and_fulfil_takes_its_parts(client: TestClient) -> None:
    part = make_part(client, qty=1)
    rule = client.put(
        f"/api/v1/parts/rules/{SLUG}", json={"lines": [{"per_unit": 2, "part_id": part["id"]}]}
    )
    assert rule.status_code == 200, rule.text
    assert client.get(f"/api/v1/parts/rules/{SLUG}").json()["lines"][0]["item_id"] == part["id"]
    bad = client.put(f"/api/v1/parts/rules/{SLUG}", json={"lines": [{"part_id": "nope"}]})
    assert bad.status_code == 404

    order_id, [item_id] = accepted_order(client)
    parts = client.get(f"/api/v1/orders/{order_id}/parts").json()
    row = {"item_id": part["id"], "part_id": part["id"], "per_unit": 2, "used": 0}
    assert parts["items"] == [{"item_id": item_id, "parts": [row]}]
    assert [(s["name"], s["short"]) for s in parts["short"]] == [("Silver hook", "1")]

    fulfil(client, order_id, item_id, 1)

    costs = client.get(f"/api/v1/orders/{order_id}/costs").json()
    assert costs["items"][0]["cost_entries"] == []
    assert costs["items"][0]["parts_minor"] == 12
    assert _on_hand(client, part["id"]) == -1
    refused = client.put(f"/api/v1/orders/{order_id}/items/{item_id}/parts", json={"parts": []})
    assert refused.status_code == 409
    assert refused.json()["type"].endswith("/item_parts_used")

    unfulfil(client, order_id, item_id)
    assert _on_hand(client, part["id"]) == 1
    kinds = [e["kind"] for e in client.get(f"/api/v1/orders/{order_id}/events").json()]
    assert {"item.parts_set", "parts.used", "parts.returned"} <= set(kinds)


def test_an_items_parts_are_set_by_hand(client: TestClient) -> None:
    part = make_part(client)
    order_id, [item_id] = accepted_order(client)
    path = f"/api/v1/orders/{order_id}/items/{item_id}/parts"

    twice = {"parts": [{"item_id": part["id"], "per_unit": 1}] * 2}
    assert client.put(path, json=twice).status_code == 422
    unknown = client.put(path, json={"parts": [{"item_id": "nope", "per_unit": 1}]})
    assert unknown.status_code == 409
    done = client.put(path, json={"parts": [{"item_id": part["id"], "per_unit": 3}]})
    assert done.status_code == 200, done.text
    assert done.json() == [{"item_id": part["id"], "part_id": part["id"], "per_unit": 3, "used": 0}]

    history = client.get(
        "/api/v1/orders/history/unit-cost", params={"slug": SLUG, "item_id": item_id}
    ).json()
    # No history yet: nothing is figured, the parts included.
    assert (history["jobs"], history["parts_unit_minor"]) == (0, None)
