"""The inventory's wire and store models (#7067, spec §6.4)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scadbuddy.entities.links import MAX_URL, is_web_url

#: How a kind's items are counted; `none` is "not counted" (the owner's amendment to
#: §6.4 of 2026-11-23): offers and prices, never lots, units or moves.
Tracking = Literal["lot", "unit", "filament", "none"]
LifeBasis = Literal["none", "hours", "months"]

#: The column CHECKs of `item_kinds` (migration `20261124T0323Z_inventory_core.sql`).
KIND_ID = r"^[a-z][a-z0-9_]{0,39}$"
UOM = r"^[a-z]{1,8}$"


def empty_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


class ItemKind(BaseModel):
    id: str
    name: str
    parent_id: str | None
    tracking: Tracking
    uom: str
    attribute_schema: dict[str, Any]
    life_basis: LifeBasis
    saleable: bool
    builtin: bool
    archived_at: datetime | None
    #: Its unarchived items, not its descendants'.
    item_count: int


class KindInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=KIND_ID)
    name: str = Field(min_length=1, max_length=100)
    parent_id: str | None = None
    tracking: Tracking
    uom: str = Field(default="each", pattern=UOM)
    attribute_schema: dict[str, Any] = Field(default_factory=empty_schema)
    life_basis: LifeBasis = "none"
    saleable: bool = False


class KindPatch(BaseModel):
    """The fields to change; an absent field keeps its value. `parent_id` null makes the
    kind a root."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    parent_id: str | None = None
    tracking: Tracking | None = None
    uom: str | None = Field(default=None, pattern=UOM)
    attribute_schema: dict[str, Any] | None = None
    life_basis: LifeBasis | None = None
    saleable: bool | None = None


class Item(BaseModel):
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


class ItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind_id: str = Field(pattern=KIND_ID)
    name: str = Field(min_length=1, max_length=200)
    manufacturer: str = ""
    compatible_with: list[str] = Field(default_factory=list)
    low_stock: Decimal = Field(default=Decimal(0), ge=0, max_digits=20, decimal_places=6)
    life_hours: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    life_months: int | None = Field(default=None, gt=0)
    saleable: bool | None = None
    source: str = Field(default="manual", min_length=1)
    #: A blank one is stored as null.
    source_ref: str | None = None
    note: str = Field(default="", max_length=4000)


class ItemPatch(BaseModel):
    """The fields a person changes; an absent field keeps its value. `life_hours`,
    `life_months` and `saleable` null clear it."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    manufacturer: str | None = None
    compatible_with: list[str] | None = None
    low_stock: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=6)
    life_hours: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    life_months: int | None = Field(default=None, gt=0)
    saleable: bool | None = None
    note: str | None = Field(default=None, max_length=4000)


#: The ISO code of `offer_price_points.currency`.
CURRENCY = r"^[A-Z]{3}$"


def _blank_none(text: str | None) -> str | None:
    return None if text is None or not text.strip() else text


class PricePoint(BaseModel):
    id: UUID
    #: The price of one pack, in `currency`.
    price: Decimal
    currency: str
    on_date: date
    source: Literal["invoice", "manual"]
    invoice_line_id: UUID | None


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
    latest_price: PricePoint | None


class OfferInput(BaseModel):
    """A blank `url` is stored as null."""

    model_config = ConfigDict(extra="forbid")

    supplier_id: UUID
    item_id: UUID
    sku: str = Field(min_length=1, max_length=100)
    url: str | None = None
    pack_size: Decimal = Field(default=Decimal(1), gt=0, max_digits=20, decimal_places=6)
    pack_uom: str = Field(default="each", min_length=1)
    note: str | None = None

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

    sku: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = None
    pack_size: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=6)
    pack_uom: str | None = Field(default=None, min_length=1)
    note: str | None = None

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
