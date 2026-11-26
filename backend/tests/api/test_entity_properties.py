"""Properties on any entity (#7026, spec 6.3a): `/entities/{kind}:{ref}/properties` and
`/entity-kinds/{kind}/…`."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient

from tests.api.test_settings_runtime import _state
from tests.support.operations import press

PROBLEMS = "https://scadbuddy.dev/problems/entities/"
SCHEMA = "/api/v1/entity-kinds/spool_type/property-schema"


def _spool_type(client: TestClient) -> str:
    with _state(client).refs.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g)"
            " SELECT %s, id, '#FFFFFFFF', 1000 FROM filament_materials WHERE name = 'PLA'"
            " RETURNING id",
            (f"T{uuid.uuid4().hex[:10]}",),
        ).fetchone()
    assert row is not None
    return f"spool_type:{row['id']}"


def _code(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["type"] == PROBLEMS + code


def _patch(client: TestClient, entity: str, values: dict[str, Any]) -> Any:
    return client.patch(
        f"/api/v1/entities/{entity}/properties", json={"set": values}, headers=press()
    )


def test_a_patch_round_trips_and_a_rename_moves_the_value(client: TestClient) -> None:
    entity = _spool_type(client)
    patched = _patch(client, entity, {"dry_temp": 55, "tags": ["a", "b"]})
    assert patched.status_code == 200, patched.text
    listed = client.get(f"/api/v1/entities/{entity}/properties").json()["properties"]
    assert {p["key"]: p["value"] for p in listed} == {"dry_temp": 55, "tags": ["a", "b"]}
    assert {p["source"] for p in listed} == {"manual"}
    renamed = _patch(client, entity, {"drying_c": 55, "dry_temp": None})
    assert {p["key"]: p["value"] for p in renamed.json()["properties"]} == {
        "drying_c": 55,
        "tags": ["a", "b"],
    }


def test_a_bad_value_names_its_key(client: TestClient) -> None:
    entity = _spool_type(client)
    bad = _patch(client, entity, {"Bad Key": 1})
    _code(bad, 422, "bad_property_value")
    assert "Bad Key" in bad.json()["detail"]
    obj = _patch(client, entity, {"obj": {"a": 1}})
    _code(obj, 422, "bad_property_value")
    assert "obj" in obj.json()["detail"]
    nul = _patch(client, entity, {"lot_number": "a\u0000b"})
    _code(nul, 422, "bad_property_value")
    assert "lot_number" in nul.json()["detail"]


def test_a_missing_entity_and_kinds_that_take_none(client: TestClient) -> None:
    _code(_patch(client, f"spool_type:{uuid.uuid4()}", {"a": 1}), 404, "entity_not_found")
    missing = client.get(f"/api/v1/entities/spool_type:{uuid.uuid4()}/properties")
    _code(missing, 404, "entity_not_found")
    archive = _patch(client, "print_archive:1", {"a": 1})
    _code(archive, 422, "unknown_entity_kind")
    assert "takes properties" in archive.json()["detail"]
    _code(client.get("/api/v1/entity-kinds/nope/properties"), 422, "unknown_entity_kind")
    _code(client.get("/api/v1/entity-kinds/nope/property-schema"), 422, "unknown_entity_kind")
    _code(client.get("/api/v1/entities/nope/properties"), 422, "bad_entity_ref")


def test_the_kind_read_returns_every_entitys_map(client: TestClient) -> None:
    one, two = _spool_type(client), _spool_type(client)
    _patch(client, one, {"a": 1})
    _patch(client, two, {"b": "x"})
    found = client.get("/api/v1/entity-kinds/spool_type/properties").json()["properties"]
    assert found[one.split(":")[1]] == {"a": 1}
    assert found[two.split(":")[1]] == {"b": "x"}


def test_a_patch_that_only_removes_on_a_bare_entity_is_empty(client: TestClient) -> None:
    entity = _spool_type(client)
    removed = _patch(client, entity, {"td": None, "mpn": None})
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"properties": []}
    found = client.get("/api/v1/entity-kinds/spool_type/properties").json()["properties"]
    assert entity.split(":")[1] not in found


def test_a_kind_with_no_properties_reads_empty(client: TestClient) -> None:
    got = client.get("/api/v1/entity-kinds/supplier/properties")
    assert got.status_code == 200, got.text
    assert got.json() == {"properties": {}}


def test_a_malformed_patch_body_is_the_generic_422(client: TestClient) -> None:
    entity = _spool_type(client)
    path = f"/api/v1/entities/{entity}/properties"
    bodies: list[dict[str, Any]] = [{"set": {}}, {"set": {"a": 1}, "extra": 1}]
    for body in bodies:
        refused = client.patch(path, json=body, headers=press())
        assert refused.status_code == 422, refused.text
        assert not refused.json().get("type", "").endswith("bad_property_value")


def test_a_restricting_schema_is_refused_and_the_old_one_stays(client: TestClient) -> None:
    seeded = client.get(SCHEMA).json()
    assert seeded["kind"] == "spool_type"
    bad = client.put(
        SCHEMA, json={"schema": {"type": "object", "required": ["x"]}}, headers=press()
    )
    _code(bad, 422, "bad_property_schema")
    assert client.get(SCHEMA).json()["schema"] == seeded["schema"]


def test_a_good_schema_types_a_key_and_counts_keys_in_use(client: TestClient) -> None:
    good = {"type": "object", "properties": {"dry_temp": {"type": "number"}}}
    put = client.put(SCHEMA, json={"schema": good}, headers=press())
    assert put.status_code == 200, put.text
    assert put.json()["schema"] == good
    entity = _spool_type(client)
    _code(_patch(client, entity, {"dry_temp": "hot"}), 422, "bad_property_value")
    assert _patch(client, entity, {"dry_temp": 60}).status_code == 200
    keys = client.get(SCHEMA).json()["keys_in_use"]
    mine = [k for k in keys if k["key"] == "dry_temp"]
    assert len(mine) == 1 and mine[0]["count"] >= 1
    other = client.put(
        "/api/v1/entity-kinds/Spool/property-schema", json={"schema": good}, headers=press()
    )
    _code(other, 422, "unknown_entity_kind")


def test_an_items_schema_is_set_on_its_kind(client: TestClient) -> None:
    refused = client.put(
        "/api/v1/entity-kinds/item/property-schema",
        json={"schema": {"type": "object", "properties": {}}},
        headers=press(),
    )
    _code(refused, 409, "schema_owned_by_item_kind")
    assert "attribute_schema" in refused.json()["detail"]
    read = client.get("/api/v1/entity-kinds/item/property-schema")
    _code(read, 409, "schema_owned_by_item_kind")


def test_a_patch_that_empties_a_required_value_is_refused(client: TestClient) -> None:
    schema = {"type": "object", "properties": {"mpn": {"type": "string"}}, "required": ["mpn"]}
    assert client.put(SCHEMA, json={"schema": schema}, headers=press()).status_code == 200
    entity = _spool_type(client)
    assert _patch(client, entity, {"other": "x"}).status_code == 200
    assert _patch(client, entity, {"mpn": "A1"}).status_code == 200
    refused = _patch(client, entity, {"mpn": None})
    _code(refused, 422, "missing_required_properties")
    assert refused.json()["missing"] == ["mpn"]
    assert refused.json()["invalid"] == []


def test_required_is_refused_on_a_kind_that_does_not_check(client: TestClient) -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    put = client.put(
        "/api/v1/entity-kinds/supplier/property-schema", json={"schema": schema}, headers=press()
    )
    _code(put, 422, "bad_property_schema")
