"""Inventory lots, units and moves over HTTP (#7067, spec 6.4 and 6.6): one round trip per
family and the stock problem codes."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.component import ORDERS
from tests.api.test_inventory import API, ENTITY_PROBLEMS, _code, _item
from tests.api.test_orders_lifecycle import fulfil
from tests.api.test_orders_unfulfil import accepted_order
from tests.api.test_settings_runtime import _state
from tests.api.test_versions import SLUG
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
    stray = _post(client, f"/items/{bolt['id']}/use", {"qty": "1", "lot_id": str(uuid.uuid4())})
    _code(stray, 404, "lot_not_found")
    assert stray.json()["detail"].startswith("no lot: ")
    _code(_post(client, f"/items/{uuid.uuid4()}/lots", {"qty": "1"}), 404, "item_not_found")
    missing = uuid.uuid4()
    _code(client.get(f"{API}/lots/{missing}"), 404, "lot_not_found")
    assert client.get(f"{API}/lots/{missing}").json()["detail"] == f"no lot {str(missing)!r}"
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


def _supplier_ref(client: TestClient) -> str:
    with _state(client).refs.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_suppliers (name) VALUES (%s) RETURNING id",
            (f"Bolt Depot {uuid.uuid4()}",),
        ).fetchone()
    assert row is not None
    return f"supplier:{row['id']}"


def test_use_and_status_name_an_entity(client: TestClient) -> None:
    ref = _supplier_ref(client)
    bolt = _item(client)
    _receive(client, bolt["id"])
    used = _post(client, f"/items/{bolt['id']}/use", {"qty": "1", "entity": ref})
    assert used.status_code == 200, used.text
    assert used.json()["moves"][0]["entity_id"] is not None

    nozzle = _item(client, "nozzle", name=f"0.2 {uuid.uuid4().hex[:6]}")
    unit = _post(client, f"/items/{nozzle['id']}/units", {"count": 1}).json()["units"][0]["id"]
    status = _post(client, f"/units/{unit}/status", {"status": "sold", "entity": ref})
    assert status.status_code == 200, status.text
    moves = client.get(f"{API}/moves", params={"unit_id": unit}).json()["moves"]
    assert moves[0]["entity_id"] == used.json()["moves"][0]["entity_id"]

    _code(
        _post(client, f"/items/{bolt['id']}/use", {"qty": "1", "entity": "nope"}),
        422,
        "bad_entity_ref",
        ENTITY_PROBLEMS,
    )
    _code(
        _post(client, f"/units/{unit}/status", {"status": "in_stock", "entity": "nope"}),
        422,
        "bad_entity_ref",
        ENTITY_PROBLEMS,
    )
    for entity in ("order:\x00", "supplier:\x00"):
        used_on = _post(client, f"/items/{bolt['id']}/use", {"qty": "1", "entity": entity})
        assert used_on.status_code == 422, used_on.text
        went = _post(client, f"/units/{unit}/status", {"status": "in_stock", "entity": entity})
        assert went.status_code == 422, went.text
    gone = f"supplier:{uuid.uuid4()}"
    _code(
        _post(client, f"/items/{bolt['id']}/use", {"qty": "1", "entity": gone}),
        404,
        "entity_not_found",
        ENTITY_PROBLEMS,
    )
    _code(
        _post(client, f"/units/{unit}/status", {"status": "in_stock", "entity": gone}),
        404,
        "entity_not_found",
        ENTITY_PROBLEMS,
    )
    long = _post(
        client, f"/items/{bolt['id']}/use", {"qty": "1", "entity": "supplier:" + "x" * 200}
    )
    assert long.status_code == 422, long.text


def test_stock_texts_with_a_nul_are_refused(client: TestClient) -> None:
    bolt = _item(client)
    lot = _receive(client, bolt["id"])
    for path, body in (
        (f"/items/{bolt['id']}/lots", {"qty": "1", "note": "\x00"}),
        (f"/items/{bolt['id']}/use", {"qty": "1", "reason": "\x00"}),
        (f"/items/{bolt['id']}/count", {"on_hand": "1", "note": "a\x00"}),
        (f"/lots/{lot['id']}/adjust", {"on_hand": "1", "note": "a\x00"}),
    ):
        got = _post(client, path, body)
        assert got.status_code == 422, (path, got.text)
    patched = client.patch(f"{API}/lots/{lot['id']}", json={"note": "\x00"}, headers=press())
    assert patched.status_code == 422, patched.text
    nozzle = _item(client, "nozzle", name=f"0.8 {uuid.uuid4().hex[:6]}")
    for units in ({"count": 1, "name": "\x00"}, {"count": 1, "serials": ["S\x00"]}):
        got = _post(client, f"/items/{nozzle['id']}/units", units)
        assert got.status_code == 422, got.text
    unit = _post(client, f"/items/{nozzle['id']}/units", {"count": 1}).json()["units"][0]["id"]
    for body in ({"name": "\x00"}, {"serial": "\x00"}):
        got = client.patch(f"{API}/units/{unit}", json=body, headers=press())
        assert got.status_code == 422, got.text
    status = _post(client, f"/units/{unit}/status", {"status": "sold", "note": "\x00"})
    assert status.status_code == 422, status.text
    moves = client.get(f"{API}/moves", params={"order_item_id": "a\x00"})
    assert moves.status_code == 422, moves.text


def test_an_amount_past_the_column_is_bad_quantity(client: TestClient) -> None:
    bolt = _item(client)
    huge = "10000000000000"
    _code(
        _post(client, f"/items/{bolt['id']}/lots", {"qty": huge, "unit_cost_minor": huge}),
        422,
        "bad_quantity",
    )
    lot = _receive(client, bolt["id"], qty=huge)
    _code(
        client.patch(f"{API}/lots/{lot['id']}", json={"unit_cost_minor": huge}, headers=press()),
        422,
        "bad_quantity",
    )
    most = "99999999999999"
    short = _item(client)
    assert _post(client, f"/items/{short['id']}/use", {"qty": most}).status_code == 200
    _code(
        _post(client, f"/items/{short['id']}/count", {"on_hand": most, "note": "found"}),
        422,
        "bad_quantity",
    )
    nozzle = _item(client, "nozzle", name=f"1.0 {uuid.uuid4().hex[:6]}")
    ok = _post(client, f"/items/{nozzle['id']}/units", {"count": 1, "cost_minor": huge})
    assert ok.status_code == 201, ok.text


def test_missing_units_and_move_limits(client: TestClient) -> None:
    _code(
        client.patch(f"{API}/units/{uuid.uuid4()}", json={"name": "x"}, headers=press()),
        404,
        "unit_not_found",
    )
    for limit in (0, 1001):
        got = client.get(f"{API}/moves", params={"limit": limit})
        assert got.status_code == 422, got.text
    assert client.get(f"{API}/moves", params={"limit": 1000}).status_code == 200


def test_a_use_of_a_named_empty_lot_is_lot_empty(client: TestClient) -> None:
    bolt = _item(client)
    lot = _receive(client, bolt["id"], qty="1")
    _receive(client, bolt["id"])
    assert _post(client, f"/items/{bolt['id']}/use", {"qty": "1"}).status_code == 200
    _code(
        _post(client, f"/items/{bolt['id']}/use", {"qty": "1", "lot_id": lot["id"]}),
        409,
        "lot_empty",
    )


# -- an order's parts are items (#7067 Task 7) ---------------------------------------------


@pytest.mark.requires_git
def test_an_orders_parts_take_from_lots_and_recost_writes_the_order(client: TestClient) -> None:
    hook = _item(client)
    lot = _receive(client, hook["id"], unit_cost_minor="5")
    order_id, [item_id] = accepted_order(client)
    path = f"/api/v1/orders/{order_id}/items/{item_id}/parts"

    # The old body's `part_id` key still works for one release.
    done = client.put(path, json={"parts": [{"part_id": hook["id"], "per_unit": 2}]})
    assert done.status_code == 200, done.text
    assert done.json() == [{"item_id": hook["id"], "part_id": hook["id"], "per_unit": 2, "used": 0}]
    twice = {"parts": [{"item_id": hook["id"], "per_unit": 1}] * 2}
    assert client.put(path, json=twice).status_code == 422
    for missing in ("nope", str(uuid.uuid4())):
        unknown = client.put(path, json={"parts": [{"item_id": missing, "per_unit": 1}]})
        assert unknown.status_code == 409, unknown.text
        assert unknown.json()["type"].endswith("/part_not_found")

    fulfil(client, order_id, item_id, 1)
    [use] = client.get(f"{API}/moves", params={"item_id": hook["id"]}).json()["moves"][:1]
    assert (use["kind"], use["qty"], use["order_item_id"]) == ("use", "-2", item_id)
    # The quote's parts: a FIFO take of 2 from what is left, at 5 each.
    history = client.get(
        "/api/v1/orders/history/unit-cost", params={"slug": SLUG, "item_id": item_id}
    ).json()
    assert (history["jobs"], history["parts_unit_minor"]) == (1, 10)

    client.patch(f"{API}/lots/{lot['id']}", json={"unit_cost_minor": "6"}, headers=press())
    recosted = _post(client, f"/lots/{lot['id']}/recost")
    assert recosted.status_code == 200, recosted.text
    assert recosted.json()["moves"] == 1
    assert [
        (o["order_id"], o["order_item_id"], o["before_minor"], o["after_minor"])
        for o in recosted.json()["orders"]
    ] == [(order_id, item_id, "10", "12")]
    events = client.get(f"/api/v1/orders/{order_id}/events").json()
    [event] = [e for e in events if e["kind"] == "item.parts_recosted"]
    assert event["detail"]["items"][0]["after_minor"] == "12"

    _code(_post(client, f"/lots/{uuid.uuid4()}/recost"), 404, "lot_not_found")


@pytest.mark.requires_git
def test_a_parts_estimate_past_an_amount_is_a_422(client: TestClient) -> None:
    """R-P2-16: the unit-cost route's parts estimate never answers a 500."""
    hook = _item(client)
    _receive(client, hook["id"], qty="1", unit_cost_minor="90000000000000")
    order_id, [item_id] = accepted_order(client)
    path = f"/api/v1/orders/{order_id}/items/{item_id}/parts"
    assert client.put(path, json={"parts": [{"item_id": hook["id"], "per_unit": 1000}]}).is_success
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        conn.execute("UPDATE order_items SET quantity = 2000000 WHERE id = %s", (item_id,))

    got = client.get("/api/v1/orders/history/unit-cost", params={"slug": SLUG, "item_id": item_id})

    assert got.status_code == 422, got.text
