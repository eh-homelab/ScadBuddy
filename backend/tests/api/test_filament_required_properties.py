"""#7703, spec 6.3b: a filament row's own create and save routes take `properties` with the
row, write both or neither, and refuse a row a required key leaves incomplete; every other
write is not checked, and reads mark an incomplete row."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_filament import SPOOLS, _material_id
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

PROBLEM = "https://scadbuddy.dev/problems/entities/missing_required_properties"


def _require(client: TestClient, kind: str, key: str, sub: dict[str, Any] | None = None) -> None:
    schema = {"type": "object", "properties": {key: sub or {"type": "string"}}, "required": [key]}
    put = client.put(
        f"/api/v1/entity-kinds/{kind}/property-schema", json={"schema": schema}, headers=press()
    )
    assert put.status_code == 200, put.text


def _refused(response: Any, missing: list[str]) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["type"] == PROBLEM
    assert response.json()["missing"] == missing


def _props(client: TestClient, entity: str) -> dict[str, Any]:
    listed = client.get(f"/api/v1/entities/{entity}/properties").json()["properties"]
    return {p["key"]: p["value"] for p in listed}


def _new_spool(client: TestClient, **extra: Any) -> Any:
    return client.post(SPOOLS, json={"material_id": _material_id(client), **extra})


def test_a_spool_create_without_a_required_key_is_refused_and_writes_nothing(
    client: TestClient,
) -> None:
    _require(client, "spool", "lot_number")
    _refused(_new_spool(client), ["lot_number"])
    _refused(_new_spool(client, properties={"lot_number": "  "}), ["lot_number"])
    assert client.get(SPOOLS).json() == []


def test_a_spool_create_with_its_properties_is_one_write(client: TestClient) -> None:
    _require(client, "spool", "lot_number")
    made = _new_spool(client, properties={"lot_number": "L1", "free": True})
    assert made.status_code == 201, made.text
    assert made.json()["missing_properties"] == []
    assert _props(client, f"spool:{made.json()['id']}") == {"lot_number": "L1", "free": True}


def test_a_bulk_create_without_a_required_key_writes_no_spool(client: TestClient) -> None:
    _require(client, "spool", "lot_number")
    body = {"spool": {"material_id": _material_id(client)}, "quantity": 3}
    _refused(
        client.post(f"{SPOOLS}/bulk", json=body, headers={"Idempotency-Key": "a"}), ["lot_number"]
    )
    assert client.get(SPOOLS).json() == []
    body["spool"]["properties"] = {"lot_number": "L2"}  # type: ignore[index]
    made = client.post(f"{SPOOLS}/bulk", json=body, headers={"Idempotency-Key": "b"})
    assert made.status_code == 201, made.text
    assert {_props(client, f"spool:{s['id']}")["lot_number"] for s in made.json()} == {"L2"}


def test_a_spool_save_is_checked_but_an_archive_is_not(client: TestClient) -> None:
    spool = _new_spool(client).json()  # made before the key was required: incomplete
    _require(client, "spool", "lot_number")
    path = f"{SPOOLS}/{spool['id']}"
    _refused(client.patch(path, json={"note": "x"}), ["lot_number"])
    assert client.patch(path, json={"archived": True}).status_code == 200
    assert client.patch(path, json={"archived": False}).status_code == 200
    saved = client.patch(path, json={"note": "x", "properties": {"lot_number": "L3"}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["note"] == "x"
    assert client.patch(path, json={"note": "y"}).status_code == 200


def test_writes_outside_the_form_are_not_checked(client: TestClient) -> None:
    spool = _new_spool(client).json()
    _require(client, "spool", "lot_number")
    bulk = client.post(
        f"{SPOOLS}/bulk-update", json={"spool_ids": [spool["id"]], "patch": {"note": "b"}}
    )
    assert bulk.status_code == 200, bulk.text
    event = client.post(f"{SPOOLS}/{spool['id']}/events", json={"kind": "weighed", "grams": 500})
    assert event.status_code in (200, 201), event.text


def test_a_list_marks_an_incomplete_spool_but_not_an_archived_one(client: TestClient) -> None:
    kept, gone = _new_spool(client).json(), _new_spool(client).json()
    client.patch(f"{SPOOLS}/{gone['id']}", json={"archived": True})
    _require(client, "spool", "lot_number")
    found = {s["id"]: s for s in client.get(f"{SPOOLS}?include_archived=true").json()}
    assert found[kept["id"]]["missing_properties"] == ["lot_number"]
    assert found[gone["id"]]["missing_properties"] == []
    assert client.get(f"{SPOOLS}/{kept['id']}").json()["missing_properties"] == ["lot_number"]


def test_a_stored_value_that_no_longer_fits_is_reported_and_blocks_a_save(
    client: TestClient,
) -> None:
    _require(client, "spool", "lot_number")
    spool = _new_spool(client, properties={"lot_number": "L1"}).json()
    _require(client, "spool", "lot_number", {"type": "string", "maxLength": 1})
    read = client.get(f"{SPOOLS}/{spool['id']}").json()
    assert read["invalid_properties"] == ["lot_number"]
    refused = client.patch(f"{SPOOLS}/{spool['id']}", json={"note": "x"})
    assert refused.status_code == 422, refused.text
    assert [i["key"] for i in refused.json()["invalid"]] == ["lot_number"]
