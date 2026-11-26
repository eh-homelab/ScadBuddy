"""Inventory lots, units and moves over HTTP (#7067, spec 6.4 and 6.6; #6949 K1). Since K1
a read goes to Go's CRUD route and a write starts its activity on the queue `inventory`
(R-K1-4, R-K1-24); Go and Temporal are fakes here (R-K1-21), and Go's tests hold the
stock rules. "Re-cost past uses" stays the orders' lifecycle until Task 16."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from scadbuddy.inventory.component import INVENTORY, InventoryComponent
from scadbuddy.inventory.models import LotPatch, ReceiveLot
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.models import Actor
from tests.api.test_inventory import API, PROBLEMS, _code, _item, answer, done
from tests.api.test_orders_lifecycle import fulfil
from tests.api.test_orders_unfulfil import accepted_order
from tests.api.test_settings_runtime import _state
from tests.api.test_versions import SLUG
from tests.support.inventory_core import Go, install_go
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

ITEM = "00000000-0000-4000-8000-000000000002"
LOT = "00000000-0000-4000-8000-000000000001"
UNIT = "00000000-0000-4000-8000-000000000003"


@pytest.fixture
def go(client: TestClient) -> Go:
    return install_go(client)


def _post(client: TestClient, path: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(f"{API}{path}", json=body or {}, headers=press())


def _inventory(client: TestClient) -> InventoryComponent:
    return _state(client).components.get(INVENTORY)


def _receive(client: TestClient, item_id: str, **body: Any) -> dict[str, Any]:
    """A lot as Python's stock store still writes it for the orders (until Task 16)."""
    lot = _inventory(client).stock.receive_lot(
        UUID(item_id), ReceiveLot.model_validate({"qty": "10", **body}), Actor(kind="browser")
    )
    return {"id": str(lot.id)}


# -- lots --------------------------------------------------------------------------------


def test_lot_reads_go_to_go(client: TestClient, go: Go) -> None:
    go.reads[f"/items/{ITEM}/lots"] = (200, answer("LotList"))
    go.reads[f"/lots/{LOT}"] = (200, answer("Lot"))

    assert client.get(f"{API}/items/{ITEM}/lots").json() == answer("LotList")
    assert client.get(f"{API}/lots/{LOT}").json() == answer("Lot")


def test_lot_writes_start_their_activities(client: TestClient, go: Go) -> None:
    go.temporal.answers += [
        done(201, answer("ReceiveLot")),
        done(200, answer("LotUpdate")),
        done(200, answer("LotAdjust")),
        done(200, answer("Take")),
        done(200, answer("Count")),
    ]

    received = _post(client, f"/items/{ITEM}/lots", {"qty": "10", "unit_cost_minor": "5"})
    patched = client.patch(f"{API}/lots/{LOT}", json={"location_id": None}, headers=press())
    adjusted = _post(client, f"/lots/{LOT}/adjust", {"on_hand": "4", "note": "shelf"})
    used = _post(client, f"/items/{ITEM}/use", {"qty": "2", "lot_id": LOT})
    counted = _post(client, f"/items/{ITEM}/count", {"on_hand": "3", "note": "shelf"})

    assert [r.status_code for r in (received, patched, adjusted, used, counted)] == [
        201,
        200,
        200,
        200,
        200,
    ]
    assert received.json() == answer("ReceiveLot")
    assert used.json() == answer("Take")
    assert [(s.name, s.arg["params"], s.arg["body"]) for s in go.temporal.starts] == [
        ("inventory.ReceiveLot", {"item_id": ITEM}, {"qty": "10", "unit_cost_minor": "5"}),
        ("inventory.LotUpdate", {"lot_id": LOT}, {"location_id": None}),
        ("inventory.LotAdjust", {"lot_id": LOT}, {"on_hand": "4", "note": "shelf"}),
        ("inventory.Take", {"item_id": ITEM}, {"qty": "2", "lot_id": LOT}),
        ("inventory.Count", {"item_id": ITEM}, {"on_hand": "3", "note": "shelf"}),
    ]
    assert [s.options["summary"] for s in go.temporal.starts] == [
        f"ReceiveLot item:{ITEM}",
        f"LotUpdate lot:{LOT}",
        f"LotAdjust lot:{LOT}",
        f"Take item:{ITEM}",
        f"Count item:{ITEM}",
    ]
    assert {s.options["task_queue"] for s in go.temporal.starts} == {"inventory"}


# -- units -------------------------------------------------------------------------------


def test_unit_reads_go_to_go(client: TestClient, go: Go) -> None:
    go.reads[f"/items/{ITEM}/units"] = (200, answer("UnitList"))
    go.reads[f"/units/{UNIT}"] = (200, answer("Unit"))

    listed = client.get(f"{API}/items/{ITEM}/units", params={"status": "in_stock"})

    assert listed.json() == answer("UnitList")
    assert dict(go.seen[0].url.params) == {"status": "in_stock"}
    assert client.get(f"{API}/units/{UNIT}").json() == answer("Unit")
    assert client.get(f"{API}/items/{ITEM}/units", params={"status": "lost"}).status_code == 422


def test_unit_writes_start_their_activities(client: TestClient, go: Go) -> None:
    go.temporal.answers += [
        done(201, answer("ReceiveUnits")),
        done(200, answer("UnitUpdate")),
        done(200, answer("UnitStatus")),
    ]

    received = _post(client, f"/items/{ITEM}/units", {"count": 2, "serials": ["A1", ""]})
    patched = client.patch(f"{API}/units/{UNIT}", json={"serial": None}, headers=press())
    status = _post(client, f"/units/{UNIT}/status", {"status": "in_service"})

    assert [r.status_code for r in (received, patched, status)] == [201, 200, 200]
    assert received.json() == answer("ReceiveUnits")
    assert [(s.name, s.arg["params"], s.arg["body"]) for s in go.temporal.starts] == [
        ("inventory.ReceiveUnits", {"item_id": ITEM}, {"count": 2, "serials": ["A1", ""]}),
        ("inventory.UnitUpdate", {"unit_id": UNIT}, {"serial": None}),
        ("inventory.UnitStatus", {"unit_id": UNIT}, {"status": "in_service"}),
    ]


# -- moves -------------------------------------------------------------------------------


def test_move_reads_go_to_go(client: TestClient, go: Go) -> None:
    go.reads["/moves"] = (200, answer("MoveList"))

    got = client.get(f"{API}/moves", params={"item_id": ITEM, "limit": 5})

    assert got.json() == answer("MoveList")
    assert dict(go.seen[0].url.params) == {"item_id": ITEM, "limit": "5"}
    assert client.get(f"{API}/moves", params={"limit": 1001}).status_code == 422


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("step_not_applicable", 409),
        ("wrong_counting", 409),
        ("handler_not_built", 409),
        ("step_incomplete", 409),
        ("lot_empty", 409),
        ("bad_quantity", 422),
    ],
)
def test_a_stock_refusal_is_gos_code(client: TestClient, go: Go, code: str, status: int) -> None:
    """R-K1-23: the new codes pass through as Go wrote them; nothing maps them."""
    go.temporal.answers.append(
        done(
            status,
            {
                "type": PROBLEMS + code,
                "title": "Refused",
                "status": status,
                "detail": "refused",
                "code": code,
            },
        )
    )

    _code(_post(client, f"/items/{ITEM}/use", {"qty": "1"}), status, code)


def test_stock_bodies_pydantic_refuses_never_start(client: TestClient, go: Go) -> None:
    for path, body in (
        (f"/items/{ITEM}/lots", {"qty": "0"}),
        (f"/items/{ITEM}/lots", {"qty": "1", "unit_cost_minor": "1", "total_cost_minor": "1"}),
        (f"/items/{ITEM}/lots", {"qty": "1", "note": "\x00"}),
        (f"/items/{ITEM}/use", {"qty": "1", "reason": "\x00"}),
        (f"/items/{ITEM}/use", {"qty": "1", "entity": "e" * 201}),
        (f"/items/{ITEM}/count", {"on_hand": "1", "note": " "}),
        (f"/items/{ITEM}/units", {"count": 2, "serials": ["one"]}),
        (f"/units/{UNIT}/status", {"status": "lost"}),
        (f"/lots/{LOT}/adjust", {"on_hand": "-1", "note": "x"}),
    ):
        got = _post(client, path, body)
        assert got.status_code == 422, (path, got.text)
    assert go.temporal.starts == []


# -- orders ------------------------------------------------------------------------------


@pytest.mark.requires_git
def test_an_orders_parts_take_from_lots_and_recost_writes_the_order(
    client: TestClient, go: Go
) -> None:
    hook = _item(client)
    lot = _receive(client, hook["id"], unit_cost_minor="5")
    order_id, [item_id] = accepted_order(client)
    path = f"/api/v1/orders/{order_id}/items/{item_id}/parts"

    # The old body's `part_id` key still works for one release.
    made = client.put(path, json={"parts": [{"part_id": hook["id"], "per_unit": 2}]})
    assert made.status_code == 200, made.text
    assert made.json() == [{"item_id": hook["id"], "part_id": hook["id"], "per_unit": 2, "used": 0}]
    both = {"parts": [{"item_id": hook["id"], "part_id": hook["id"], "per_unit": 1}]}
    assert client.put(path, json=both).status_code == 422
    twice = {"parts": [{"item_id": hook["id"], "per_unit": 1}] * 2}
    assert client.put(path, json=twice).status_code == 422
    for missing in ("nope", str(uuid.uuid4())):
        unknown = client.put(path, json={"parts": [{"item_id": missing, "per_unit": 1}]})
        assert unknown.status_code == 409, unknown.text
        assert unknown.json()["type"].endswith("/part_not_found")

    fulfil(client, order_id, item_id, 1)
    stock = _inventory(client).stock
    [use] = stock.moves(item_id=UUID(hook["id"]), limit=1)
    assert (use.kind, use.qty, use.order_item_id) == ("use", Decimal(-2), item_id)
    # The quote's parts: a FIFO take of 2 from what is left, at 5 each.
    history = client.get(
        "/api/v1/orders/history/unit-cost", params={"slug": SLUG, "item_id": item_id}
    ).json()
    assert (history["jobs"], history["parts_unit_minor"], history["parts_unknown"]) == (
        1,
        10,
        False,
    )

    stock.update_lot(UUID(lot["id"]), LotPatch(unit_cost_minor=Decimal(6)))
    recosted = _post(client, f"/lots/{lot['id']}/recost")
    assert recosted.status_code == 200, recosted.text
    assert recosted.json()["moves"] == 1
    assert (recosted.json()["item_id"], recosted.json()["unit_cost_minor"]) == (hook["id"], "6")
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
    assert got.json()["errors"][0]["loc"] == ["query", "item_id"]
