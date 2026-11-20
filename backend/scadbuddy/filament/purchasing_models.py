"""The models of ScadBuddy's filament purchasing tables (#6266, epic #6260, spec §5.8): one
set per table of `migrations/20261117T2042Z_filament_suppliers.sql`.

The shapes follow `filament/models.py`: ``XCreate`` is what a person sends, ``XPatch``
changes only the fields sent, ``X`` is what a row reads back as. Money is `Decimal`, never
float, and goes over the wire as a string. A URL a person types is `Url`: ``http`` or
``https`` only, so a ``javascript:`` link never reaches an anchor.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from scadbuddy.core.problems import Problem
from scadbuddy.filament.colour import Rgba
from scadbuddy.filament.models import Name, Note, SpoolTypeCreate, Text, _In, _Patch, _Row


def _http_url(value: str) -> str:
    if len(value) > 500:
        raise ValueError("a URL has at most 500 characters")
    if any(c.isspace() or not c.isprintable() for c in value):
        raise ValueError("a URL has no spaces or control characters")
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError("a URL must start with http:// or https://")
    return value


Url = Annotated[str, AfterValidator(_http_url)]
#: A money amount: the column is `numeric(12,4)`, so a fifth decimal is refused, not rounded.
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=4)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
PackSize = Annotated[int, Field(gt=0, le=1000)]
Quantity = Annotated[int, Field(gt=0, le=1_000)]
_MONEY_LIMIT = Decimal(10) ** 8
Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Source = Literal["invoice", "manual"]
InvoiceStatus = Literal["draft", "confirmed"]
ShoppingStatus = Literal["pending", "purchased"]

# -- suppliers --------------------------------------------------------------------------


class SupplierCreate(_In):
    name: Name
    website: Url | None = None
    customer_number: Text | None = None
    note: Note | None = None


class SupplierPatch(_Patch):
    not_null = frozenset({"name"})
    name: Name | None = None
    website: Url | None = None
    customer_number: Text | None = None
    note: Note | None = None
    archived: bool | None = None


class Supplier(_Row):
    name: str
    website: str | None
    customer_number: str | None
    note: str | None
    archived_at: datetime | None
    product_count: int = 0


# -- products and price points ----------------------------------------------------------


class PricePointCreate(_In):
    """A manual price: per pack, with tax. ``currency`` defaults to the shop's and
    ``on_date`` to today."""

    price: Money
    currency: Currency | None = None
    on_date: date | None = None


class PricePoint(_Row):
    product_id: UUID
    price: Decimal
    currency: str
    on_date: date
    source: Source
    invoice_line_id: UUID | None
    price_per_kg: Decimal | None = None


class TypePricePoint(PricePoint):
    supplier_id: UUID
    supplier_name: str
    sku: str
    supplier_archived: bool = False
    product_archived: bool = False


class ProductCreate(_In):
    type_id: UUID
    sku: Sku
    product_url: Url | None = None
    pack_size: PackSize = 1
    note: Note | None = None


class ProductPatch(_Patch):
    not_null = frozenset({"type_id", "sku", "pack_size"})
    type_id: UUID | None = None
    sku: Sku | None = None
    product_url: Url | None = None
    pack_size: PackSize | None = None
    note: Note | None = None
    archived: bool | None = None


class SupplierProduct(_Row):
    supplier_id: UUID
    type_id: UUID
    sku: str
    product_url: str | None
    pack_size: int
    note: str | None
    archived_at: datetime | None
    newest_price: PricePoint | None = None


class PriceSheetRow(BaseModel):
    product: SupplierProduct
    type_code: str
    type_label: str
    newest: PricePoint | None
    previous: PricePoint | None
    change_pct: Decimal | None


class SupplierStats(BaseModel):
    supplier_id: UUID
    supplier_name: str
    spool_count: int
    remaining_g: float
    consumed_g: float
    spend: Decimal
    archived: bool = False


# -- invoices ---------------------------------------------------------------------------


class FileRef(BaseModel):
    name: str
    media_type: str
    size: int


class InvoiceLineIn(_In):
    """What a person (or the session) sends for a line. ``tax_basis``, ``line_total`` and
    ``allocated_cost`` are computed by the server and are not inputs."""

    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    sku: Sku | None = None
    quantity: Quantity
    unit_price: Money
    line_subtotal: Money | None = None
    line_tax: Money | None = None
    colour_code: Text | None = None
    type_id: UUID | None = None
    supplier_product_id: UUID | None = None
    proposed_type: SpoolTypeCreate | None = None
    update_spool_ids: Annotated[list[UUID], Field(max_length=1_000)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _within_bounds(self) -> InvoiceLineIn:
        if len(self.update_spool_ids) > self.quantity:
            raise ValueError("update_spool_ids names more spools than the line's quantity")
        subtotal = self.line_subtotal
        if subtotal is None:
            subtotal = self.quantity * self.unit_price
        if subtotal >= _MONEY_LIMIT:
            raise ValueError("the line total is out of range (at most 99,999,999.9999)")
        return self


class InvoiceLine(_Row):
    invoice_id: UUID
    position: int
    description: str
    sku: str | None
    quantity: int
    unit_price: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal
    allocated_cost: Decimal
    tax_basis: str
    colour_code: str | None
    type_id: UUID | None
    supplier_product_id: UUID | None
    proposed_type: SpoolTypeCreate | None
    update_spool_ids: list[UUID]
    unit_cost: Decimal | None = None
    spool_ids: list[UUID] = Field(default_factory=list)


class SpoolBrief(BaseModel):
    """A spool as a line's candidate: what a person or the session needs to tell it
    from another one (#6267). Values are the effective ones (own override over the type's).
    ``archived``: the spool is used up and archived; a line may still fill it (#6707)."""

    id: UUID
    number: int
    type_id: UUID | None
    colour_name: str | None
    rgba: Rgba | None
    cost_per_kg: Decimal | None
    grams_left: float | None
    #: The invoice line the spool was bought on. A costed spool with none may be filled,
    #: and the line's cost replaces its own; one with a line is refused at confirm (#6700).
    invoice_line_id: UUID | None = None
    archived: bool = False


class LineCandidates(BaseModel):
    """The spools, archived ones too (#6707), a line could update instead of making new ones
    (#6267): those with
    the line's material and exact RGBA, split by whether they have a cost yet. A ``costed``
    spool with no ``invoice_line_id`` may be filled; its cost is then replaced (#6700)."""

    position: int
    uncosted: list[SpoolBrief]
    costed: list[SpoolBrief]


class InvoiceConflict(Problem):
    """The 409 of an invoice write: ``existing_invoice_id`` names the invoice that has the
    number; it is absent when the invoice is confirmed (frozen)."""

    existing_invoice_id: UUID | None = None


class InvoiceCreate(_In):
    supplier_id: UUID
    number: Sku | None = None
    invoice_date: date
    currency: Currency
    subtotal: Money
    shipping: Money = Decimal(0)
    tax: Money = Decimal(0)
    total: Money
    note: Note | None = None
    import_run_id: Text | None = None
    lines: list[InvoiceLineIn] = Field(default_factory=list)


class InvoicePatch(_Patch):
    not_null = frozenset(
        {"supplier_id", "invoice_date", "currency", "subtotal", "shipping", "tax", "total"}
    )
    supplier_id: UUID | None = None
    number: Sku | None = None
    invoice_date: date | None = None
    currency: Currency | None = None
    subtotal: Money | None = None
    shipping: Money | None = None
    tax: Money | None = None
    total: Money | None = None
    note: Note | None = None
    import_run_id: Text | None = None
    # Not a column: the write applies only while the invoice's `import_run_id` is this value
    # (`null` for none), else 409. How the import claims an invoice without a lock (#6267).
    if_import_run_id: Text | None = None


class Invoice(_Row):
    supplier_id: UUID
    number: str | None
    invoice_date: date
    currency: str
    subtotal: Decimal
    shipping: Decimal
    tax: Decimal
    total: Decimal
    status: InvoiceStatus
    import_run_id: str | None
    confirmed_at: datetime | None
    note: str | None
    minor_exponent: int = 2
    difference: Decimal | None = None
    reconciles: bool | None = None
    line_count: int = 0
    spool_count: int = 0
    file: FileRef | None = None


class InvoiceDetail(Invoice):
    lines: list[InvoiceLine] = Field(default_factory=list)


# -- the shopping list ------------------------------------------------------------------


class ShoppingItemCreate(_In):
    type_id: UUID | None = None
    material: Text | None = None
    subtype: Text | None = None
    brand: Text | None = None
    colour_name: Text | None = None
    quantity_spools: Annotated[int, Field(gt=0, le=1000)] = 1
    note: Note | None = None

    @model_validator(mode="after")
    def _material_or_type(self) -> ShoppingItemCreate:
        if self.type_id is None and not self.material:
            raise ValueError("material is required when no type_id is given")
        return self


class ShoppingItemPatch(_Patch):
    not_null = frozenset({"quantity_spools", "status"})
    quantity_spools: Annotated[int, Field(gt=0, le=1000)] | None = None
    note: Note | None = None
    status: ShoppingStatus | None = None


class ShoppingItem(_Row):
    type_id: UUID | None
    material: str
    subtype: str | None
    brand: str | None
    colour_name: str | None
    quantity_spools: int
    note: str | None
    status: ShoppingStatus
    purchased_at: datetime | None
