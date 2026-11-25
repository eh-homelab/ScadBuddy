"""`/api/v1/inventory`: item kinds, items and supplier offers (#7067, spec
2026-11-21-inventory-and-purchasing-design.md §6.4 and §6.5). The stock routes are in
`api/inventory_stock.py`.

Kinds are rows, never an allowlist in code (spec §4); a kind's `attribute_schema` types its
items' properties and never refuses a key (§6.3a). An item's properties are set on
`/entities/item:<id>/properties`. Only this process reads or writes the tables (#5295):
the assistant's tools and the UI call these routes.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.entity_properties import KeyInUse
from scadbuddy.inventory.component import InventoryDep
from scadbuddy.inventory.models import (
    KIND_ID,
    InventoryItemPatch,
    Item,
    ItemInput,
    ItemKind,
    KindInput,
    KindPatch,
    Offer,
    OfferInput,
    OfferPatch,
    OfferPricePoint,
    PriceInput,
)
from scadbuddy.inventory.problems import (
    item_not_found,
    kind_not_found,
    offer_not_found,
    refused,
    supplier_not_found,
)
from scadbuddy.orders.actor import current_actor

router = APIRouter(tags=["inventory"])

KindId = Annotated[str, Path(pattern=KIND_ID, description="An item kind's id, such as `hardware`")]
#: A query text with no NUL byte, which Postgres `text` refuses (R-P2-16).
NO_NUL = r"^[^\x00]*$"


async def run[T](call: Callable[..., T], *args: Any) -> T:
    """`call(*args)` off the event loop; a store error becomes its inventory problem."""
    try:
        return await asyncio.to_thread(call, *args)
    except Exception as error:
        problem = refused(error)
        if problem is None:
            raise
        raise problem from None


def codes(*by_status: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    """A route's `responses`: each problem status with the codes it carries."""
    return {code: {"description": text} for code, text in by_status}


# -- kinds -------------------------------------------------------------------------------


class KindList(BaseModel):
    kinds: list[ItemKind]


class KindSchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind_id: str
    #: Merged down the kind tree: each ancestor's `properties`, the kind's own last.
    schema_: dict[str, Any] = Field(alias="schema")
    own_schema: dict[str, Any]
    keys_in_use: list[KeyInUse]


@router.get(
    "/inventory/kinds",
    response_model=KindList,
    summary="Item kinds",
    description=(
        "Every item kind by id: its parent, its tracking (`lot`, `unit`, `filament`, or "
        "`none` for a kind that is not counted), its unit, its own `attribute_schema` and "
        "its unarchived items' count. With `archived`, archived kinds too."
    ),
)
async def list_kinds(inventory: InventoryDep, archived: bool = False) -> KindList:
    return KindList(kinds=await run(inventory.kinds.list, archived))


@router.post(
    "/inventory/kinds",
    response_model=ItemKind,
    status_code=status.HTTP_201_CREATED,
    summary="Add an item kind",
    description=(
        "The id is `^[a-z][a-z0-9_]{0,39}$`. The `attribute_schema` documents and types "
        "the properties of the kind's items and never refuses a key: the subset that "
        "`PUT /entity-kinds/{kind}/property-schema` takes, else 422 `bad_property_schema` "
        "and no kind is made. A child kind inherits its parent's keys."
    ),
    responses=codes(
        (404, "`kind_not_found` (the parent)"),
        (409, "`kind_exists` or `kind_archived` (the parent)"),
        (422, "`bad_property_schema`"),
    ),
)
async def create_kind(body: KindInput, inventory: InventoryDep) -> ItemKind:
    return await run(inventory.kinds.create, body, current_actor())


@router.patch(
    "/inventory/kinds/{kind_id}",
    response_model=ItemKind,
    summary="Edit an item kind",
    description=(
        "Only what is sent changes; `parent_id` null makes the kind a root. A parent at or "
        "below the kind is 422 `kind_cycle`. A tracking change on a built-in kind or on one "
        "with items is 409 `kind_in_use`. A bad schema is 422 `bad_property_schema`, and "
        "the old schema stays."
    ),
    responses=codes(
        (404, "`kind_not_found`"),
        (409, "`kind_in_use` or `kind_archived` (the parent)"),
        (422, "`kind_cycle` or `bad_property_schema`"),
    ),
)
async def patch_kind(kind_id: KindId, body: KindPatch, inventory: InventoryDep) -> ItemKind:
    return await run(inventory.kinds.update, kind_id, body, current_actor())


@router.post(
    "/inventory/kinds/{kind_id}/archive",
    response_model=ItemKind,
    summary="Archive an item kind",
    description="409 `kind_in_use` while the kind has unarchived items.",
    responses=codes((404, "`kind_not_found`"), (409, "`kind_in_use`")),
)
async def archive_kind(kind_id: KindId, inventory: InventoryDep) -> ItemKind:
    return await run(inventory.kinds.archive, kind_id)


@router.post(
    "/inventory/kinds/{kind_id}/restore",
    response_model=ItemKind,
    summary="Restore an archived item kind",
    description="The kind is listed again and takes new items.",
    responses=codes((404, "`kind_not_found`")),
)
async def restore_kind(kind_id: KindId, inventory: InventoryDep) -> ItemKind:
    return await run(inventory.kinds.restore, kind_id)


@router.get(
    "/inventory/kinds/{kind_id}/schema",
    response_model=KindSchema,
    summary="An item kind's property schema",
    description=(
        "`schema` is the schema that types the kind's items' properties: each ancestor's "
        "`properties` merged down to the kind's own, so a child's key wins. `own_schema` "
        "is the kind's own `attribute_schema`. `keys_in_use` lists each property key the "
        "items of the kind and of its descendants have (archived ones too), with how many."
    ),
    responses=codes((404, "`kind_not_found`")),
)
async def get_kind_schema(kind_id: KindId, inventory: InventoryDep) -> KindSchema:
    found = await run(inventory.kinds.schema_of, kind_id)
    if found is None:
        raise kind_not_found(kind_id)
    kind, schema, keys = found
    return KindSchema(
        kind_id=kind_id,
        schema=schema,
        own_schema=kind.attribute_schema,
        keys_in_use=[KeyInUse(key=k, count=n) for k, n in keys],
    )


# -- items -------------------------------------------------------------------------------


class ItemList(BaseModel):
    items: list[Item]


@router.get(
    "/inventory/items",
    response_model=ItemList,
    summary="Inventory items",
    description=(
        "Items by name, with their stock: `on_hand` (the lots' sum, or the units in stock; "
        "0 for a kind that is not counted), `stock_value_minor` in minor units of the shop "
        "currency, `cost_unknown` and `low`, and `offer_count`, its unarchived offers. "
        "`kind` takes the kind with its descendants unless `descendants` is false; `q` "
        "matches the name, the manufacturer or any property value, in any case; `low` "
        "keeps those at or below their `low_stock`; "
        "`compatible` those that name the printer model code; `archived` adds archived "
        "items. Up to `limit` (1 to 5000, 1000 when absent)."
    ),
)
async def list_items(
    inventory: InventoryDep,
    kind: Annotated[str | None, Query(pattern=KIND_ID)] = None,
    descendants: bool = True,
    q: Annotated[str | None, Query(max_length=200, pattern=NO_NUL)] = None,
    low: bool = False,
    archived: bool = False,
    compatible: Annotated[str | None, Query(max_length=100, pattern=NO_NUL)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> ItemList:
    def listed() -> list[Item]:
        return inventory.items.list(
            kind=kind,
            descendants=descendants,
            q=q,
            low=low,
            include_archived=archived,
            compatible=compatible,
            limit=limit,
        )

    return ItemList(items=await run(listed))


@router.post(
    "/inventory/items",
    response_model=Item,
    status_code=status.HTTP_201_CREATED,
    summary="Add an inventory item",
    description=(
        "An item of one kind. Its attributes (a manufacturer part number, a thread size) "
        "are its properties, set on `/entities/item:<id>/properties` and typed by the "
        "kind's merged schema. Items of a `filament` kind wait for the filament phase "
        "(409 `filament_item_refused`)."
    ),
    responses=codes(
        (404, "`kind_not_found`"),
        (409, "`item_exists`, `kind_archived` or `filament_item_refused`"),
    ),
)
async def create_item(body: ItemInput, inventory: InventoryDep) -> Item:
    return await run(inventory.items.create, body, current_actor())


@router.get(
    "/inventory/items/{item_id}",
    response_model=Item,
    summary="An inventory item",
    description="The item with its stock figures and `offer_count`, archived or not.",
    responses=codes((404, "`item_not_found`")),
)
async def get_item(item_id: UUID, inventory: InventoryDep) -> Item:
    item = await run(inventory.items.get, item_id)
    if item is None:
        raise item_not_found(item_id)
    return item


@router.patch(
    "/inventory/items/{item_id}",
    response_model=Item,
    summary="Edit an inventory item",
    description=(
        "Only what is sent changes; `life_hours`, `life_months` and `saleable` null clear "
        "it. On an imported item each field that changes joins `edited_fields`, which a "
        "later import leaves alone. The kind does not change here."
    ),
    responses=codes((404, "`item_not_found`"), (409, "`item_exists`")),
)
async def patch_item(item_id: UUID, body: InventoryItemPatch, inventory: InventoryDep) -> Item:
    return await run(inventory.items.update, item_id, body, current_actor())


@router.post(
    "/inventory/items/{item_id}/archive",
    response_model=Item,
    summary="Archive an inventory item",
    description="It leaves the lists; its stock, moves and offers stay.",
    responses=codes((404, "`item_not_found`")),
)
async def archive_item(item_id: UUID, inventory: InventoryDep) -> Item:
    return await run(inventory.items.archive, item_id)


@router.post(
    "/inventory/items/{item_id}/restore",
    response_model=Item,
    summary="Restore an archived inventory item",
    description=(
        "409 `kind_archived` while its kind is archived, `item_exists` when an unarchived "
        "item of its kind took its name meanwhile."
    ),
    responses=codes((404, "`item_not_found`"), (409, "`kind_archived` or `item_exists`")),
)
async def restore_item(item_id: UUID, inventory: InventoryDep) -> Item:
    return await run(inventory.items.restore, item_id)


# -- offers ------------------------------------------------------------------------------


class OfferList(BaseModel):
    offers: list[Offer]


class PriceList(BaseModel):
    prices: list[OfferPricePoint]


@router.get(
    "/inventory/items/{item_id}/offers",
    response_model=OfferList,
    summary="An item's supplier offers",
    description=(
        "Each supplier's offer of the item by SKU, with its newest pack price. With "
        "`archived`, archived offers too."
    ),
    responses=codes((404, "`item_not_found`")),
)
async def list_item_offers(
    item_id: UUID, inventory: InventoryDep, archived: bool = False
) -> OfferList:
    if await run(inventory.items.get, item_id) is None:
        raise item_not_found(item_id)
    return OfferList(offers=await run(inventory.offers.for_item, item_id, archived))


@router.get(
    "/inventory/suppliers/{supplier_id}/offers",
    response_model=OfferList,
    summary="A supplier's offers",
    description=(
        "Every item the supplier offers, by SKU, with its newest pack price. One SKU may "
        "cover several items (a bundle). With `archived`, archived offers too."
    ),
    responses=codes((404, "`supplier_not_found`")),
)
async def list_supplier_offers(
    supplier_id: UUID, inventory: InventoryDep, archived: bool = False
) -> OfferList:
    if not await run(inventory.offers.supplier_exists, supplier_id):
        raise supplier_not_found(supplier_id)
    return OfferList(offers=await run(inventory.offers.for_supplier, supplier_id, archived))


@router.post(
    "/inventory/offers",
    response_model=Offer,
    status_code=status.HTTP_201_CREATED,
    summary="Add a supplier offer",
    description=(
        "One item at one supplier, under the supplier's SKU and pack. A SKU is unique per "
        "supplier and item among the unarchived, in any case: the same SKU for two items "
        "(a bundle) is allowed, twice for one item is 409 `offer_exists`. Offers are for "
        "items that are not filament until the filament phase. Nothing here reaches "
        "Bambuddy."
    ),
    responses=codes(
        (404, "`supplier_not_found` or `item_not_found` (unknown or archived)"),
        (409, "`offer_exists` or `filament_item_refused`"),
    ),
)
async def create_offer(body: OfferInput, inventory: InventoryDep) -> Offer:
    return await run(inventory.offers.create, body, current_actor())


@router.patch(
    "/inventory/offers/{offer_id}",
    response_model=Offer,
    summary="Edit a supplier offer",
    description="Only what is sent changes; `url` or `note` null clears it.",
    responses=codes((404, "`offer_not_found`"), (409, "`offer_exists`")),
)
async def patch_offer(offer_id: UUID, body: OfferPatch, inventory: InventoryDep) -> Offer:
    return await run(inventory.offers.update, offer_id, body, current_actor())


@router.post(
    "/inventory/offers/{offer_id}/archive",
    response_model=Offer,
    summary="Archive a supplier offer",
    description="Its SKU is free again for the item at the supplier; its prices stay.",
    responses=codes((404, "`offer_not_found`")),
)
async def archive_offer(offer_id: UUID, inventory: InventoryDep) -> Offer:
    return await run(inventory.offers.archive, offer_id)


@router.get(
    "/inventory/offers/{offer_id}/prices",
    response_model=PriceList,
    summary="A supplier offer's pack prices",
    description="Newest first: by date, then by when each was written.",
    responses=codes((404, "`offer_not_found`")),
)
async def list_offer_prices(offer_id: UUID, inventory: InventoryDep) -> PriceList:
    if await run(inventory.offers.get, offer_id) is None:
        raise offer_not_found(offer_id)
    return PriceList(prices=await run(inventory.offers.prices, offer_id))


@router.post(
    "/inventory/offers/{offer_id}/prices",
    response_model=OfferPricePoint,
    status_code=status.HTTP_201_CREATED,
    summary="Add a pack price to a supplier offer",
    description=(
        "A person's price of one pack (`manual`), in any ISO currency: the shop's "
        "`orders_currency` when `currency` is absent; today when `on_date` is absent."
    ),
    responses=codes((404, "`offer_not_found`")),
)
async def add_offer_price(
    offer_id: UUID, body: PriceInput, inventory: InventoryDep
) -> OfferPricePoint:
    return await run(inventory.offers.add_price, offer_id, body)
