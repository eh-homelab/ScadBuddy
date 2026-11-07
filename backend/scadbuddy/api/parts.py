"""`/api/v1/parts` and an order's parts (#5543, job cost spec 2026-11-05 §11): the parts
inventory, the rule a model gives its items, and the parts each line item uses.

A part's own writes (`POST /parts`, `PATCH`, `…/stock`, `…/adjust`) each run in one
transaction under the part's row lock and write a stock line. An item's parts are set
through the lifecycle (the order's lock, an `order_events` row); they leave stock when the
item is fulfilled and come back when it is unfulfilled (`orders/lifecycle.py`). Only this
process reads or writes the tables (#5295): the assistant's tools call these routes.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.api.orders import _path, _run
from scadbuddy.core.problems import ApiError
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.parts import (
    InventoryPart,
    ItemPartRow,
    ItemPartsInput,
    OrderParts,
    PartExistsError,
    PartInput,
    PartNotFoundError,
    PartPatch,
    PartRule,
    PartRuleInput,
    StockAdd,
    StockAdjust,
    StockMove,
    order_parts,
)
from scadbuddy.orders.problems import PROBLEMS

router = APIRouter(tags=["parts"])

#: A model's slug, as a line item's reference names it (a built-in's with its prefix).
Slug = Annotated[str, Path(max_length=MAX_MODEL_ID_LENGTH, pattern=MODEL_ID_PATTERN)]


def _not_found(error: PartNotFoundError) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, str(error), type_=PROBLEMS + "part_not_found")


def _exists(error: PartExistsError) -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        f"{error}: {error.existing_id}",
        type_=PROBLEMS + "part_exists",
        part_id=error.existing_id,
    )


@router.get(
    "/parts",
    response_model=list[InventoryPart],
    summary="The parts inventory",
    description=(
        "Every part, by name: its pack, its unit cost (the weighted average of what is on "
        "hand, a decimal string in minor units), what is on hand (below zero when more was "
        "used than recorded) and `low` when that is at or below its `low_stock` level "
        "(0 never warns). With `low`, only the low ones."
    ),
)
async def list_parts(orders: OrdersDep, low: bool = False) -> list[InventoryPart]:
    parts = await asyncio.to_thread(orders.lifecycle.parts.parts)
    return [p for p in parts if p.low] if low else parts


@router.post(
    "/parts",
    response_model=InventoryPart,
    status_code=status.HTTP_201_CREATED,
    summary="Add a part",
    description=(
        "Priced in the shop's `orders_currency`. Its unit cost starts at the pack's price "
        "over its size. `on_hand` above 0 is recorded as an `adjust` line. 409 "
        "`part_exists` when another part has the name, in any case."
    ),
)
async def create_part(
    body: PartInput, orders: OrdersDep, settings: SettingsStoreDep
) -> InventoryPart:
    by = current_actor()
    currency = settings.snapshot().runtime.orders_currency
    try:
        return await asyncio.to_thread(
            orders.lifecycle.parts.create, body, currency=currency, by=by
        )
    except PartExistsError as error:
        raise _exists(error) from None


@router.patch(
    "/parts/{part_id}",
    response_model=InventoryPart,
    summary="Edit a part",
    description=(
        'Only what is sent changes. The pack is what the next "Add stock" records: the '
        "unit cost stays, unless nothing is on hand, when it becomes the new pack's."
    ),
)
async def patch_part(part_id: str, body: PartPatch, orders: OrdersDep) -> InventoryPart:
    _path(part_id)
    try:
        return await asyncio.to_thread(orders.lifecycle.parts.update, part_id, body)
    except PartNotFoundError as error:
        raise _not_found(error) from None
    except PartExistsError as error:
        raise _exists(error) from None


@router.post(
    "/parts/{part_id}/stock",
    response_model=InventoryPart,
    summary="Add stock: packs bought",
    description=(
        "`packs` of the part's pack join the stock, at `pack_price_minor` each when sent "
        "(it becomes the part's pack price). The unit cost becomes the weighted average of "
        "the stock on hand and the new packs (their own cost when nothing is on hand). "
        "Orders already fulfilled keep their cost. On the part's stock line as `add`."
    ),
)
async def add_stock(part_id: str, body: StockAdd, orders: OrdersDep) -> InventoryPart:
    _path(part_id)
    by = current_actor()
    try:
        return await asyncio.to_thread(orders.lifecycle.parts.add_stock, part_id, body, by)
    except PartNotFoundError as error:
        raise _not_found(error) from None


@router.post(
    "/parts/{part_id}/adjust",
    response_model=InventoryPart,
    summary="Correct a part's count",
    description=(
        "The count on the shelf, with a note saying why it differs. The unit cost stays. "
        "On the part's stock line as `adjust`, with the difference."
    ),
)
async def adjust_stock(part_id: str, body: StockAdjust, orders: OrdersDep) -> InventoryPart:
    _path(part_id)
    by = current_actor()
    try:
        return await asyncio.to_thread(orders.lifecycle.parts.adjust, part_id, body, by)
    except PartNotFoundError as error:
        raise _not_found(error) from None


@router.get(
    "/parts/{part_id}/moves",
    response_model=list[StockMove],
    summary="A part's stock line",
    description="Every change to the part's stock, newest first: `add`, `adjust`, `use`, `return`.",
)
async def part_moves(
    part_id: str, orders: OrdersDep, limit: Annotated[int, Query(ge=1, le=500)] = 200
) -> list[StockMove]:
    _path(part_id)
    part = await asyncio.to_thread(orders.lifecycle.parts.part, part_id)
    if part is None:
        raise _not_found(PartNotFoundError(part_id))
    return await asyncio.to_thread(orders.lifecycle.parts.moves, part_id, limit=limit)


@router.get(
    "/parts/rules/{slug}",
    response_model=PartRule,
    summary="The parts a model's items use",
    description="The model's rule; one with no lines when none was saved.",
)
async def get_rule(slug: Slug, orders: OrdersDep) -> PartRule:
    return await asyncio.to_thread(orders.lifecycle.parts.rule, slug)


@router.put(
    "/parts/rules/{slug}",
    response_model=PartRule,
    summary="Set the parts a model's items use",
    description=(
        "Each line is one part per unit: the first `when` whose colour the item has (in any "
        "case), else `part_id` (null: none). A new item of the model copies the parts it "
        "gives; items already added keep theirs. No lines removes the rule. 404 "
        "`part_not_found` for a part that does not exist."
    ),
)
async def put_rule(slug: Slug, body: PartRuleInput, orders: OrdersDep) -> PartRule:
    by = current_actor()
    try:
        return await asyncio.to_thread(orders.lifecycle.parts.set_rule, slug, body, by)
    except PartNotFoundError as error:
        raise _not_found(error) from None


@router.get(
    "/orders/{order_id}/parts",
    response_model=OrderParts,
    summary="An order's parts, and what it is short",
    description=(
        "Each item's parts per unit and how many its fulfilment took (`used`). `needs` is "
        "what the items not yet fulfilled (nor cancelled) need, part by part, against the "
        "stock on hand; `short` lists those it does not cover. Advice only: stock never "
        "blocks."
    ),
)
async def get_order_parts(order_id: str, orders: OrdersDep) -> OrderParts:
    _path(order_id)
    found = await asyncio.to_thread(order_parts, orders.store, orders.lifecycle.parts, order_id)
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    return found


@router.put(
    "/orders/{order_id}/items/{item_id}/parts",
    response_model=list[ItemPartRow],
    summary="Set the parts a line item uses",
    description=(
        "The parts one unit uses, replaced whole; on the audit line as `item.parts_set`. "
        "They leave stock, each as a `material` cost entry (`source` "
        "`part:<item>:<part>`), when the item is fulfilled, and come back when it is "
        "unfulfilled. 409 `item_parts_used` for a fulfilled item, `part_not_found` for a "
        "part that does not exist, `order_closed`/`order_cancelled` once ended."
    ),
)
async def set_item_parts(
    order_id: str, item_id: str, body: ItemPartsInput, orders: OrdersDep
) -> list[ItemPartRow]:
    _path(order_id, item_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.set_item_parts(order_id, item_id, body.parts, by))
