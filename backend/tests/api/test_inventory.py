"""Inventory kinds, items and offers over HTTP (#7067, spec 6.4 and 6.5): one round trip
per family, the problem codes, and an item's property schema (its kind's, merged)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_settings_runtime import _state
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

API = "/api/v1/inventory"
PROBLEMS = "https://scadbuddy.dev/problems/inventory/"
ENTITY_PROBLEMS = "https://scadbuddy.dev/problems/entities/"


def _code(response: Any, status: int, code: str, prefix: str = PROBLEMS) -> None:
    assert response.status_code == status, response.text
    assert response.json()["type"] == prefix + code


def _kind_id() -> str:
    return f"k{uuid.uuid4().hex[:12]}"


def _kind(client: TestClient, **body: Any) -> dict[str, Any]:
    body = {"id": _kind_id(), "name": "Fasteners", "tracking": "lot", **body}
    made = client.post(f"{API}/kinds", json=body, headers=press())
    assert made.status_code == 201, made.text
    return dict(made.json())


def _item(client: TestClient, kind_id: str = "hardware", **body: Any) -> dict[str, Any]:
    body = {"kind_id": kind_id, "name": f"M3 bolt {uuid.uuid4().hex[:6]}", **body}
    made = client.post(f"{API}/items", json=body, headers=press())
    assert made.status_code == 201, made.text
    return dict(made.json())


def _supplier(client: TestClient) -> str:
    with _state(client).refs.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_suppliers (name) VALUES (%s) RETURNING id",
            (f"Bolt Depot {uuid.uuid4()}",),
        ).fetchone()
    assert row is not None
    return str(row["id"])


MPN_SCHEMA = {"type": "object", "properties": {"mpn": {"type": "string"}}}


# -- kinds -------------------------------------------------------------------------------


def test_a_kind_round_trips(client: TestClient) -> None:
    parent = _kind(client, attribute_schema=MPN_SCHEMA)
    child = _kind(
        client,
        parent_id=parent["id"],
        attribute_schema={"type": "object", "properties": {"thread": {"type": "string"}}},
    )
    listed = client.get(f"{API}/kinds").json()["kinds"]
    assert {parent["id"], child["id"]} <= {k["id"] for k in listed}
    assert "part" in {k["id"] for k in listed}

    patched = client.patch(f"{API}/kinds/{child['id']}", json={"name": "Bolts"}, headers=press())
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Bolts"

    schema = client.get(f"{API}/kinds/{child['id']}/schema")
    assert schema.status_code == 200, schema.text
    body = schema.json()
    assert body["kind_id"] == child["id"]
    assert set(body["schema"]["properties"]) == {"mpn", "thread"}
    assert set(body["own_schema"]["properties"]) == {"thread"}
    assert body["keys_in_use"] == []

    item = _item(client, child["id"])
    client.patch(
        f"/api/v1/entities/item:{item['id']}/properties",
        json={"set": {"mpn": "X1", "colour": "red"}},
        headers=press(),
    )
    keys = client.get(f"{API}/kinds/{parent['id']}/schema").json()["keys_in_use"]
    assert keys == [{"key": "colour", "count": 1}, {"key": "mpn", "count": 1}]

    client.post(f"{API}/items/{item['id']}/archive", headers=press())
    archived = client.post(f"{API}/kinds/{child['id']}/archive", headers=press())
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    assert child["id"] not in {k["id"] for k in client.get(f"{API}/kinds").json()["kinds"]}
    every = client.get(f"{API}/kinds", params={"archived": True}).json()["kinds"]
    assert child["id"] in {k["id"] for k in every}
    restored = client.post(f"{API}/kinds/{child['id']}/restore", headers=press())
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None


def test_kind_refusals(client: TestClient) -> None:
    kind = _kind(client)
    again = client.post(
        f"{API}/kinds", json={"id": kind["id"], "name": "X", "tracking": "lot"}, headers=press()
    )
    _code(again, 409, "kind_exists")
    orphan = client.post(
        f"{API}/kinds",
        json={"id": _kind_id(), "name": "X", "tracking": "lot", "parent_id": "nope_kind"},
        headers=press(),
    )
    _code(orphan, 404, "kind_not_found")
    _code(client.get(f"{API}/kinds/nope_kind/schema"), 404, "kind_not_found")
    _code(client.patch(f"{API}/kinds/nope_kind", json={}, headers=press()), 404, "kind_not_found")
    child = _kind(client, parent_id=kind["id"])
    cycle = client.patch(
        f"{API}/kinds/{kind['id']}", json={"parent_id": child["id"]}, headers=press()
    )
    _code(cycle, 422, "kind_cycle")
    builtin = client.patch(f"{API}/kinds/part", json={"tracking": "unit"}, headers=press())
    _code(builtin, 409, "kind_in_use")
    _item(client, kind["id"])
    _code(client.post(f"{API}/kinds/{kind['id']}/archive", headers=press()), 409, "kind_in_use")
    archived = _kind(client)
    client.post(f"{API}/kinds/{archived['id']}/archive", headers=press())
    into_archived = client.post(
        f"{API}/items", json={"kind_id": archived["id"], "name": "X"}, headers=press()
    )
    _code(into_archived, 409, "kind_archived")


def test_a_restricting_kind_schema_is_refused_and_makes_no_kind(client: TestClient) -> None:
    kind_id = _kind_id()
    refused = client.post(
        f"{API}/kinds",
        json={
            "id": kind_id,
            "name": "Strict",
            "tracking": "lot",
            "attribute_schema": {"type": "object", "required": ["mpn"]},
        },
        headers=press(),
    )
    _code(refused, 422, "bad_property_schema", ENTITY_PROBLEMS)
    every = client.get(f"{API}/kinds", params={"archived": True}).json()["kinds"]
    assert kind_id not in {k["id"] for k in every}
    kind = _kind(client, attribute_schema=MPN_SCHEMA)
    patched = client.patch(
        f"{API}/kinds/{kind['id']}",
        json={"attribute_schema": {"type": "object", "required": ["mpn"]}},
        headers=press(),
    )
    _code(patched, 422, "bad_property_schema", ENTITY_PROBLEMS)
    kept = client.get(f"{API}/kinds/{kind['id']}/schema").json()["own_schema"]
    assert kept == MPN_SCHEMA


# -- items -------------------------------------------------------------------------------


def test_an_item_round_trips(client: TestClient) -> None:
    kind = _kind(client, parent_id="part")
    item = _item(client, kind["id"], name="Brass insert M3", low_stock="5", manufacturer="Ruthex")
    assert (item["kind_id"], item["tracking"], item["on_hand"]) == (kind["id"], "lot", "0")
    assert item["kind_path"] == ["part", kind["id"]]
    assert item["low"] is True

    got = client.get(f"{API}/items/{item['id']}")
    assert got.status_code == 200, got.text
    assert got.json()["name"] == "Brass insert M3"
    patched = client.patch(
        f"{API}/items/{item['id']}", json={"note": "for lids", "low_stock": "0"}, headers=press()
    )
    assert patched.status_code == 200, patched.text
    assert (patched.json()["note"], patched.json()["low"]) == ("for lids", False)

    by_parent = client.get(f"{API}/items", params={"kind": "part"}).json()["items"]
    assert item["id"] in {i["id"] for i in by_parent}
    only_part = client.get(f"{API}/items", params={"kind": "part", "descendants": False})
    assert item["id"] not in {i["id"] for i in only_part.json()["items"]}
    by_q = client.get(f"{API}/items", params={"q": "ruthex"}).json()["items"]
    assert [i["id"] for i in by_q] == [item["id"]]

    archived = client.post(f"{API}/items/{item['id']}/archive", headers=press())
    assert archived.status_code == 200, archived.text
    assert item["id"] not in {i["id"] for i in client.get(f"{API}/items").json()["items"]}
    every = client.get(f"{API}/items", params={"archived": True}).json()["items"]
    assert item["id"] in {i["id"] for i in every}
    restored = client.post(f"{API}/items/{item['id']}/restore", headers=press())
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None


def test_item_refusals(client: TestClient) -> None:
    item = _item(client)
    twin = client.post(
        f"{API}/items", json={"kind_id": "hardware", "name": item["name"].upper()}, headers=press()
    )
    _code(twin, 409, "item_exists")
    missing = uuid.uuid4()
    _code(client.get(f"{API}/items/{missing}"), 404, "item_not_found")
    _code(client.patch(f"{API}/items/{missing}", json={}, headers=press()), 404, "item_not_found")
    _code(client.post(f"{API}/items/{missing}/archive", headers=press()), 404, "item_not_found")
    filament = client.post(
        f"{API}/items", json={"kind_id": "filament", "name": "PLA"}, headers=press()
    )
    _code(filament, 409, "filament_item_refused")
    unknown = client.post(
        f"{API}/items", json={"kind_id": "nope_kind", "name": "X"}, headers=press()
    )
    _code(unknown, 404, "kind_not_found")


# -- offers ------------------------------------------------------------------------------


def test_an_offer_round_trips_with_its_prices(client: TestClient) -> None:
    item = _item(client)
    supplier = _supplier(client)
    made = client.post(
        f"{API}/offers",
        json={"supplier_id": supplier, "item_id": item["id"], "sku": "M3-100", "pack_size": "100"},
        headers=press(),
    )
    assert made.status_code == 201, made.text
    offer = made.json()
    assert offer["latest_price"] is None

    assert [o["id"] for o in client.get(f"{API}/items/{item['id']}/offers").json()["offers"]] == [
        offer["id"]
    ]
    by_supplier = client.get(f"{API}/suppliers/{supplier}/offers").json()["offers"]
    assert [o["id"] for o in by_supplier] == [offer["id"]]

    patched = client.patch(
        f"{API}/offers/{offer['id']}", json={"url": "https://example.com/m3"}, headers=press()
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["url"] == "https://example.com/m3"

    price = client.post(
        f"{API}/offers/{offer['id']}/prices",
        json={"price": "4.99", "currency": "EUR", "on_date": "2026-11-20"},
        headers=press(),
    )
    assert price.status_code == 201, price.text
    assert (price.json()["currency"], price.json()["source"]) == ("EUR", "manual")
    prices = client.get(f"{API}/offers/{offer['id']}/prices").json()["prices"]
    assert [p["id"] for p in prices] == [price.json()["id"]]
    assert (
        client.get(f"{API}/items/{item['id']}/offers").json()["offers"][0]["latest_price"]["id"]
        == price.json()["id"]
    )

    archived = client.post(f"{API}/offers/{offer['id']}/archive", headers=press())
    assert archived.status_code == 200, archived.text
    assert client.get(f"{API}/items/{item['id']}/offers").json()["offers"] == []


def test_offer_refusals(client: TestClient) -> None:
    item = _item(client)
    supplier = _supplier(client)
    body = {"supplier_id": supplier, "item_id": item["id"], "sku": "NUT-3"}
    assert client.post(f"{API}/offers", json=body, headers=press()).status_code == 201
    _code(
        client.post(f"{API}/offers", json={**body, "sku": "nut-3"}, headers=press()),
        409,
        "offer_exists",
    )
    nowhere = {**body, "supplier_id": str(uuid.uuid4())}
    _code(client.post(f"{API}/offers", json=nowhere, headers=press()), 404, "supplier_not_found")
    _code(client.get(f"{API}/suppliers/{uuid.uuid4()}/offers"), 404, "supplier_not_found")
    no_item = {**body, "item_id": str(uuid.uuid4())}
    _code(client.post(f"{API}/offers", json=no_item, headers=press()), 404, "item_not_found")
    _code(client.get(f"{API}/items/{uuid.uuid4()}/offers"), 404, "item_not_found")
    missing = uuid.uuid4()
    _code(client.patch(f"{API}/offers/{missing}", json={}, headers=press()), 404, "offer_not_found")
    _code(client.get(f"{API}/offers/{missing}/prices"), 404, "offer_not_found")
    price = client.post(f"{API}/offers/{missing}/prices", json={"price": "1"}, headers=press())
    _code(price, 404, "offer_not_found")


# -- an item's properties ----------------------------------------------------------------


def test_an_items_property_schema_is_its_kinds_merged(client: TestClient) -> None:
    parent = _kind(client, parent_id="part", attribute_schema=MPN_SCHEMA)
    child = _kind(
        client,
        parent_id=parent["id"],
        attribute_schema={"type": "object", "properties": {"length_mm": {"type": "number"}}},
    )
    item = _item(client, child["id"])
    got = client.get(f"/api/v1/entities/item:{item['id']}/property-schema")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["kind"] == "item"
    assert set(body["schema"]["properties"]) == {"mpn", "length_mm"}

    wrong = client.patch(
        f"/api/v1/entities/item:{item['id']}/properties",
        json={"set": {"mpn": 12}},
        headers=press(),
    )
    _code(wrong, 422, "bad_property_value", ENTITY_PROBLEMS)
    unknown_key = client.patch(
        f"/api/v1/entities/item:{item['id']}/properties",
        json={"set": {"finish": "zinc"}},
        headers=press(),
    )
    assert unknown_key.status_code == 200, unknown_key.text

    put = client.put(
        "/api/v1/entity-kinds/item/property-schema",
        json={"schema": {"type": "object", "properties": {}}},
        headers=press(),
    )
    _code(put, 409, "schema_owned_by_item_kind", ENTITY_PROBLEMS)


def test_the_entity_property_schema_serves_other_kinds_and_refuses_a_missing_entity(
    client: TestClient,
) -> None:
    supplier = _supplier(client)
    got = client.get(f"/api/v1/entities/supplier:{supplier}/property-schema")
    assert got.status_code == 200, got.text
    assert got.json()["kind"] == "supplier"
    assert got.json()["schema"]["type"] == "object"
    missing = client.get(f"/api/v1/entities/item:{uuid.uuid4()}/property-schema")
    _code(missing, 404, "entity_not_found", ENTITY_PROBLEMS)
    _code(
        client.get("/api/v1/entities/nope/property-schema"),
        422,
        "bad_entity_ref",
        ENTITY_PROBLEMS,
    )
