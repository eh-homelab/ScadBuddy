"""`/api/v1/inventory`: item kinds, items and supplier offers (#7067, spec
2026-11-21-inventory-and-purchasing-design.md §6.4 and §6.5). The stock routes are in
`api/inventory_stock.py`, the operation read in `api/inventory_operations.py`.

Since K1 (#6949, design §6.1a) scadbuddy-core owns the kinds and the items: each route here
parses its request, then reads Go's CRUD route or starts Go's activity on the queue
`inventory` (`inventory/core_client.py`), and answers Go's JSON, or 202 `OperationAccepted`
when the write takes longer than 15 s. Go's problems pass through with their codes. Kinds
are rows, never an allowlist in code (spec §4); a kind's `attribute_schema` types its
items' properties and never refuses a key (§6.3a). An item's properties are set on
`/entities/item:<id>/properties`. Offers stay this process's SQL until K3 (R-K1-17).

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from collections.abc import Callable, Mapping
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.entity_properties import KeyInUse, LimitQuery, SchemaHistory
from scadbuddy.entities.component import EntitiesDep
from scadbuddy.inventory.component import InventoryComponent, InventoryDep
from scadbuddy.inventory.core_client import operation_key
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
from scadbuddy.inventory.problems import offer_not_found, refused, supplier_not_found
from scadbuddy.operations.store import OperationAccepted
from scadbuddy.orders.actor import current_actor

router = APIRouter(tags=["inventory"])

KindId = Annotated[str, Path(pattern=KIND_ID, description="An item kind's id, such as `hardware`")]
#: A query text with no NUL byte, which Postgres `text` refuses (R-P2-16).
NO_NUL = r"^[^\x00]*$"
#: One deliberate press (R-K1-24): the same key again attaches to the write it started, or
#: reads its answer, never doing it twice. Scoped to who sends it. Without one, a request
#: is a write of its own.
IdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)
]
_ACCEPTED = (
    "The write takes longer than 15 s: `OperationAccepted`, to follow at "
    "`GET /inventory/operations/{id}`. It still runs; the same `Idempotency-Key` again "
    "answers 202 while it runs and its answer once it is done."
)
#: What any inventory write may answer besides its own codes.
_UNAVAILABLE = (503, "`inventory_unavailable`: scadbuddy-core or Temporal did not answer")


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


def write_codes(*by_status: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    """A write's `responses`: its codes, the 202 and the 503."""
    return {
        status.HTTP_202_ACCEPTED: {"model": OperationAccepted, "description": _ACCEPTED},
        **codes(*by_status, _UNAVAILABLE),
    }


async def forward(
    inventory: InventoryComponent,
    name: str,
    model: type[BaseModel],
    *,
    key: str | None,
    subject: str,
    params: Mapping[str, str] | None = None,
    body: BaseModel | Mapping[str, Any] | None = None,
) -> JSONResponse:
    """Go's `inventory.{name}` for this request: its answer as `model` at Go's status, or
    202 with the operation to follow."""
    by = current_actor()
    answer = await inventory.core.write(
        name, key=operation_key(by, key), by=by, subject=subject, params=params, body=body
    )
    if isinstance(answer, OperationAccepted):
        return JSONResponse(answer.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    shown = model.model_validate(answer.body).model_dump(mode="json", by_alias=True)
    return JSONResponse(shown, status_code=answer.status)


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
    #: The keys an item of the kind must have a value for (R-K1-33): the schema's
    #: `required` keys, then its special type's required properties (a printer's
    #: `model_code`), each once. An item save without one is 422
    #: `missing_required_properties`.
    item_required: list[str] = Field(default_factory=list)


@router.get(
    "/inventory/kinds",
    response_model=KindList,
    summary="Item kinds",
    description=(
        "Every item kind by id: its parent, its own class keys (`nature`, `role`, "
        "`counting`, `billing`; null leaves a key to its children), `special_type`, its own "
        "`steps` and `incomplete_steps`, its unit, its own `attribute_schema` and its "
        "unarchived items' count. `tracking` (`lot`, `unit`, `filament`, or `none` for a "
        "kind that is not counted) and `life_basis` are read from the class and the steps. "
        "With `archived`, archived kinds too."
    ),
    responses=codes(_UNAVAILABLE),
)
async def list_kinds(inventory: InventoryDep, archived: bool = False) -> KindList:
    return KindList.model_validate(
        await inventory.core.read("/kinds", params={"archived": archived})
    )


@router.post(
    "/inventory/kinds",
    response_model=ItemKind,
    status_code=status.HTTP_201_CREATED,
    summary="Add an item kind",
    description=(
        "The id is `^[a-z][a-z0-9_]{0,39}$` and optional: with none, the kind is `k` and "
        "its number (`k12`, code `K-12`). A typed id of `k` and digits is 422. The class "
        "keys narrow the parent's and never change one it answered; `steps` names the "
        "kind's own handlers. `tracking` and `life_basis` are still taken for one release: "
        "a tracking sets the class keys its rule gives, and a life basis names the `life` "
        "step's handler. The "
        "`attribute_schema` documents and types the properties of the kind's items and "
        "never refuses a key: the subset that `PUT /entity-kinds/{kind}/property-schema` "
        "takes, else 422 `bad_property_schema` and no kind is made. A child kind inherits "
        "its parent's keys."
    ),
    responses=write_codes(
        (404, "`kind_not_found` (the parent)"),
        (409, "`kind_exists` or `kind_archived` (the parent)"),
        (
            422,
            "`bad_property_schema`, `steps_invalid` (with `problems`), `class_changed`, "
            "`class_invalid` or `bad_input`",
        ),
    ),
)
async def create_kind(
    body: KindInput, inventory: InventoryDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    return await forward(
        inventory,
        "KindCreate",
        ItemKind,
        key=idempotency_key,
        subject=f"item_kind:{body.id}" if body.id else "item_kind",
        body=body,
    )


@router.patch(
    "/inventory/kinds/{kind_id}",
    response_model=ItemKind,
    summary="Edit an item kind",
    description=(
        "Only what is sent changes; `parent_id` null makes the kind a root, and a class key "
        "or `special_type` null leaves it open. `steps` replaces the kind's own steps. A "
        "parent at or below the kind is 422 `kind_cycle`; a change that changes the class "
        "of a kind with items is 422 `class_changed`; a class or special type change on a "
        "built-in kind is 409 `kind_in_use`. A bad schema is 422 `bad_property_schema`, and "
        "the old schema stays."
    ),
    responses=write_codes(
        (404, "`kind_not_found`"),
        (409, "`kind_in_use` or `kind_archived` (the parent)"),
        (
            422,
            "`kind_cycle`, `bad_property_schema`, `steps_invalid`, `class_changed`, "
            "`class_invalid` or `bad_input`",
        ),
    ),
)
async def patch_kind(
    kind_id: KindId,
    body: KindPatch,
    inventory: InventoryDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    return await forward(
        inventory,
        "KindUpdate",
        ItemKind,
        key=idempotency_key,
        subject=f"item_kind:{kind_id}",
        params={"kind_id": kind_id},
        body=body,
    )


@router.post(
    "/inventory/kinds/{kind_id}/archive",
    response_model=ItemKind,
    summary="Archive an item kind",
    description="409 `kind_in_use` while the kind has unarchived items.",
    responses=write_codes((404, "`kind_not_found`"), (409, "`kind_in_use`")),
)
async def archive_kind(
    kind_id: KindId, inventory: InventoryDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    return await forward(
        inventory,
        "KindArchive",
        ItemKind,
        key=idempotency_key,
        subject=f"item_kind:{kind_id}",
        params={"kind_id": kind_id},
    )


@router.post(
    "/inventory/kinds/{kind_id}/restore",
    response_model=ItemKind,
    summary="Restore an archived item kind",
    description="The kind is listed again and takes new items.",
    responses=write_codes((404, "`kind_not_found`")),
)
async def restore_kind(
    kind_id: KindId, inventory: InventoryDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    return await forward(
        inventory,
        "KindRestore",
        ItemKind,
        key=idempotency_key,
        subject=f"item_kind:{kind_id}",
        params={"kind_id": kind_id},
    )


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
    responses=codes((404, "`kind_not_found`"), _UNAVAILABLE),
)
async def get_kind_schema(kind_id: KindId, inventory: InventoryDep) -> KindSchema:
    return KindSchema.model_validate(await inventory.core.read(f"/kinds/{kind_id}/schema"))


@router.get(
    "/inventory/kinds/{kind_id}/schema/history",
    response_model=SchemaHistory,
    summary="The history of an item kind's property schema",
    description=(
        "Each write of the kind's `attribute_schema`, newest first, with the schema before "
        "and after it, its time and actor. The history subscriber writes a change about two "
        "seconds after its commit, so a change just made may not be listed yet. A kind with "
        "no history, or no such kind, has no changes."
    ),
    responses=codes((422, "a `limit` outside 1 to 500")),
)
async def get_kind_schema_history(
    kind_id: KindId, entities: EntitiesDep, limit: LimitQuery = 100
) -> SchemaHistory:
    changes = await asyncio.to_thread(
        entities.properties.schema_history, f"item_kind:{kind_id}", limit
    )
    return SchemaHistory(changes=changes)


# -- items -------------------------------------------------------------------------------

#: The refusals of an item create or save that its properties cause (R-K1-33), as the spool
#: routes of `api/filament.py` name them.
_PROPERTY_REFUSALS = (
    "`missing_required_properties` (with `missing` and `invalid` keys) when the item would "
    "be left without a required property or with a stored value that does not fit; "
    "`bad_property_value` when a sent value does not fit its key"
)


def _item_body(body: ItemInput | InventoryItemPatch) -> dict[str, Any]:
    """The fields the request set, as Go takes them. `properties: null` is no patch of the
    properties, so it is not sent: a patch of it alone stays empty, and Go checks nothing."""
    sent = body.model_dump(mode="json", exclude_unset=True)
    if "properties" in sent and sent["properties"] is None:
        del sent["properties"]
    return sent


class ItemList(BaseModel):
    items: list[Item]


@router.get(
    "/inventory/items",
    response_model=ItemList,
    summary="Inventory items",
    description=(
        "Items by name, with their stock: `on_hand` (the lots' sum, or the units in stock; "
        "0 for a kind that is not counted), `stock_value_minor` in minor units of the shop "
        "currency, `cost_unknown` and `low`, and `offer_count`, its unarchived offers; "
        "and their resolved `class`, `special_type`, own `steps`, `incomplete_steps` and "
        "`missing_properties`. "
        "`kind` takes the kind with its descendants unless `descendants` is false; `q` "
        "matches the name, the manufacturer or any property value, in any case; `low` "
        "keeps those at or below their `low_stock`; "
        "`compatible` those that name the printer model code; `archived` adds archived "
        "items. Up to `limit` (1 to 5000, 1000 when absent)."
    ),
    responses=codes(_UNAVAILABLE),
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
    params = {
        "kind": kind,
        "descendants": descendants,
        "q": q,
        "low": low,
        "archived": archived,
        "compatible": compatible,
        "limit": limit,
    }
    return ItemList.model_validate(await inventory.core.read("/items", params=params))


@router.post(
    "/inventory/items",
    response_model=Item,
    status_code=status.HTTP_201_CREATED,
    summary="Add an inventory item",
    description=(
        "An item of one kind. Its attributes (a manufacturer part number, a thread size) "
        "are its properties, typed by the kind's merged schema. `properties` sets them with "
        "the item, in one write; a key set to null is left out. The item must have a value "
        "for each key its kind requires (`GET /inventory/kinds/{kind_id}/schema` "
        "`item_required`, a printer's `model_code` among them), else 422 "
        + _PROPERTY_REFUSALS
        + ", and nothing is written. `steps` sets the item's own handlers and parameters. "
        "Items of a filament spool wait for the filament phase (409 `filament_item_refused`)."
    ),
    responses=write_codes(
        (404, "`kind_not_found`"),
        (409, "`item_exists`, `kind_archived` or `filament_item_refused`"),
        (
            422,
            "`steps_invalid`, `class_open`, `bad_input`, `missing_required_properties` or "
            "`bad_property_value`",
        ),
    ),
)
async def create_item(
    body: ItemInput, inventory: InventoryDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    return await forward(
        inventory,
        "ItemCreate",
        Item,
        key=idempotency_key,
        subject=f"item_kind:{body.kind_id}",
        body=_item_body(body),
    )


@router.get(
    "/inventory/items/{item_id}",
    response_model=Item,
    summary="An inventory item",
    description="The item with its stock figures and `offer_count`, archived or not.",
    responses=codes((404, "`item_not_found`"), _UNAVAILABLE),
)
async def get_item(item_id: UUID, inventory: InventoryDep) -> Item:
    return Item.model_validate(await inventory.core.read(f"/items/{item_id}"))


@router.patch(
    "/inventory/items/{item_id}",
    response_model=Item,
    summary="Edit an inventory item",
    description=(
        "Only what is sent changes; `life_hours`, `life_months` and `saleable` null clear "
        "it, and `steps` replaces the item's own steps. `properties` is a patch of the "
        "item's properties, written with the item: a key set to null is removed. A save "
        "that sends a field is checked as a create is: 422 "
        + _PROPERTY_REFUSALS
        + ", and nothing is written. A patch that sends no field is not checked. On an "
        "imported item each field that changes joins `edited_fields`, which a later import "
        "leaves alone. The kind does not change here."
    ),
    responses=write_codes(
        (404, "`item_not_found`"),
        (409, "`item_exists`"),
        (
            422,
            "`steps_invalid`, `class_open`, `bad_input`, `missing_required_properties` or "
            "`bad_property_value`",
        ),
    ),
)
async def patch_item(
    item_id: UUID,
    body: InventoryItemPatch,
    inventory: InventoryDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    return await forward(
        inventory,
        "ItemUpdate",
        Item,
        key=idempotency_key,
        subject=f"item:{item_id}",
        params={"item_id": str(item_id)},
        body=_item_body(body),
    )


@router.post(
    "/inventory/items/{item_id}/archive",
    response_model=Item,
    summary="Archive an inventory item",
    description="It leaves the lists; its stock, moves and offers stay.",
    responses=write_codes((404, "`item_not_found`")),
)
async def archive_item(
    item_id: UUID, inventory: InventoryDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    return await forward(
        inventory,
        "ItemArchive",
        Item,
        key=idempotency_key,
        subject=f"item:{item_id}",
        params={"item_id": str(item_id)},
    )


@router.post(
    "/inventory/items/{item_id}/restore",
    response_model=Item,
    summary="Restore an archived inventory item",
    description=(
        "409 `kind_archived` while its kind is archived, `item_exists` when an unarchived "
        "item of its kind took its name meanwhile."
    ),
    responses=write_codes((404, "`item_not_found`"), (409, "`kind_archived` or `item_exists`")),
)
async def restore_item(
    item_id: UUID, inventory: InventoryDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    return await forward(
        inventory,
        "ItemRestore",
        Item,
        key=idempotency_key,
        subject=f"item:{item_id}",
        params={"item_id": str(item_id)},
    )


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
    responses=codes((404, "`item_not_found`"), _UNAVAILABLE),
)
async def list_item_offers(
    item_id: UUID, inventory: InventoryDep, archived: bool = False
) -> OfferList:
    # The item's existence is Go's to tell (404 `item_not_found` passes through).
    await inventory.core.read(f"/items/{item_id}")
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
