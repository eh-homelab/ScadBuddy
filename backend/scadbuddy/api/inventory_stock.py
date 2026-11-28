"""`/api/v1/inventory` stock: lots, units and moves (#7067, spec
2026-11-21-inventory-and-purchasing-design.md §6.4 and §6.6).

Since K1 (#6949, design §6.1a) scadbuddy-core owns the stock: each route here parses its
request, then reads Go's CRUD route or starts Go's activity on the queue `inventory`
(`inventory/core_client.py`), and answers Go's JSON, or 202 `OperationAccepted` when the
write takes longer than 15 s. Go's problems pass through with their codes (R-K1-23): a lots
call on an item counted in units, or the reverse, is 409 `wrong_counting`; a step the
item's class has none of (a service's receive) is 409 `step_not_applicable`; a handler K1
does not build (a durable's use, a service's) is 409 `handler_not_built`; a step that lacks
a required parameter is 409 `step_incomplete`.

A lot item's stock is in lots, each at its own cost; a use takes FIFO by received time
unless a lot is named (D2), and a use past the stock on hand goes on the newest lot,
flagged `needs_review`. A move's cost is a snapshot (D14). "Re-cost past uses"
(`POST /inventory/lots/{lot_id}/recost`) is the `RecostLot` workflow (Task 16,
`workflows/order_stock.py`): it reads the orders the lot's moves name, runs Go's `Recost`,
then writes the orders' events.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scadbuddy.api.inventory import (
    NO_NUL,
    IdempotencyKey,
    codes,
    forward,
    run,
    write_codes,
)
from scadbuddy.inventory.component import InventoryDep
from scadbuddy.inventory.core_client import operation_key, passed_through
from scadbuddy.inventory.models import (
    AdjustInput,
    CountInput,
    Lot,
    LotPatch,
    Move,
    ReceiveLot,
    ReceiveUnits,
    RecostResult,
    Unit,
    UnitPatch,
    UnitStatusInput,
    UseInput,
)
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.stock_start import WorkflowAccepted, WorkflowRefused, accepted_response

router = APIRouter(tags=["inventory"])

_ITEM_CODES = (
    "`step_not_applicable`, `wrong_counting`, `handler_not_built`, `step_incomplete` or "
    "`filament_item_refused`"
)
_ENTITY = "`bad_entity_ref` or `unknown_entity_kind` (the `entity`)"
_UNAVAILABLE = (503, "`inventory_unavailable`: scadbuddy-core did not answer")


class LotList(BaseModel):
    lots: list[Lot]


class UnitList(BaseModel):
    units: list[Unit]


class MoveList(BaseModel):
    moves: list[Move]


def _item(item_id: UUID) -> dict[str, str]:
    return {"item_id": str(item_id)}


# -- lots --------------------------------------------------------------------------------


@router.get(
    "/inventory/items/{item_id}/lots",
    response_model=LotList,
    summary="An item's lots",
    description=(
        "The item's lots in FIFO order (received time, then id). Quantities and costs are "
        "decimal strings; costs in minor units of the shop currency. `on_hand` is below 0 "
        "after a shortfall."
    ),
    responses=codes((404, "`item_not_found`"), (409, _ITEM_CODES), _UNAVAILABLE),
)
async def list_lots(item_id: UUID, inventory: InventoryDep) -> LotList:
    return LotList.model_validate(await inventory.core.read(f"/items/{item_id}/lots"))


@router.post(
    "/inventory/items/{item_id}/lots",
    response_model=Lot,
    status_code=status.HTTP_201_CREATED,
    summary="Receive a lot",
    description=(
        "A lot with no invoice yet (`origin` `manual`) and its `receive` move. The cost is "
        "`unit_cost_minor` or `total_cost_minor`, not both, in `currency` (the shop's "
        "`orders_currency` when absent). With no cost, or a cost in another currency, the "
        "lot opens at 0 with `cost_unknown`; a cost in another currency is kept as "
        "`foreign_unit_cost_minor`. An `each` item takes whole quantities only "
        "(422 `bad_quantity`)."
    ),
    responses=write_codes(
        (404, "`item_not_found` or `location_not_found`"),
        (409, _ITEM_CODES),
        (422, "`bad_quantity` or `bad_input`"),
    ),
)
async def receive_lot(
    item_id: UUID,
    body: ReceiveLot,
    inventory: InventoryDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    return await forward(
        inventory,
        "ReceiveLot",
        Lot,
        key=idempotency_key,
        subject=f"item:{item_id}",
        params=_item(item_id),
        body=body,
    )


@router.get(
    "/inventory/lots/{lot_id}",
    response_model=Lot,
    summary="A lot",
    description="The lot with what is on hand and its cost now.",
    responses=codes((404, "`lot_not_found`"), _UNAVAILABLE),
)
async def get_lot(lot_id: UUID, inventory: InventoryDep) -> Lot:
    return Lot.model_validate(await inventory.core.read(f"/lots/{lot_id}"))


@router.patch(
    "/inventory/lots/{lot_id}",
    response_model=Lot,
    summary="Edit a lot",
    description=(
        "Only what is sent changes. `unit_cost_minor` makes the lot's cost known from now "
        "on, in minor units of the shop currency; no past move changes (its cost is a "
        "snapshot) until the lot's past uses are re-costed. `location_id` null clears the "
        "location."
    ),
    responses=write_codes(
        (404, "`lot_not_found` or `location_not_found`"),
        (409, _ITEM_CODES),
        (422, "`bad_quantity` (the cost) or `bad_input`"),
    ),
)
async def patch_lot(
    lot_id: UUID,
    body: LotPatch,
    inventory: InventoryDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    return await forward(
        inventory,
        "LotUpdate",
        Lot,
        key=idempotency_key,
        subject=f"lot:{lot_id}",
        params={"lot_id": str(lot_id)},
        body=body,
    )


@router.post(
    "/inventory/lots/{lot_id}/adjust",
    response_model=Lot,
    summary="Count a lot",
    description=(
        "Sets the lot's `on_hand` to the count, with one `adjust` move of the difference at "
        "the lot's cost; none when the count matches. The note says why."
    ),
    responses=write_codes(
        (404, "`lot_not_found`"), (409, _ITEM_CODES), (422, "`bad_quantity` or `bad_input`")
    ),
)
async def adjust_lot(
    lot_id: UUID,
    body: AdjustInput,
    inventory: InventoryDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    return await forward(
        inventory,
        "LotAdjust",
        Lot,
        key=idempotency_key,
        subject=f"lot:{lot_id}",
        params={"lot_id": str(lot_id)},
        body=body,
    )


@router.post(
    "/inventory/lots/{lot_id}/recost",
    response_model=RecostResult,
    summary="Re-cost a lot's past uses",
    description=(
        "Every past `use`, `return` and `adjust` move of the lot takes the lot's cost now "
        "(and its `cost_unknown`): a move's cost is otherwise a snapshot. `moves` counts "
        "the moves that changed; `orders` names each order item whose parts cost or unknown "
        "flag changed as job cost reads them (`parts_minor`: the known-cost moves; unknown "
        "while a use not given back is of unknown cost), before and after. Each such "
        "order gets one `item.parts_recosted` event with its items' before and after. "
        "It runs as a workflow (#6949 K1): 202 with it when it takes longer than 15 s; the "
        "same `Idempotency-Key` again attaches to it. 409 `recost_orders_moved` when new "
        "order moves kept reaching the lot; nothing was written, try again."
    ),
    responses=write_codes(
        (404, "`lot_not_found`"),
        (409, _ITEM_CODES + " or `recost_orders_moved`"),
        (422, "`bad_quantity` (a move's amount at the cost)"),
    ),
)
async def recost_lot(
    lot_id: UUID, orders: OrdersDep, idempotency_key: IdempotencyKey = None
) -> RecostResult | JSONResponse:
    by = current_actor()
    key = operation_key(by, idempotency_key)
    try:
        answer = await run(lambda: orders.lifecycle.recost_lot(lot_id, by, request_key=key))
    except WorkflowRefused as refused:
        raise passed_through(int(refused.problem.get("status") or 409), refused.problem) from None
    if isinstance(answer, WorkflowAccepted):
        return accepted_response(answer)
    return answer


@router.post(
    "/inventory/items/{item_id}/use",
    response_model=MoveList,
    summary="Use some of an item",
    description=(
        "Takes `qty` FIFO, the named `lot_id` first, one `use` move per lot at that lot's "
        "cost now (a snapshot). A named lot with no stock on hand is 409 `lot_empty`. "
        "What no lot holds goes on the newest lot, below 0, flagged `needs_review`; an item "
        "with no lot opens a zero-cost `adjustment` lot of unknown cost. "
        "`entity` (`kind:ref`, such as `printer:<id>`) names what it was used on. A use of "
        "a durable or a service is 409 `handler_not_built` until K2 builds its handler."
    ),
    responses=write_codes(
        (404, "`item_not_found`, `lot_not_found` or `entity_not_found`"),
        (409, _ITEM_CODES + " or `lot_empty` (the named lot has no stock)"),
        (422, "`bad_quantity`, `bad_input`, " + _ENTITY),
    ),
)
async def use_item(
    item_id: UUID,
    body: UseInput,
    inventory: InventoryDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    return await forward(
        inventory,
        "Take",
        MoveList,
        key=idempotency_key,
        subject=f"item:{item_id}",
        params=_item(item_id),
        body=body,
    )


@router.post(
    "/inventory/items/{item_id}/count",
    response_model=MoveList,
    summary="Count an item",
    description=(
        "Sets the item's whole `on_hand` (the sum of its lots) to the count. A lower count "
        "takes the difference FIFO as `adjust` moves; a higher one adds it to the newest "
        "lot at its cost, or to a new `adjustment` lot of unknown cost when there is none. "
        "The moves written; none when the count matches. The note says why."
    ),
    responses=write_codes(
        (404, "`item_not_found`"), (409, _ITEM_CODES), (422, "`bad_quantity` or `bad_input`")
    ),
)
async def count_item(
    item_id: UUID,
    body: CountInput,
    inventory: InventoryDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    return await forward(
        inventory,
        "Count",
        MoveList,
        key=idempotency_key,
        subject=f"item:{item_id}",
        params=_item(item_id),
        body=body,
    )


# -- units -------------------------------------------------------------------------------


@router.get(
    "/inventory/items/{item_id}/units",
    response_model=UnitList,
    summary="An item's units",
    description="The item's units, oldest first; only those of `status` when given.",
    responses=codes((404, "`item_not_found`"), (409, _ITEM_CODES), _UNAVAILABLE),
)
async def list_units(
    item_id: UUID,
    inventory: InventoryDep,
    status_: Annotated[
        str | None,
        Query(alias="status", pattern="^(in_stock|in_service|retired|scrapped|sold|ended)$"),
    ] = None,
) -> UnitList:
    read = await inventory.core.read(f"/items/{item_id}/units", params={"status": status_})
    return UnitList.model_validate(read)


@router.post(
    "/inventory/items/{item_id}/units",
    response_model=UnitList,
    status_code=status.HTTP_201_CREATED,
    summary="Receive units",
    description=(
        "`count` units in stock, each with a `receive` move, at `cost_minor` each in minor "
        "units of the shop currency (unknown when absent). `serials`, when given, has one "
        "entry per unit; a blank one is no serial. A serial is unique within the item "
        "(409 `serial_exists`)."
    ),
    responses=write_codes(
        (404, "`item_not_found` or `location_not_found`"),
        (409, _ITEM_CODES + " or `serial_exists`"),
        (422, "`bad_input`"),
    ),
)
async def receive_units(
    item_id: UUID,
    body: ReceiveUnits,
    inventory: InventoryDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    return await forward(
        inventory,
        "ReceiveUnits",
        UnitList,
        key=idempotency_key,
        subject=f"item:{item_id}",
        params=_item(item_id),
        body=body,
    )


@router.get(
    "/inventory/units/{unit_id}",
    response_model=Unit,
    summary="A unit",
    description="The unit with its status, cost and service dates.",
    responses=codes((404, "`unit_not_found`"), _UNAVAILABLE),
)
async def get_unit(unit_id: UUID, inventory: InventoryDep) -> Unit:
    return Unit.model_validate(await inventory.core.read(f"/units/{unit_id}"))


@router.patch(
    "/inventory/units/{unit_id}",
    response_model=Unit,
    summary="Edit a unit",
    description=(
        "Only what is sent changes: its name, its serial (null or blank clears it; unique "
        "within the item) and its location (null clears it). No stock moves."
    ),
    responses=write_codes(
        (404, "`unit_not_found` or `location_not_found`"),
        (409, "`serial_exists`"),
        (422, "`bad_input`"),
    ),
)
async def patch_unit(
    unit_id: UUID,
    body: UnitPatch,
    inventory: InventoryDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    return await forward(
        inventory,
        "UnitUpdate",
        Unit,
        key=idempotency_key,
        subject=f"unit:{unit_id}",
        params={"unit_id": str(unit_id)},
        body=body,
    )


@router.post(
    "/inventory/units/{unit_id}/status",
    response_model=Unit,
    summary="Change a unit's status",
    description=(
        "Leaving `in_stock` writes a move of -1 at the unit's cost (`in_service` a `use`, "
        "`sold` a `sell`, `retired` a `retire`, `scrapped` a `scrap`); coming back writes a "
        "`return` of +1; a change between two statuses out of stock moves no stock. "
        "`entity` (`kind:ref`) names what it went into or to. `entity` and `note` go on the "
        "move, so a change that writes none refuses them (422 `bad_input`). A unit fitted in "
        "another is 409 `unit_installed`."
    ),
    responses=write_codes(
        (404, "`unit_not_found` or `entity_not_found`"),
        (409, "`unit_installed`, `step_not_applicable` or `handler_not_built`"),
        (422, "`bad_input`, " + _ENTITY),
    ),
)
async def set_unit_status(
    unit_id: UUID,
    body: UnitStatusInput,
    inventory: InventoryDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    return await forward(
        inventory,
        "UnitStatus",
        Unit,
        key=idempotency_key,
        subject=f"unit:{unit_id}",
        params={"unit_id": str(unit_id)},
        body=body,
    )


# -- moves -------------------------------------------------------------------------------


@router.get(
    "/inventory/moves",
    response_model=MoveList,
    summary="Stock moves",
    description=(
        "Moves matching every filter given, newest first, up to `limit`. Each holds its "
        "cost when it was written (`unit_cost_minor`, `amount_minor`), a decimal string in "
        "minor units of the shop currency; `qty` below 0 takes stock."
    ),
    responses=codes(_UNAVAILABLE),
)
async def list_moves(
    inventory: InventoryDep,
    item_id: UUID | None = None,
    lot_id: UUID | None = None,
    unit_id: UUID | None = None,
    order_item_id: Annotated[str | None, Query(max_length=100, pattern=NO_NUL)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> MoveList:
    params = {
        "item_id": item_id,
        "lot_id": lot_id,
        "unit_id": unit_id,
        "order_item_id": order_item_id,
        "limit": limit,
    }
    return MoveList.model_validate(await inventory.core.read("/moves", params=params))
