"""`/api/v1/inventory` stock: lots, units and moves (#7067, spec
2026-11-21-inventory-and-purchasing-design.md §6.4 and §6.6).

A lot item's stock is in lots, each at its own cost; a use takes FIFO by received time
unless a lot is named (D2), and a use past the stock on hand goes on the newest lot,
flagged `needs_review`. A move's cost is a snapshot (D14): a later lot cost change moves no
past move. A unit item's stock is its units in stock. A kind with tracking `none` is not
counted: every call here refuses its items (409 `item_not_counted`). "Re-cost past uses"
(`POST /inventory/lots/{lot_id}/recost`) comes with the orders' use of items, as it writes
order events. Only this process reads or writes the tables (#5295).

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import BaseModel

from scadbuddy.api.inventory import NO_NUL, codes, run
from scadbuddy.inventory.component import InventoryDep
from scadbuddy.inventory.models import (
    AdjustInput,
    CountInput,
    Lot,
    LotPatch,
    Move,
    ReceiveLot,
    ReceiveUnits,
    Unit,
    UnitPatch,
    UnitStatusInput,
    UseInput,
)
from scadbuddy.inventory.problems import lot_not_found, unit_not_found
from scadbuddy.orders.actor import current_actor

router = APIRouter(tags=["inventory"])

_ITEM_CODES = "`item_not_counted`, `wrong_tracking` or `filament_item_refused`"
_ENTITY = "`bad_entity_ref` or `unknown_entity_kind` (the `entity`)"


class LotList(BaseModel):
    lots: list[Lot]


class UnitList(BaseModel):
    units: list[Unit]


class MoveList(BaseModel):
    moves: list[Move]


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
    responses=codes((404, "`item_not_found`"), (409, _ITEM_CODES)),
)
async def list_lots(item_id: UUID, inventory: InventoryDep) -> LotList:
    return LotList(lots=await run(inventory.stock.lots, item_id))


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
    responses=codes(
        (404, "`item_not_found` or `location_not_found`"),
        (409, _ITEM_CODES),
        (422, "`bad_quantity`"),
    ),
)
async def receive_lot(item_id: UUID, body: ReceiveLot, inventory: InventoryDep) -> Lot:
    return await run(inventory.stock.receive_lot, item_id, body, current_actor())


@router.get(
    "/inventory/lots/{lot_id}",
    response_model=Lot,
    summary="A lot",
    description="The lot with what is on hand and its cost now.",
    responses=codes((404, "`lot_not_found`")),
)
async def get_lot(lot_id: UUID, inventory: InventoryDep) -> Lot:
    lot = await run(inventory.stock.lot, lot_id)
    if lot is None:
        raise lot_not_found(lot_id)
    return lot


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
    responses=codes(
        (404, "`lot_not_found` or `location_not_found`"),
        (409, _ITEM_CODES),
        (422, "`bad_quantity` (the cost)"),
    ),
)
async def patch_lot(lot_id: UUID, body: LotPatch, inventory: InventoryDep) -> Lot:
    return await run(inventory.stock.update_lot, lot_id, body)


@router.post(
    "/inventory/lots/{lot_id}/adjust",
    response_model=Lot,
    summary="Count a lot",
    description=(
        "Sets the lot's `on_hand` to the count, with one `adjust` move of the difference at "
        "the lot's cost; none when the count matches. The note says why."
    ),
    responses=codes((404, "`lot_not_found`"), (409, _ITEM_CODES), (422, "`bad_quantity`")),
)
async def adjust_lot(lot_id: UUID, body: AdjustInput, inventory: InventoryDep) -> Lot:
    return await run(inventory.stock.adjust_lot, lot_id, body, current_actor())


@router.post(
    "/inventory/items/{item_id}/use",
    response_model=MoveList,
    summary="Use some of an item",
    description=(
        "Takes `qty` FIFO, the named `lot_id` first, one `use` move per lot at that lot's "
        "cost now (a snapshot). What no lot holds goes on the newest lot, below 0, flagged "
        "`needs_review`; an item with no lot opens a zero-cost `adjustment` lot of unknown "
        "cost. `entity` (`kind:ref`, such as `printer:<id>`) names what it was used on."
    ),
    responses=codes(
        (404, "`item_not_found`, `lot_not_found` or `entity_not_found`"),
        (409, _ITEM_CODES),
        (422, "`bad_quantity`, " + _ENTITY),
    ),
)
async def use_item(item_id: UUID, body: UseInput, inventory: InventoryDep) -> MoveList:
    return MoveList(moves=await run(inventory.stock.use, item_id, body, current_actor()))


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
    responses=codes((404, "`item_not_found`"), (409, _ITEM_CODES), (422, "`bad_quantity`")),
)
async def count_item(item_id: UUID, body: CountInput, inventory: InventoryDep) -> MoveList:
    moves = await run(inventory.stock.count_item, item_id, body.on_hand, body.note, current_actor())
    return MoveList(moves=moves)


# -- units -------------------------------------------------------------------------------


@router.get(
    "/inventory/items/{item_id}/units",
    response_model=UnitList,
    summary="An item's units",
    description="The item's units, oldest first; only those of `status` when given.",
    responses=codes((404, "`item_not_found`"), (409, _ITEM_CODES)),
)
async def list_units(
    item_id: UUID,
    inventory: InventoryDep,
    status_: Annotated[
        str | None,
        Query(alias="status", pattern="^(in_stock|in_service|retired|scrapped|sold)$"),
    ] = None,
) -> UnitList:
    return UnitList(units=await run(inventory.stock.units, item_id, status_))


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
    responses=codes(
        (404, "`item_not_found` or `location_not_found`"),
        (409, _ITEM_CODES + " or `serial_exists`"),
    ),
)
async def receive_units(item_id: UUID, body: ReceiveUnits, inventory: InventoryDep) -> UnitList:
    return UnitList(units=await run(inventory.stock.receive_units, item_id, body, current_actor()))


@router.get(
    "/inventory/units/{unit_id}",
    response_model=Unit,
    summary="A unit",
    description="The unit with its status, cost and service dates.",
    responses=codes((404, "`unit_not_found`")),
)
async def get_unit(unit_id: UUID, inventory: InventoryDep) -> Unit:
    unit = await run(inventory.stock.unit, unit_id)
    if unit is None:
        raise unit_not_found(unit_id)
    return unit


@router.patch(
    "/inventory/units/{unit_id}",
    response_model=Unit,
    summary="Edit a unit",
    description=(
        "Only what is sent changes: its name, its serial (null or blank clears it; unique "
        "within the item) and its location (null clears it). No stock moves."
    ),
    responses=codes(
        (404, "`unit_not_found` or `location_not_found`"),
        (409, "`serial_exists`"),
    ),
)
async def patch_unit(unit_id: UUID, body: UnitPatch, inventory: InventoryDep) -> Unit:
    return await run(inventory.stock.update_unit, unit_id, body)


@router.post(
    "/inventory/units/{unit_id}/status",
    response_model=Unit,
    summary="Change a unit's status",
    description=(
        "Leaving `in_stock` writes a move of -1 at the unit's cost (`in_service` a `use`, "
        "`sold` a `sell`, `retired` a `retire`, `scrapped` a `scrap`); coming back writes a "
        "`return` of +1; a change between two statuses out of stock moves no stock. "
        "`entity` (`kind:ref`) names what it went into or to. A unit fitted in another is "
        "409 `unit_installed`."
    ),
    responses=codes(
        (404, "`unit_not_found` or `entity_not_found`"),
        (409, "`unit_installed`"),
        (422, _ENTITY),
    ),
)
async def set_unit_status(unit_id: UUID, body: UnitStatusInput, inventory: InventoryDep) -> Unit:
    return await run(inventory.stock.set_unit_status, unit_id, body, current_actor())


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
)
async def list_moves(
    inventory: InventoryDep,
    item_id: UUID | None = None,
    lot_id: UUID | None = None,
    unit_id: UUID | None = None,
    order_item_id: Annotated[str | None, Query(max_length=100, pattern=NO_NUL)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> MoveList:
    def listed() -> list[Move]:
        return inventory.stock.moves(
            item_id=item_id,
            lot_id=lot_id,
            unit_id=unit_id,
            order_item_id=order_item_id,
            limit=limit,
        )

    return MoveList(moves=await run(listed))
