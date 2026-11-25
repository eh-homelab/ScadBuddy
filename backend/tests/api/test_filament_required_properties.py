"""#7703, spec 6.3b: a filament row's own create and save routes take `properties` with the
row, write both or neither, and refuse a row a required key leaves incomplete; every other
write is not checked, and reads mark an incomplete row."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_filament import SPOOLS, TYPES, _material_id
from tests.api.test_filament_purchasing import URL, _confirmable
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


def _new_type(client: TestClient, code: str = "PLA-RED", **extra: Any) -> Any:
    body = {"code": code, "material_id": _material_id(client), "rgba": "#ff0000"}
    return client.post(TYPES, json=body | {"label_weight_g": 1000} | extra)


def test_a_type_create_is_checked_and_takes_its_properties(client: TestClient) -> None:
    _require(client, "spool_type", "mpn")
    _refused(_new_type(client), ["mpn"])
    assert client.get(TYPES).json() == []
    made = _new_type(client, properties={"mpn": "A00-R1"})
    assert made.status_code == 201, made.text
    assert made.json()["missing_properties"] == []
    assert _props(client, f"spool_type:{made.json()['id']}") == {"mpn": "A00-R1"}


def test_a_type_save_is_checked_but_an_archive_and_the_cost_route_are_not(
    client: TestClient,
) -> None:
    made = _new_type(client).json()
    _require(client, "spool_type", "mpn")
    path = f"{TYPES}/{made['id']}"
    assert client.get(path).json()["missing_properties"] == ["mpn"]
    assert client.get(TYPES).json()[0]["missing_properties"] == ["mpn"]
    _refused(client.patch(path, json={"note": "x"}), ["mpn"])
    _refused(client.patch(path, json={"cost_per_kg": "20"}), ["mpn"])
    cost = client.put(f"{path}/cost-per-kg", json={"cost_per_kg": "20"})
    assert cost.status_code == 200, cost.text
    assert cost.json()["cost_per_kg"] == "20.0000"
    assert cost.json()["missing_properties"] == ["mpn"]
    assert client.patch(path, json={"archived": True}).status_code == 200
    assert client.patch(path, json={"archived": False}).status_code == 200
    saved = client.patch(path, json={"note": "x", "properties": {"mpn": "M"}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["missing_properties"] == []


def test_the_cost_route_answers_404_for_no_type(client: TestClient) -> None:
    gone = "00000000-0000-0000-0000-000000000000"
    assert client.put(f"{TYPES}/{gone}/cost-per-kg", json={"cost_per_kg": "1"}).status_code == 404


def test_an_invoice_confirm_is_not_checked(client: TestClient) -> None:
    invoice_id = _confirmable(client)
    _require(client, "spool", "lot_number")
    _require(client, "spool_type", "mpn")
    done = client.post(f"{URL}/invoices/{invoice_id}/confirm")
    assert done.status_code == 200, done.text
    spool_id = done.json()["lines"][0]["spool_ids"][0]
    assert client.get(f"{SPOOLS}/{spool_id}").json()["missing_properties"] == ["lot_number"]


CATALOG = "/api/v1/filament/catalog"


def test_a_catalog_row_create_is_checked_and_takes_its_properties(client: TestClient) -> None:
    _require(client, "insert_type", "part_number")
    hub = {"name": "Hub", "empty_weight_g": 200}
    _refused(client.post(f"{CATALOG}/insert-types", json=hub), ["part_number"])
    names = [r["name"] for r in client.get(f"{CATALOG}/insert-types").json()]
    assert "Hub" not in names
    made = client.post(f"{CATALOG}/insert-types", json=hub | {"properties": {"part_number": "P1"}})
    assert made.status_code == 201, made.text
    assert made.json()["missing_properties"] == []
    assert _props(client, f"insert_type:{made.json()['id']}") == {"part_number": "P1"}


def test_a_catalog_save_is_checked_but_a_re_order_and_an_archive_are_not(
    client: TestClient,
) -> None:
    made = client.post(f"{CATALOG}/brands", json={"name": "Acme"}).json()
    _require(client, "brand", "site")
    path = f"{CATALOG}/brands/{made['id']}"
    listed = {r["id"]: r for r in client.get(f"{CATALOG}/brands").json()}
    assert listed[made["id"]]["missing_properties"] == ["site"]
    _refused(client.patch(path, json={"name": "Acme 2"}), ["site"])
    assert client.patch(path, json={"sort_order": 3}).status_code == 200
    assert client.patch(path, json={"archived": True}).status_code == 200
    assert client.patch(path, json={"sort_order": 4, "archived": False}).status_code == 200
    saved = client.patch(path, json={"name": "Acme 2", "properties": {"site": "acme.test"}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["missing_properties"] == []


def test_an_invoice_line_proposed_type_takes_no_properties(client: TestClient) -> None:
    supplier = client.post(f"{URL}/suppliers", json={"name": "Acme"}).json()["id"]
    proposed = {
        "code": "PLA-WHT",
        "material_id": _material_id(client),
        "rgba": "#FFFFFF",
        "label_weight_g": 1000,
        "properties": {"mpn": "M1"},
    }
    line = {"description": "x", "quantity": 1, "unit_price": "40.00", "proposed_type": proposed}
    body = {
        "supplier_id": supplier,
        "invoice_date": "2026-11-01",
        "currency": "USD",
        "subtotal": "40.00",
        "shipping": "0",
        "tax": "0",
        "total": "40.00",
        "lines": [line],
    }
    refused = client.post(f"{URL}/invoices", json=body)
    assert refused.status_code == 422, refused.text
    assert "properties" in refused.text
