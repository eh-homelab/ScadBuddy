"""`/api/v1/parts` and an order's parts (#5543, job cost spec 2026-11-05 §11): the parts
inventory, the rule a model gives its items, and the parts each line item uses.

Since #7067 a part is an inventory item, and the part routes (`GET`/`POST /parts`, `PATCH`,
`…/stock`, `…/adjust`, `…/moves`) are aliases of `/inventory` (`inventory/part_alias.py`):
each keeps its path, method and answer, and goes after one release (spec §6.11). An item's
parts are set through the lifecycle (the order's lock, an `order_events` row); they leave
the lots when the item is fulfilled and come back when it is unfulfilled
(`orders/lifecycle.py`). Only this process reads or writes the tables (#5295): the
assistant's tools call these routes.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.api.orders import _run, check_path
from scadbuddy.core.problems import ApiError
from scadbuddy.inventory.component import InventoryDep
from scadbuddy.inventory.part_alias import PartHasNoOffer
from scadbuddy.inventory.problems import refused
from scadbuddy.inventory.stock import BadQuantity
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
    StockTooLargeError,
    order_parts,
)
from scadbuddy.orders.problems import PROBLEMS

router = APIRouter(tags=["parts"])

_ALIAS = "Alias of `/inventory/…`; goes after one release (#7067, spec §6.11). "

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


def _unprocessable(field: str, message: str) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        message,
        errors=[{"loc": ["body", field], "msg": message}],
    )


async def _alias[T](
    call: Callable[..., T], *args: Any, quantity: str = "pack_price_minor", **kwargs: Any
) -> T:
    """An alias call off the event loop, its refusal as the old routes answered it;
    `BadQuantity` on the body field `quantity` names. Any other store error the inventory
    routes know is their problem (`inventory.problems.refused`), never a 500."""
    try:
        return await asyncio.to_thread(call, *args, **kwargs)
    except PartNotFoundError as error:
        raise _not_found(error) from None
    except PartExistsError as error:
        raise _exists(error) from None
    except StockTooLargeError as error:
        raise _unprocessable("packs", str(error)) from None
    except PartHasNoOffer:
        raise _unprocessable(
            "supplier", "a part with no supplier has no pack to change: name its supplier"
        ) from None
    except BadQuantity as error:
        raise _unprocessable(quantity, f"a quantity or cost too large to keep: {error}") from None
    except Exception as error:
        problem = refused(error)
        if problem is None:
            raise
        raise problem from None


@router.get(
    "/parts",
    response_model=list[InventoryPart],
    summary="The parts inventory",
    description=(
        _ALIAS + "Every part (an unarchived item of a lot-tracked kind under `hardware` or "
        "`ams_part`), by name: its pack (its "
        "offer's; a part with no supplier has no offer and reads as a pack of 1 at 0), its "
        "unit cost (the weighted average of the lots on hand, a decimal string in minor "
        "units; a lot of unknown cost with no cost entered is left out; with none on hand "
        "the pack price over its size; with no price point either `cost_unknown` is true "
        "and the unit cost reads 0), what is on hand (below "
        "zero when more was used than recorded) and `low` when that is at or below its "
        "`low_stock` level (0 never warns). With `low`, only the low ones."
    ),
)
async def list_parts(inventory: InventoryDep, low: bool = False) -> list[InventoryPart]:
    parts = await _alias(inventory.parts.parts)
    return [p for p in parts if p.low] if low else parts


@router.post(
    "/parts",
    response_model=InventoryPart,
    status_code=status.HTTP_201_CREATED,
    summary="Add a part",
    description=(
        _ALIAS
        + "An item of kind `hardware`. With a `supplier`, an offer at that supplier (matched "
        "by name in any case, else made) with the pack and a price point in the shop's "
        "`orders_currency`; with none, no offer, and the `link` becomes a `purchase` link. "
        "`on_hand` above 0 is received as a `manual` lot (an `add` move) at the pack's price "
        "over its size, of unknown cost with no supplier. 409 `part_exists` when another "
        "part of the kind has the name, in any case. 422 (on `supplier`) for a pack (a size "
        "other than 1 or a price other than 0) with no supplier: it has no offer to hold "
        "it. 422 for a pack "
        "price past what a price point holds."
    ),
)
async def create_part(
    body: PartInput, inventory: InventoryDep, settings: SettingsStoreDep
) -> InventoryPart:
    by = current_actor()
    currency = settings.snapshot().runtime.orders_currency
    return await _alias(inventory.parts.create, body, currency=currency, by=by)


@router.patch(
    "/parts/{part_id}",
    response_model=InventoryPart,
    summary="Edit a part",
    description=(
        _ALIAS
        + "Only what is sent changes. A new `supplier` moves the part to a new offer at that "
        "supplier (the old one is archived; its pack and price move with it); a blank one "
        "archives the offer, and its link stays as a `purchase` link. With no supplier a "
        "new `link` is a `purchase` link, and a blank one removes the purchase links these "
        "routes wrote. A new pack price is a new price point in the part's currency. "
        "The unit cost is the lots' and does not move. 422 for a pack edit on a part with "
        "no supplier (it has no offer to hold the pack)."
    ),
)
async def patch_part(part_id: str, body: PartPatch, inventory: InventoryDep) -> InventoryPart:
    check_path(part_id)
    return await _alias(inventory.parts.update, part_id, body, current_actor())


@router.post(
    "/parts/{part_id}/stock",
    response_model=InventoryPart,
    summary="Add stock: packs bought",
    description=(
        _ALIAS
        + "`packs` of the part's pack received as one `manual` lot at `packs` times the pack "
        "price, in the part's currency: `pack_price_minor` when sent (a new price point of "
        "the offer), else the offer's latest. A part with no supplier has no offer: a pack "
        "of 1 at the `pack_price_minor` sent, and no price point; with none sent, the lot's "
        "cost is unknown. A part priced in another currency than the shop's receives a lot "
        "of unknown cost. Orders already fulfilled keep their cost. 422 when the packs come "
        "to more than 1,000,000 units, or would leave more than 1,000,000,000 on hand."
    ),
)
async def add_stock(part_id: str, body: StockAdd, inventory: InventoryDep) -> InventoryPart:
    check_path(part_id)
    return await _alias(inventory.parts.add_stock, part_id, body, current_actor())


@router.post(
    "/parts/{part_id}/adjust",
    response_model=InventoryPart,
    summary="Correct a part's count",
    description=(
        _ALIAS + "The count on the shelf, with a note saying why it differs: the item is counted "
        "(`POST /inventory/items/{id}/count`), one `adjust` move per lot it changes, at "
        "each lot's cost."
    ),
)
async def adjust_stock(part_id: str, body: StockAdjust, inventory: InventoryDep) -> InventoryPart:
    check_path(part_id)
    return await _alias(
        inventory.parts.adjust,
        part_id,
        body.on_hand,
        body.note,
        current_actor(),
        quantity="on_hand",
    )


@router.get(
    "/parts/{part_id}/moves",
    response_model=list[StockMove],
    summary="A part's stock line",
    description=(
        _ALIAS + "Every change to the part's lots, newest first: `add` (a receive), `adjust`, "
        "`use`, `return`. `amount_minor` is null for a move of unknown cost."
    ),
)
async def part_moves(
    part_id: str, inventory: InventoryDep, limit: Annotated[int, Query(ge=1, le=500)] = 200
) -> list[StockMove]:
    check_path(part_id)
    return await _alias(inventory.parts.moves, part_id, limit=limit)


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
