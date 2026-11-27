"""The inventory's wire and store models (#7067, spec §6.4)."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, TypedDict
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from scadbuddy.entities.links import MAX_URL, is_web_url
from scadbuddy.entities.properties import MAX_KEYS
from scadbuddy.orders.models import Actor

#: How a kind's items are counted; `none` is "not counted" (the owner's amendment to
#: §6.4 of 2026-11-23): offers and prices, never lots, units or moves.
Tracking = Literal["lot", "unit", "filament", "none"]
LifeBasis = Literal["none", "hours", "months"]

#: A kind's class (#6949 K1, design §3): each key a kind answers itself, null where it
#: leaves the key open for its children. `role` and `counting` are physical only (a
#: `durable` is always `unit`), `billing` service only. `scadbuddy-core` checks the
#: combinations; these are the values the columns take.
Nature = Literal["physical", "service"]
Role = Literal["consumable", "durable"]
Counting = Literal["lot", "unit"]
Billing = Literal["term", "hourly", "flat"]

#: The column CHECKs of `item_kinds` (migration `20261124T0323Z_inventory_core.sql`).
KIND_ID = r"^[a-z][a-z0-9_]{0,39}$"
UOM = r"^[a-z]{1,8}$"
#: The id of a kind that ScadBuddy makes: `k` and the kind's number (#6949, #6955).
MADE_KIND_ID = re.compile(r"k[0-9]+")
#: The longest `kind:ref` a use or a status change names.
MAX_ENTITY_REF = 200


def _no_nul(text: str) -> str:
    if "\x00" in text:
        raise ValueError("a text holds no NUL byte")
    return text


#: Every free text of an inventory input: Postgres `text` takes no NUL byte (R-P2-16).
Text = Annotated[str, AfterValidator(_no_nul)]


def empty_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


class StepConf(TypedDict, total=False):
    """One step's setting on a layer (design §4.2): a handler, its parameters, or both. A
    layer that names a new handler drops the old parameters; one that sets only `params`
    merges them."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[misc]

    handler: Annotated[str, Field(min_length=1, max_length=80)]
    params: dict[str, Any]


#: A layer's settings, keyed by step (`receive`, `consume`, `count`, `move`, `retire`,
#: `cost`, `life`, `bill`, `label`, `sync`). `scadbuddy-core` checks the steps and the
#: handlers (422 `steps_invalid`).
Steps = dict[Annotated[str, Field(max_length=40)], StepConf]


class ItemKind(BaseModel):
    id: str
    name: str
    parent_id: str | None
    #: Read from the class and the special type since K1 (R-K1-6): `filament` for a
    #: filament spool, `none` for a service, else the counting (`lot` while it is open).
    tracking: Tracking
    uom: str
    attribute_schema: dict[str, Any]
    #: Read from `steps.life` since K1: `hours`, `months` or `none`.
    life_basis: LifeBasis
    saleable: bool
    builtin: bool
    archived_at: datetime | None
    #: Its unarchived items, not its descendants'.
    item_count: int
    #: The kind's number from `item_kinds_number_seq`, and its short code `K-<number>`
    #: (#6955, the owner's decision of 2026-11-27).
    number: int
    code: str
    #: The class keys the kind answers itself (C22); null leaves the key to its children.
    nature: Nature | None = None
    role: Role | None = None
    counting: Counting | None = None
    billing: Billing | None = None
    special_type: str | None = None
    #: The kind's own steps, not its parents'.
    steps: Steps = Field(default_factory=dict)
    #: The steps whose handler lacks a required parameter (R-K1-8): a call of one is 409
    #: `step_incomplete`.
    incomplete_steps: list[str] = Field(default_factory=list)


class KindInput(BaseModel):
    """A new kind. Its class keys narrow its parent's (design §3); `tracking` and
    `life_basis` are the phase 2 inputs, still taken for one release (R-K1-6): a tracking
    sets the class keys its rule gives that the parents leave open, and a life basis names
    the `steps.life` handler. A field that is not sent is not sent to `scadbuddy-core`."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(
        default=None,
        pattern=KIND_ID,
        description=(
            "Optional: with none, the kind is `k` and its number. Not `k` and digits only: "
            "that id is a made kind's, `k` and its number."
        ),
    )
    name: Text = Field(min_length=1, max_length=100)
    parent_id: str | None = Field(default=None, pattern=KIND_ID)
    tracking: Tracking | None = None
    uom: str = Field(default="each", pattern=UOM)
    attribute_schema: dict[str, Any] = Field(default_factory=empty_schema)
    life_basis: LifeBasis = "none"
    saleable: bool = False
    nature: Nature | None = None
    role: Role | None = None
    counting: Counting | None = None
    billing: Billing | None = None
    special_type: str | None = Field(default=None, pattern=KIND_ID)
    steps: Steps | None = None

    @field_validator("id")
    @classmethod
    def _not_a_made_id(cls, value: str | None) -> str | None:
        # `item_kinds_made_id_check` (migration `20261126T0218Z_inventory_class.sql`).
        if value is not None and MADE_KIND_ID.fullmatch(value):
            raise ValueError("an id of k and digits is a made kind's id: k and its number")
        return value


class KindPatch(BaseModel):
    """The fields to change; an absent field keeps its value. `parent_id` null makes the
    kind a root; a class key or `special_type` null opens it; `steps` replaces the kind's
    own steps."""

    model_config = ConfigDict(extra="forbid")

    name: Text | None = Field(default=None, min_length=1, max_length=100)
    parent_id: str | None = Field(default=None, pattern=KIND_ID)
    tracking: Tracking | None = None
    uom: str | None = Field(default=None, pattern=UOM)
    attribute_schema: dict[str, Any] | None = None
    life_basis: LifeBasis | None = None
    saleable: bool | None = None
    nature: Nature | None = None
    role: Role | None = None
    counting: Counting | None = None
    billing: Billing | None = None
    special_type: str | None = Field(default=None, pattern=KIND_ID)
    steps: Steps | None = None


class _Decimals(BaseModel):
    """Decimals on the wire as plain strings with their fraction (as `orders/parts.py`
    `InventoryPart` writes `unit_cost_minor`)."""

    @staticmethod
    def _decimal(value: Decimal | None) -> str | None:
        return None if value is None else format(value.normalize(), "f")


class ItemClass(BaseModel):
    """An item's class, resolved down its kind's chain (design §3); null is a key no kind
    answered."""

    nature: Nature | None = None
    role: Role | None = None
    counting: Counting | None = None
    billing: Billing | None = None


class Item(_Decimals):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    kind_id: str
    #: The kind's ancestors and the kind, root first.
    kind_path: list[str]
    tracking: Tracking
    uom: str
    name: str
    manufacturer: str
    compatible_with: list[str]
    low_stock: Decimal
    life_hours: Decimal | None
    life_months: int | None
    #: The item's own choice; null takes the kind's.
    saleable: bool | None
    effective_saleable: bool
    source: str
    source_ref: str | None
    edited_fields: list[str]
    note: str
    archived_at: datetime | None
    #: The lots' sum, or the count of units in stock; 0 for a kind that is not counted.
    on_hand: Decimal
    #: The lots with stock on hand at their cost, plus the units in stock at theirs (minor
    #: units of the shop currency).
    stock_value_minor: Decimal
    #: Some lot with stock on hand, or some unit in stock, has no known cost.
    cost_unknown: bool
    low: bool
    #: Its unarchived supplier offers (R-P2-37).
    offer_count: int
    #: The item's own steps (design §4.2 layer 4).
    steps: Steps = Field(default_factory=dict)
    #: The steps whose handler lacks a required parameter (R-K1-8).
    incomplete_steps: list[str] = Field(default_factory=list)
    class_: ItemClass = Field(default_factory=ItemClass, alias="class")
    special_type: str | None = None
    #: The keys the special type and the kinds' schemas require that the item has no
    #: value for, read on every read (Task 10); empty for an archived item (R-K1-33).
    missing_properties: list[str] = Field(default_factory=list)
    #: The keys the kinds' schemas define whose stored value does not fit, sorted; empty
    #: for an archived item (R-K1-33).
    invalid_properties: list[str] = Field(default_factory=list)

    @field_serializer("low_stock", "life_hours", "on_hand", "stock_value_minor")
    def _decimals(self, value: Decimal | None) -> str | None:
        return self._decimal(value)


#: A printer model code an item fits (`compatible_with`), as the item list's `compatible`
#: filter takes it.
ModelCode = Annotated[Text, Field(min_length=1, max_length=100)]
MAX_COMPATIBLE = 100
#: A hundred years.
MAX_LIFE_MONTHS = 1200
#: The item's properties, written with it in one transaction (R-K1-33): a key set to null
#: is removed. At most `MAX_KEYS` keys, as `PropertyPatch.set`; Go checks the keys and the
#: values (422 `bad_property_value`).
ItemProperties = Annotated[dict[str, JsonValue], Field(max_length=MAX_KEYS)]


class ItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind_id: str = Field(pattern=KIND_ID)
    name: Text = Field(min_length=1, max_length=200)
    manufacturer: Text = Field(default="", max_length=200)
    compatible_with: list[ModelCode] = Field(default_factory=list, max_length=MAX_COMPATIBLE)
    low_stock: Decimal = Field(default=Decimal(0), ge=0, max_digits=20, decimal_places=6)
    life_hours: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    life_months: int | None = Field(default=None, gt=0, le=MAX_LIFE_MONTHS)
    saleable: bool | None = None
    source: Text = Field(default="manual", min_length=1, max_length=100)
    #: A blank one is stored as null.
    source_ref: Text | None = Field(default=None, max_length=200)
    note: Text = Field(default="", max_length=4000)
    steps: Steps | None = None
    properties: ItemProperties | None = None


class InventoryItemPatch(BaseModel):
    """The fields a person changes; an absent field keeps its value. `life_hours`,
    `life_months` and `saleable` null clear it; `steps` replaces the item's own steps.
    `properties` is a patch of the item's properties; null sends no patch."""

    model_config = ConfigDict(extra="forbid")

    name: Text | None = Field(default=None, min_length=1, max_length=200)
    manufacturer: Text | None = Field(default=None, max_length=200)
    compatible_with: list[ModelCode] | None = Field(default=None, max_length=MAX_COMPATIBLE)
    low_stock: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=6)
    life_hours: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    life_months: int | None = Field(default=None, gt=0, le=MAX_LIFE_MONTHS)
    saleable: bool | None = None
    note: Text | None = Field(default=None, max_length=4000)
    steps: Steps | None = None
    properties: ItemProperties | None = None


#: The ISO code of `offer_price_points.currency`.
CURRENCY = r"^[A-Z]{3}$"


def _blank_none(text: str | None) -> str | None:
    return None if text is None or not text.strip() else text


def _plain(value: Decimal) -> str:
    """A decimal with no trailing zeros, as `_Decimals` writes one (R-P2-40): `"5.99"`."""
    return format(value.normalize(), "f")


class OfferPricePoint(BaseModel):
    id: UUID
    #: The price of one pack, in `currency`.
    price: Decimal
    currency: str
    on_date: date
    source: Literal["invoice", "manual"]
    invoice_line_id: UUID | None

    @field_serializer("price")
    def _price(self, value: Decimal) -> str:
        return _plain(value)


class Offer(BaseModel):
    id: UUID
    supplier_id: UUID
    supplier_name: str
    item_id: UUID
    item_name: str
    sku: str
    url: str | None
    pack_size: Decimal
    pack_uom: str
    note: str | None
    archived_at: datetime | None
    #: The newest price point by date.
    latest_price: OfferPricePoint | None

    @field_serializer("pack_size")
    def _pack_size(self, value: Decimal) -> str:
        return _plain(value)


class OfferInput(BaseModel):
    """A blank `url` is stored as null."""

    model_config = ConfigDict(extra="forbid")

    supplier_id: UUID
    item_id: UUID
    sku: Text = Field(min_length=1, max_length=100)
    url: Text | None = None
    pack_size: Decimal = Field(default=Decimal(1), gt=0, max_digits=20, decimal_places=6)
    pack_uom: Text = Field(default="each", min_length=1, max_length=100)
    note: Text | None = Field(default=None, max_length=4000)

    @field_validator("pack_uom")
    @classmethod
    def _pack_uom(cls, pack_uom: str) -> str:
        return _stripped(pack_uom, "a pack unit")

    @field_validator("url")
    @classmethod
    def _url(cls, url: str | None) -> str | None:
        return _checked_url(url)

    @field_validator("sku")
    @classmethod
    def _sku(cls, sku: str) -> str:
        return _checked_sku(sku)


class OfferPatch(BaseModel):
    """The fields to change; an absent field keeps its value. `url` or `note` null or
    blank clears it; another null keeps the value."""

    model_config = ConfigDict(extra="forbid")

    sku: Text | None = Field(default=None, min_length=1, max_length=100)
    url: Text | None = None
    pack_size: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=6)
    pack_uom: Text | None = Field(default=None, min_length=1, max_length=100)
    note: Text | None = Field(default=None, max_length=4000)

    @field_validator("pack_uom")
    @classmethod
    def _pack_uom(cls, pack_uom: str | None) -> str | None:
        return None if pack_uom is None else _stripped(pack_uom, "a pack unit")

    @field_validator("url")
    @classmethod
    def _url(cls, url: str | None) -> str | None:
        return _checked_url(url)

    @field_validator("sku")
    @classmethod
    def _sku(cls, sku: str | None) -> str | None:
        return None if sku is None else _checked_sku(sku)


class PriceInput(BaseModel):
    """A person's pack price: `manual`. With no `currency`, the shop's (`orders_currency`);
    with no `on_date`, today."""

    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(ge=0, max_digits=12, decimal_places=4)
    currency: str | None = Field(default=None, pattern=CURRENCY)
    on_date: date | None = None


def _checked_url(url: str | None) -> str | None:
    url = _blank_none(url)
    if url is None:
        return None
    url = url.strip()
    if len(url) > MAX_URL or not is_web_url(url):
        raise ValueError(f"a purchase link is an http or https URL with a host, up to {MAX_URL}")
    return url


def _stripped(text: str, what: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError(f"{what} is not blank")
    return text


def _checked_sku(sku: str) -> str:
    return _stripped(sku, "a SKU")


# -- stock (§6.4, §6.6) -------------------------------------------------------------------

#: Where a lot or a unit came from (`stock_lots.origin`); `manual` is a receive with no
#: invoice yet.
Origin = Literal["invoice_line", "production_run", "opening", "adjustment", "manual"]
UnitStatus = Literal["in_stock", "in_service", "retired", "scrapped", "sold"]
#: A unit's status as it reads: a service term `ended` too (`life.term`, design §4.5).
UnitState = Literal["in_stock", "in_service", "retired", "scrapped", "sold", "ended"]
MoveKind = Literal[
    "receive", "use", "return", "adjust", "produce", "transfer", "sell", "retire", "scrap"
]


class Lot(_Decimals):
    id: UUID
    item_id: UUID
    location_id: UUID | None
    received_qty: Decimal
    #: The sum of the lot's moves; below 0 after a shortfall (spec §6.6).
    on_hand: Decimal
    #: One unit's cost in minor units of the shop currency.
    unit_cost_minor: Decimal
    cost_unknown: bool
    #: The currency the lot was bought in, when it was not the shop's (R-7067-11,
    #: R-P2-12); null means the shop's. A cost set later keeps it.
    cost_currency: str | None
    #: One unit's cost as given in `cost_currency`, in its minor units (R-P2-11).
    foreign_unit_cost_minor: Decimal | None
    origin: Origin
    invoice_line_id: UUID | None
    production_run_id: UUID | None
    note: str
    received_at: datetime
    created_by: Actor

    @field_serializer("received_qty", "on_hand", "unit_cost_minor", "foreign_unit_cost_minor")
    def _decimals(self, value: Decimal | None) -> str | None:
        return self._decimal(value)


class Unit(_Decimals):
    id: UUID
    item_id: UUID
    serial: str | None
    name: str
    location_id: UUID | None
    status: UnitState
    installed_in_unit_id: UUID | None
    cost_minor: Decimal
    cost_unknown: bool
    origin: Origin
    invoice_line_id: UUID | None
    production_run_id: UUID | None
    in_service_at: datetime | None
    retired_at: datetime | None
    life_hours: Decimal | None
    life_months: int | None
    printer_id: UUID | None
    location_ref_id: UUID | None
    received_at: datetime
    created_by: Actor
    #: The unit's own parameters (design §4.2 layer 5).
    steps: Steps = Field(default_factory=dict)
    #: A service term's start and end.
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_serializer("cost_minor", "life_hours")
    def _decimals(self, value: Decimal | None) -> str | None:
        return self._decimal(value)


class Move(_Decimals):
    id: UUID
    at: datetime
    item_id: UUID
    lot_id: UUID | None
    unit_id: UUID | None
    kind: MoveKind
    #: Signed: below 0 takes stock.
    qty: Decimal
    #: The cost when the move was written (D14).
    unit_cost_minor: Decimal
    #: `round(qty * unit_cost_minor, 6)`.
    amount_minor: Decimal
    cost_unknown: bool
    #: A shortfall: taken past the stock on hand (spec §6.6).
    needs_review: bool
    reverses_move_id: UUID | None
    reason: str
    order_id: str | None
    order_item_id: str | None
    production_run_id: UUID | None
    print_run_id: str | None
    entity_id: UUID | None
    actor: Actor

    @field_serializer("qty", "unit_cost_minor", "amount_minor")
    def _decimals(self, value: Decimal) -> str | None:
        return self._decimal(value)


class ReceiveLot(BaseModel):
    """A receive with no invoice yet (`origin = manual`). The cost is `unit_cost_minor`
    or `total_cost_minor` (for the whole quantity), in `currency` (the shop's
    `orders_currency` when absent); with neither, or in another currency, the lot opens
    with its cost unknown (R-7067-11, D11). A cost in another currency is kept as
    `foreign_unit_cost_minor` (R-P2-11)."""

    model_config = ConfigDict(extra="forbid")

    qty: Decimal = Field(gt=0, max_digits=20, decimal_places=6)
    unit_cost_minor: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=6)
    total_cost_minor: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=6)
    currency: str | None = Field(default=None, pattern=CURRENCY)
    location_id: UUID | None = None
    note: Text = Field(default="", max_length=4000)
    received_at: datetime | None = None

    @model_validator(mode="after")
    def _one_cost(self) -> ReceiveLot:
        if self.unit_cost_minor is not None and self.total_cost_minor is not None:
            raise ValueError("give unit_cost_minor or total_cost_minor, not both")
        return self


class ReceiveUnits(BaseModel):
    """`count` units at `cost_minor` each, in the shop currency (unknown when absent).
    `serials`, when given, has one entry per unit; a blank one is stored as null."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=1, le=100)
    cost_minor: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=6)
    serials: list[Annotated[Text, Field(max_length=200)]] | None = None
    location_id: UUID | None = None
    name: Text = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _serials(self) -> ReceiveUnits:
        if self.serials is not None and len(self.serials) != self.count:
            raise ValueError("serials has one entry per unit")
        return self


class UseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qty: Decimal = Field(gt=0, max_digits=20, decimal_places=6)
    #: Taken first; FIFO for the rest (D2).
    lot_id: UUID | None = None
    #: What it was used on, `kind:ref` (a printer, say).
    entity: Text | None = Field(default=None, max_length=MAX_ENTITY_REF)
    reason: Text = Field(default="", max_length=4000)


class AdjustInput(BaseModel):
    """A count of one lot."""

    model_config = ConfigDict(extra="forbid")

    on_hand: Decimal = Field(ge=0, max_digits=20, decimal_places=6)
    note: Text = Field(min_length=1, max_length=4000)

    @field_validator("note")
    @classmethod
    def _note(cls, note: str) -> str:
        return _stripped(note, "a note")


class CountInput(BaseModel):
    """A count of the whole item."""

    model_config = ConfigDict(extra="forbid")

    on_hand: Decimal = Field(ge=0, max_digits=20, decimal_places=6)
    note: Text = Field(min_length=1, max_length=4000)

    @field_validator("note")
    @classmethod
    def _note(cls, note: str) -> str:
        return _stripped(note, "a note")


class LotPatch(BaseModel):
    """The fields to change; an absent field keeps its value. `unit_cost_minor` makes the
    lot's cost known now and moves no past move (D14); `location_id` null clears it."""

    model_config = ConfigDict(extra="forbid")

    unit_cost_minor: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=6)
    location_id: UUID | None = None
    note: Text | None = Field(default=None, max_length=4000)


class UnitPatch(BaseModel):
    """The fields to change; an absent field keeps its value. `serial` null or blank
    clears it, as `location_id` null does."""

    model_config = ConfigDict(extra="forbid")

    name: Text | None = Field(default=None, max_length=200)
    serial: Text | None = Field(default=None, max_length=200)
    location_id: UUID | None = None


class UnitStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: UnitStatus
    #: What it went into or to, `kind:ref` (the printer a nozzle went into).
    entity: Text | None = Field(default=None, max_length=MAX_ENTITY_REF)
    note: Text = Field(default="", max_length=4000)


class RecostedItem(_Decimals):
    model_config = ConfigDict(extra="forbid")

    order_id: str | None
    order_item_id: str
    #: The order item's parts cost as job cost reads it (`-sum(amount_minor)` of its
    #: known-cost `use` and `return` moves, `stock.parts_moved`), before and after.
    before_minor: Decimal
    after_minor: Decimal
    #: A `use` not given back was of unknown cost, before and after.
    before_unknown: bool = False
    after_unknown: bool = False

    @field_serializer("before_minor", "after_minor")
    def _decimals(self, value: Decimal) -> str | None:
        return self._decimal(value)


class RecostResult(_Decimals):
    #: The lot's item, and the lot's cost now (and whether it is unknown), which every
    #: re-costed move took.
    item_id: UUID
    unit_cost_minor: Decimal
    cost_unknown: bool
    #: The distinct unit costs the changed moves had before, lowest first.
    before_unit_costs_minor: list[Decimal]
    #: The moves whose snapshot changed.
    moves: int
    #: The order items whose parts cost or its unknown flag changed.
    orders: list[RecostedItem]

    @field_serializer("unit_cost_minor")
    def _cost(self, value: Decimal) -> str | None:
        return self._decimal(value)

    @field_serializer("before_unit_costs_minor")
    def _costs(self, values: list[Decimal]) -> list[str | None]:
        return [self._decimal(v) for v in values]


class MovedRows(BaseModel):
    """The stock rows a repoint moved, by id."""

    moves: list[UUID]
    units: list[UUID]


class Moved(BaseModel):
    """`inventory.Repoint`'s answer (Task 11): the rows it moved from the entity gone to
    the one kept. A repeat finds nothing to move."""

    moved: MovedRows
