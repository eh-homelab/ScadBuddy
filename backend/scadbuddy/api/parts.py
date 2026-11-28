"""The rule a model gives its items and an order's parts (#5543, job cost spec 2026-11-05
§11): `/parts/rules/{slug}`, `/orders/{id}/parts` and `/orders/{id}/items/{item}/parts`.

Since #7067 a part is an inventory item. The `/parts` alias routes over the inventory are
gone (#6949, K1 5/7): stock and items are the `/inventory` routes. An item's
parts are set through the lifecycle (the order's lock, an `order_events` row); they leave
the lots when the item is fulfilled and come back when it is unfulfilled
(`orders/lifecycle.py`). Only this process reads or writes the tables (#5295): the
assistant's tools call these routes.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Path, status

from scadbuddy.api.orders import _run, check_path
from scadbuddy.core.problems import ApiError
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.parts import (
    ItemPartRow,
    ItemPartsInput,
    OrderParts,
    PartNotFoundError,
    PartRule,
    PartRuleInput,
    order_parts,
)
from scadbuddy.orders.problems import PROBLEMS

router = APIRouter(tags=["parts"])

#: A model's slug, as a line item's reference names it (a built-in's with its prefix).
Slug = Annotated[str, Path(max_length=MAX_MODEL_ID_LENGTH, pattern=MODEL_ID_PATTERN)]


def _not_found(error: PartNotFoundError) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, str(error), type_=PROBLEMS + "part_not_found")


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
        "case), else `item_id` (null: none). A part is an inventory item whose kind tracks "
        "lots (#7067); a line or `when` may still name it `part_id`, read the same, and the "
        "rule answers `item_id`. A new item of the model copies the parts it gives; items "
        "already added keep theirs. No lines removes the rule. 404 `part_not_found` for an "
        "item that does not exist or does not track lots."
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
        "stock on hand (the item's lots summed; `on_hand` and `short` are decimal strings, "
        "#7067); `short` lists those it does not cover. Advice only: stock never blocks."
    ),
)
async def get_order_parts(order_id: str, orders: OrdersDep) -> OrderParts:
    check_path(order_id)
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
        "Each part is an inventory item whose kind tracks lots, named `item_id` (`part_id` "
        "is read the same, for one release; a row that names both is 422). They leave the "
        "item's lots (FIFO, each move at its lot's cost) when the item is fulfilled, and "
        "come back to the same lots at the same cost when it is unfulfilled; job cost reads "
        "them as the item's `parts_minor` (#7067). 409 `item_parts_used` for a fulfilled "
        "item, `part_not_found` for an item that does not exist or does not track lots, "
        "`order_closed`/`order_cancelled` once ended."
    ),
)
async def set_item_parts(
    order_id: str, item_id: str, body: ItemPartsInput, orders: OrdersDep
) -> list[ItemPartRow]:
    check_path(order_id, item_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.set_item_parts(order_id, item_id, body.parts, by))
