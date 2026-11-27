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
    props = client.patch(
        f"/api/v1/entities/item:{item['id']}/properties",
        json={"set": {"mpn": "X1", "colour": "red"}},
        headers=press(),
    )
    assert props.status_code == 200, props.text
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
    builtin = client.patch(f"{API}/kinds/part", json={"tracking": "lot"}, headers=press())
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
    assert item["kind_path"] == ["durable", "part", kind["id"]]
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

    other = _item(client)
    renamed = client.patch(
        f"{API}/items/{other['id']}", json={"name": item["name"].lower()}, headers=press()
    )
    _code(renamed, 409, "item_exists")


def test_restoring_an_item_is_refused_under_an_archived_kind_or_a_taken_name(
    client: TestClient,
) -> None:
    kind = _kind(client)
    item = _item(client, kind["id"])
    client.post(f"{API}/items/{item['id']}/archive", headers=press())
    client.post(f"{API}/kinds/{kind['id']}/archive", headers=press())
    _code(client.post(f"{API}/items/{item['id']}/restore", headers=press()), 409, "kind_archived")
    client.post(f"{API}/kinds/{kind['id']}/restore", headers=press())
    _item(client, kind["id"], name=item["name"])
    _code(client.post(f"{API}/items/{item['id']}/restore", headers=press()), 409, "item_exists")


def test_the_item_list_filters_low_and_compatible_and_takes_a_limit(client: TestClient) -> None:
    kind = _kind(client)
    model = f"X{uuid.uuid4().hex[:6]}"
    low = _item(client, kind["id"], name="a low", low_stock="2", compatible_with=[model])
    _item(client, kind["id"], name="b stocked")

    def ids(**params: Any) -> list[str]:
        got = client.get(f"{API}/items", params={"kind": kind["id"], **params})
        assert got.status_code == 200, got.text
        return [i["id"] for i in got.json()["items"]]

    assert ids(low=True) == [low["id"]]
    assert ids(compatible=model) == [low["id"]]
    assert ids(compatible="nope") == []
    assert ids(limit=1) == [low["id"]]
    assert len(ids(limit=5000)) == 2
    for limit in (0, 5001):
        bad = client.get(f"{API}/items", params={"limit": limit})
        assert bad.status_code == 422, bad.text


def test_an_items_figures_are_normalized(client: TestClient) -> None:
    item = _item(client, low_stock="4.5", life_hours="100")
    assert (item["low_stock"], item["life_hours"], item["on_hand"]) == ("4.5", "100", "0")
    assert item["stock_value_minor"] == "0"


def test_nul_bytes_are_refused_in_paths_queries_and_bodies(client: TestClient) -> None:
    for got in (
        client.get(f"{API}/kinds/a%00b/schema"),
        client.patch(f"{API}/kinds/a%00b", json={}, headers=press()),
        client.post(f"{API}/kinds/a%00b/archive", headers=press()),
        client.get(f"{API}/items", params={"kind": "a\x00b"}),
        client.get(f"{API}/items", params={"q": "a\x00b"}),
        client.get(f"{API}/items", params={"compatible": "a\x00b"}),
    ):
        assert got.status_code == 422, got.text
    kind = _kind(client)
    for field, value in (
        ("name", "M3\x00"),
        ("manufacturer", "Ruthex\x00"),
        ("note", "\x00"),
        ("compatible_with", ["X1\x00"]),
        ("source", "csv\x00"),
        ("source_ref", "r\x00"),
    ):
        made = client.post(
            f"{API}/items", json={"kind_id": kind["id"], "name": "N", field: value}, headers=press()
        )
        assert made.status_code == 422, (field, made.text)
    named = client.post(
        f"{API}/kinds",
        json={"id": _kind_id(), "name": "K\x00", "tracking": "lot"},
        headers=press(),
    )
    assert named.status_code == 422, named.text
    item = _item(client, kind["id"])
    patched = client.patch(f"{API}/items/{item['id']}", json={"note": "\x00"}, headers=press())
    assert patched.status_code == 422, patched.text
    supplier = _supplier(client)
    for field, value in (("sku", "S\x00"), ("pack_uom", "box\x00"), ("note", "\x00")):
        offer = client.post(
            f"{API}/offers",
            json={"supplier_id": supplier, "item_id": item["id"], "sku": "S1", field: value},
            headers=press(),
        )
        assert offer.status_code == 422, (field, offer.text)


def test_item_and_offer_texts_and_life_months_are_capped(client: TestClient) -> None:
    kind = _kind(client)
    for field, value in (
        ("life_months", 10**10),
        ("life_months", 1201),
        ("manufacturer", "m" * 201),
        ("compatible_with", ["c" * 101]),
        ("compatible_with", [f"c{n}" for n in range(101)]),
        ("source", "s" * 101),
        ("source_ref", "r" * 201),
    ):
        made = client.post(
            f"{API}/items", json={"kind_id": kind["id"], "name": "N", field: value}, headers=press()
        )
        assert made.status_code == 422, (field, made.text)
    item = _item(client, kind["id"], life_months=1200)
    patched = client.patch(
        f"{API}/items/{item['id']}", json={"life_months": 10**10}, headers=press()
    )
    assert patched.status_code == 422, patched.text
    supplier = _supplier(client)
    for field, value in (("pack_uom", "u" * 101), ("note", "n" * 4001)):
        offer = client.post(
            f"{API}/offers",
            json={"supplier_id": supplier, "item_id": item["id"], "sku": "S1", field: value},
            headers=press(),
        )
        assert offer.status_code == 422, (field, offer.text)


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
    # R-P2-40: decimals are normalized, as an item's are.
    assert offer["pack_size"] == "100"

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
    assert price.json()["price"] == "4.99"
    listed = client.get(f"{API}/items/{item['id']}/offers").json()["offers"][0]
    assert (listed["pack_size"], listed["latest_price"]["price"]) == ("100", "4.99")
    prices = client.get(f"{API}/offers/{offer['id']}/prices").json()["prices"]
    assert [p["id"] for p in prices] == [price.json()["id"]]
    assert (
        client.get(f"{API}/items/{item['id']}/offers").json()["offers"][0]["latest_price"]["id"]
        == price.json()["id"]
    )

    archived = client.post(f"{API}/offers/{offer['id']}/archive", headers=press())
    assert archived.status_code == 200, archived.text
    assert client.get(f"{API}/items/{item['id']}/offers").json()["offers"] == []


def test_an_item_counts_its_unarchived_offers(client: TestClient) -> None:
    """R-P2-37: `offer_count` comes with the item, on the list and on one item."""
    item = _item(client)
    bare = _item(client)
    supplier, other = _supplier(client), _supplier(client)
    for shop, sku in ((supplier, "A-1"), (other, "B-1"), (supplier, "A-2")):
        made = client.post(
            f"{API}/offers",
            json={"supplier_id": shop, "item_id": item["id"], "sku": sku},
            headers=press(),
        )
        assert made.status_code == 201, made.text
    assert (
        client.post(f"{API}/offers/{made.json()['id']}/archive", headers=press()).status_code == 200
    )

    listed = {
        i["id"]: i for i in client.get(f"{API}/items", params={"kind": "hardware"}).json()["items"]
    }
    assert listed[item["id"]]["offer_count"] == 2
    assert listed[bare["id"]]["offer_count"] == 0
    assert client.get(f"{API}/items/{item['id']}").json()["offer_count"] == 2
    assert bare["offer_count"] == 0


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
    unknown = uuid.uuid4()
    listed = client.get(f"{API}/suppliers/{unknown}/offers")
    _code(listed, 404, "supplier_not_found")
    assert listed.json()["detail"] == f"no supplier {str(unknown)!r}"
    no_item = {**body, "item_id": str(uuid.uuid4())}
    _code(client.post(f"{API}/offers", json=no_item, headers=press()), 404, "item_not_found")
    _code(client.get(f"{API}/items/{uuid.uuid4()}/offers"), 404, "item_not_found")
    missing = uuid.uuid4()
    _code(client.patch(f"{API}/offers/{missing}", json={}, headers=press()), 404, "offer_not_found")
    _code(client.get(f"{API}/offers/{missing}/prices"), 404, "offer_not_found")
    price = client.post(f"{API}/offers/{missing}/prices", json={"price": "1"}, headers=press())
    _code(price, 404, "offer_not_found")

    other = client.post(f"{API}/offers", json={**body, "sku": "NUT-4"}, headers=press())
    clash = client.patch(
        f"{API}/offers/{other.json()['id']}", json={"sku": "nut-3"}, headers=press()
    )
    _code(clash, 409, "offer_exists")

    with _state(client).refs.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO items (kind_id, name) VALUES ('filament', %s) RETURNING id",
            (f"PLA {uuid.uuid4().hex[:6]}",),
        ).fetchone()
    assert row is not None
    filament = client.post(
        f"{API}/offers", json={**body, "item_id": str(row["id"])}, headers=press()
    )
    _code(filament, 409, "filament_item_refused")


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
