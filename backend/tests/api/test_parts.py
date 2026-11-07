"""Parts inventory over the routes (#5543, job cost spec §11): parts and their stock, a
model's rule, an item's parts, what an order is short, and the use on fulfil."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_orders_lifecycle import fulfil
from tests.api.test_orders_unfulfil import accepted_order, unfulfil
from tests.api.test_versions import SLUG

pytestmark = pytest.mark.requires_git


def make_part(client: TestClient, **fields: Any) -> dict[str, Any]:
    body = {"name": "Silver hook", "pack_size": 100, "pack_price_minor": 599, **fields}
    made = client.post("/api/v1/parts", json=body)
    assert made.status_code == 201, made.text
    part: dict[str, Any] = made.json()
    return part


def test_parts_and_their_stock(client: TestClient) -> None:
    part = make_part(client, on_hand=3, low_stock=5)
    assert (part["unit_cost_minor"], part["currency"], part["low"]) == ("5.99", "USD", True)
    assert client.post("/api/v1/parts", json={**part, "name": "SILVER HOOK"}).status_code == 422
    clash = client.post(
        "/api/v1/parts", json={"name": "SILVER HOOK", "pack_size": 1, "pack_price_minor": 1}
    )
    assert clash.status_code == 409
    assert clash.json()["part_id"] == part["id"]

    added = client.post(f"/api/v1/parts/{part['id']}/stock", json={"packs": 1})
    assert added.status_code == 200, added.text
    assert (added.json()["on_hand"], added.json()["low"]) == (103, False)

    assert (
        client.post(f"/api/v1/parts/{part['id']}/adjust", json={"on_hand": 100}).status_code == 422
    )
    fixed = client.post(
        f"/api/v1/parts/{part['id']}/adjust", json={"on_hand": 100, "note": "counted"}
    )
    assert fixed.json()["on_hand"] == 100
    moves = client.get(f"/api/v1/parts/{part['id']}/moves").json()
    assert [(m["kind"], m["delta"]) for m in moves] == [("adjust", -3), ("add", 100), ("adjust", 3)]

    renamed = client.patch(f"/api/v1/parts/{part['id']}", json={"supplier": "AliExpress"})
    assert renamed.json()["supplier"] == "AliExpress"
    assert client.get("/api/v1/parts?low=true").json() == []
    assert client.post("/api/v1/parts/nope/stock", json={}).status_code == 404

    # #5549 review: packs times the pack size past one change's most is a 422, never a 500.
    big = make_part(client, name="Magnet", pack_size=1_000_000)
    too_many = client.post(f"/api/v1/parts/{big['id']}/stock", json={"packs": 10_000})
    assert too_many.status_code == 422, too_many.text
    assert too_many.json()["errors"][0]["loc"] == ["body", "packs"]


def test_a_rule_fills_a_new_item_and_fulfil_uses_its_parts(client: TestClient) -> None:
    part = make_part(client, on_hand=1)
    rule = client.put(
        f"/api/v1/parts/rules/{SLUG}", json={"lines": [{"per_unit": 2, "part_id": part["id"]}]}
    )
    assert rule.status_code == 200, rule.text
    assert client.get(f"/api/v1/parts/rules/{SLUG}").json()["lines"][0]["per_unit"] == 2
    bad = client.put(f"/api/v1/parts/rules/{SLUG}", json={"lines": [{"part_id": "nope"}]})
    assert bad.status_code == 404

    order_id, [item_id] = accepted_order(client)
    parts = client.get(f"/api/v1/orders/{order_id}/parts").json()
    assert parts["items"] == [
        {"item_id": item_id, "parts": [{"part_id": part["id"], "per_unit": 2, "used": 0}]}
    ]
    assert [(s["name"], s["short"]) for s in parts["short"]] == [("Silver hook", 1)]

    fulfil(client, order_id, item_id, 1)

    costs = client.get(f"/api/v1/orders/{order_id}/costs").json()
    [entry] = costs["items"][0]["cost_entries"]
    assert (entry["kind"], entry["amount_minor"], entry["source"]) == (
        "material",
        12,
        f"part:{item_id}:{part['id']}",
    )
    assert client.get("/api/v1/parts").json()[0]["on_hand"] == -1
    refused = client.put(f"/api/v1/orders/{order_id}/items/{item_id}/parts", json={"parts": []})
    assert refused.status_code == 409
    assert refused.json()["type"].endswith("/item_parts_used")

    unfulfil(client, order_id, item_id)
    assert client.get("/api/v1/parts").json()[0]["on_hand"] == 1
    kinds = [e["kind"] for e in client.get(f"/api/v1/orders/{order_id}/events").json()]
    assert {"item.parts_set", "parts.used", "parts.returned"} <= set(kinds)


def test_an_items_parts_are_set_by_hand(client: TestClient) -> None:
    part = make_part(client)
    order_id, [item_id] = accepted_order(client)
    path = f"/api/v1/orders/{order_id}/items/{item_id}/parts"

    twice = {"parts": [{"part_id": part["id"], "per_unit": 1}] * 2}
    assert client.put(path, json=twice).status_code == 422
    unknown = client.put(path, json={"parts": [{"part_id": "nope", "per_unit": 1}]})
    assert unknown.status_code == 409
    done = client.put(path, json={"parts": [{"part_id": part["id"], "per_unit": 3}]})
    assert done.status_code == 200, done.text
    assert done.json() == [{"part_id": part["id"], "per_unit": 3, "used": 0}]

    history = client.get(
        "/api/v1/orders/history/unit-cost", params={"slug": SLUG, "item_id": item_id}
    ).json()
    # No history yet: nothing is figured, the parts included.
    assert (history["jobs"], history["parts_unit_minor"]) == (0, None)
