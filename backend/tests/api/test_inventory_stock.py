"""Inventory lots, units and moves over HTTP (#7067, spec 6.4 and 6.6): one round trip per
family and the stock problem codes."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_inventory import API, _code, _item
from tests.api.test_settings_runtime import _state
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres


def _location(client: TestClient) -> str:
    with _state(client).refs.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_locations (name, kind_id)"
            " SELECT %s, id FROM filament_location_kinds ORDER BY sort_order LIMIT 1"
            " RETURNING id",
            (f"Shelf {uuid.uuid4().hex[:6]}",),
        ).fetchone()
    assert row is not None
    return str(row["id"])


def _post(client: TestClient, path: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(f"{API}{path}", json=body or {}, headers=press())


def _receive(client: TestClient, item_id: str, **body: Any) -> dict[str, Any]:
    made = _post(client, f"/items/{item_id}/lots", {"qty": "10", **body})
    assert made.status_code == 201, made.text
    return dict(made.json())


def test_a_lot_round_trips_through_use_adjust_and_count(client: TestClient) -> None:
    item = _item(client)
    first = _receive(client, item["id"], unit_cost_minor="5")
    second = _receive(client, item["id"], total_cost_minor="70")
    assert (first["on_hand"], first["unit_cost_minor"], first["cost_unknown"]) == ("10", "5", False)
    assert second["unit_cost_minor"] == "7"

    lots = client.get(f"{API}/items/{item['id']}/lots").json()["lots"]
    assert [lot["id"] for lot in lots] == [first["id"], second["id"]]
    got = client.get(f"{API}/lots/{first['id']}")
    assert got.status_code == 200, got.text

    shelf = _location(client)
    patched = client.patch(
        f"{API}/lots/{second['id']}",
        json={"unit_cost_minor": "8", "location_id": shelf, "note": "box 2"},
        headers=press(),
    )
    assert patched.status_code == 200, patched.text
    assert (patched.json()["unit_cost_minor"], patched.json()["location_id"]) == ("8", shelf)
    assert patched.json()["note"] == "box 2"
    cleared = client.patch(
        f"{API}/lots/{second['id']}", json={"location_id": None}, headers=press()
    )
    assert cleared.json()["location_id"] is None
    assert cleared.json()["unit_cost_minor"] == "8"

    used = _post(client, f"/items/{item['id']}/use", {"qty": "12", "reason": "lid"})
    assert used.status_code == 200, used.text
    taken = [(m["lot_id"], m["qty"], m["unit_cost_minor"]) for m in used.json()["moves"]]
    assert taken == [(first["id"], "-10", "5"), (second["id"], "-2", "8")]

    adjusted = _post(client, f"/lots/{second['id']}/adjust", {"on_hand": "7", "note": "recount"})
    assert adjusted.status_code == 200, adjusted.text
    assert adjusted.json()["on_hand"] == "7"

    counted = _post(client, f"/items/{item['id']}/count", {"on_hand": "4", "note": "stocktake"})
    assert counted.status_code == 200, counted.text
    assert sum(Decimal(m["qty"]) for m in counted.json()["moves"]) == Decimal(-3)
    assert Decimal(client.get(f"{API}/items/{item['id']}").json()["on_hand"]) == 4

    moves = client.get(f"{API}/moves", params={"item_id": item["id"]}).json()["moves"]
    assert [m["kind"] for m in moves][-2:] == ["receive", "receive"]
    assert len(moves) == 6
    on_lot = client.get(f"{API}/moves", params={"lot_id": first["id"], "limit": 1}).json()
    assert len(on_lot["moves"]) == 1


def test_units_round_trip_through_a_status(client: TestClient) -> None:
    item = _item(client, "nozzle", name=f"0.4 hardened {uuid.uuid4().hex[:6]}")
    made = _post(
        client,
        f"/items/{item['id']}/units",
        {"count": 2, "cost_minor": "1500", "serials": ["A1", ""]},
    )
    assert made.status_code == 201, made.text
    units = made.json()["units"]
    assert [u["serial"] for u in units] == ["A1", None]

    listed = client.get(f"{API}/items/{item['id']}/units").json()["units"]
    assert [u["id"] for u in listed] == [u["id"] for u in units]
    one = units[1]["id"]
    assert client.get(f"{API}/units/{one}").json()["status"] == "in_stock"

    shelf = _location(client)
    patched = client.patch(
        f"{API}/units/{one}",
        json={"name": "Spare", "serial": "B2", "location_id": shelf},
        headers=press(),
    )
    assert patched.status_code == 200, patched.text
    assert (patched.json()["name"], patched.json()["serial"]) == ("Spare", "B2")
    assert patched.json()["location_id"] == shelf
    blanked = client.patch(f"{API}/units/{one}", json={"serial": " "}, headers=press())
    assert blanked.json()["serial"] is None

    status = _post(client, f"/units/{one}/status", {"status": "in_service", "note": "X1C"})
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "in_service"
    moves = client.get(f"{API}/moves", params={"unit_id": one}).json()["moves"]
    assert [(m["kind"], m["qty"]) for m in moves] == [("use", "-1"), ("receive", "1")]
    assert client.get(f"{API}/items/{item['id']}").json()["on_hand"] == "1"


def test_stock_refusals(client: TestClient) -> None:
    service = _item(client, "service", name=f"Laser cutting {uuid.uuid4().hex[:6]}")
    _code(_post(client, f"/items/{service['id']}/lots", {"qty": "1"}), 409, "item_not_counted")
    _code(client.get(f"{API}/items/{service['id']}/units"), 409, "item_not_counted")
    _code(_post(client, f"/items/{service['id']}/use", {"qty": "1"}), 409, "item_not_counted")

    bolt = _item(client)
    _code(_post(client, f"/items/{bolt['id']}/units", {"count": 1}), 409, "wrong_tracking")
    _code(_post(client, f"/items/{bolt['id']}/lots", {"qty": "1.5"}), 422, "bad_quantity")
    _code(
        _post(client, f"/items/{bolt['id']}/lots", {"qty": "1", "location_id": str(uuid.uuid4())}),
        404,
        "location_not_found",
    )
    _code(
        _post(client, f"/items/{bolt['id']}/use", {"qty": "1", "lot_id": str(uuid.uuid4())}),
        404,
        "lot_not_found",
    )
    _code(_post(client, f"/items/{uuid.uuid4()}/lots", {"qty": "1"}), 404, "item_not_found")
    missing = uuid.uuid4()
    _code(client.get(f"{API}/lots/{missing}"), 404, "lot_not_found")
    _code(
        client.patch(f"{API}/lots/{missing}", json={"note": "x"}, headers=press()),
        404,
        "lot_not_found",
    )
    _code(
        _post(client, f"/lots/{missing}/adjust", {"on_hand": "1", "note": "x"}),
        404,
        "lot_not_found",
    )
    _code(client.get(f"{API}/units/{missing}"), 404, "unit_not_found")
    _code(_post(client, f"/units/{missing}/status", {"status": "sold"}), 404, "unit_not_found")

    nozzle = _item(client, "nozzle", name=f"0.6 {uuid.uuid4().hex[:6]}")
    units = _post(client, f"/items/{nozzle['id']}/units", {"count": 2, "serials": ["S1", "S2"]})
    first, second = (u["id"] for u in units.json()["units"])
    _code(
        client.patch(f"{API}/units/{second}", json={"serial": "S1"}, headers=press()),
        409,
        "serial_exists",
    )
    _code(_post(client, f"/items/{nozzle['id']}/lots", {"qty": "1"}), 409, "wrong_tracking")
    with _state(client).refs.pool.connection() as conn:
        conn.execute(
            "UPDATE stock_units SET installed_in_unit_id = %s WHERE id = %s", (first, second)
        )
    _code(_post(client, f"/units/{second}/status", {"status": "sold"}), 409, "unit_installed")
