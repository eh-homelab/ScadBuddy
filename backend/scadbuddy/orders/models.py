"""The orders feature's wire and row shapes (#4249, spec 2026-10-18 §3).

Money (§3.6, Ruling 11): every ``*_minor`` field is an ``int`` of minor units (cents,
pence) in the order's ``currency``, a ``bigint`` in Postgres; nothing here is a float.
``Money`` names that convention once.

No body here carries an author, a principal or a status the store derives (Ruling 12):
``created_by`` and an event's ``by`` are an :class:`Actor` the routes read from the
author header (``core/authorship.py``), never from a request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.library.libraries import ModelLibrary

#: Integer minor units in the order's currency (§3.6).
Money = int

OrderStatus = Literal[
    "draft", "quoted", "accepted", "in_production", "ready", "shipped", "closed", "cancelled"
]
#: Every §3.5 state: `queued`, `printing` and `printed` wait for the print path (phase
#: 3, plan Ruling 4); phase 1 reaches the others.
ItemStatus = Literal[
    "draft",
    "quoted",
    "approved",
    "queued",
    "printing",
    "printed",
    "fulfilled",
    "failed",
    "cancelled",
]
ContactKind = Literal[
    "phone", "email", "instagram", "facebook", "tiktok", "etsy", "website", "other"
]
ChargeKind = Literal["shipping", "discount", "fee", "tax"]
QuoteSource = Literal["slice", "manual"]
QuoteLineKind = Literal["filament", "machine_time", "setup", "labour", "markup", "manual"]
Outcome = Literal["succeeded", "failed", "partial", "unknown"]

#: The ``#RRGGBB`` of a colour slot as rendered (§3.3).
Colour = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
#: ISO 3166-1 alpha-2 (§3.1); the shape only, as the currency's is.
Country = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]


class Actor(BaseModel):
    """Who did it: the browser, or the agent principal (and session) a request was
    authored as, exactly as ``api/flows.py`` ``_started_by`` reads it."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["browser", "agent"]
    principal: str | None = None
    session: str | None = None


# -- customers (§3.1) -------------------------------------------------------------------


class ContactInput(BaseModel):
    """A contact point as a body carries it. With an ``id`` the row is updated in place,
    without one it is inserted; rows the body leaves out are deleted (Ruling 11)."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    kind: ContactKind
    value: str = Field(min_length=1, max_length=500)
    label: str | None = Field(default=None, max_length=100)
    primary: bool = False


class Contact(ContactInput):
    id: str
    position: int


class AddressInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    label: str | None = Field(default=None, max_length=100)
    recipient: str = Field(min_length=1, max_length=200)
    lines: list[str] = Field(default_factory=list, max_length=6)
    locality: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: Country | None = None
    phone: str | None = None
    default_shipping: bool = False
    default_billing: bool = False


class Address(AddressInput):
    id: str
    position: int


class AddressSnapshot(AddressInput):
    """An address as copied onto an order at accept, or onto a shipment (Ruling 11):
    the row's fields with its id, so a later edit to the customer never moves it."""


class Customer(BaseModel):
    id: str
    display_name: str
    company: str | None
    notes: str
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    contacts: list[Contact]
    addresses: list[Address]


# -- orders (§3.2) ----------------------------------------------------------------------


class ChargeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ChargeKind
    label: str = Field(min_length=1, max_length=200)
    #: Signed: a discount is negative.
    amount_minor: Money


class Charge(ChargeInput):
    id: str
    order_id: str
    position: int


class Payment(BaseModel):
    id: str
    order_id: str
    amount_minor: Money
    method: str | None
    reference: str | None
    received_at: datetime | None
    notes: str
    created_at: datetime


class OrderEvent(BaseModel):
    """One line of the order's audit line (§3.2, Ruling 12)."""

    id: int
    order_id: str
    at: datetime
    by: Actor
    kind: str
    detail: dict[str, Any]


class Order(BaseModel):
    id: str
    number: str
    customer_id: str
    status: OrderStatus
    currency: str
    on_hold: bool
    #: The customer's rows an open order follows (Ruling 11)...
    shipping_address_id: str | None
    billing_address_id: str | None
    #: ...and the copies taken at accept, which a later customer edit never moves.
    shipping_address: AddressSnapshot | None
    billing_address: AddressSnapshot | None
    notes: str
    due_at: datetime | None
    accepted_at: datetime | None
    closed_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    created_by: Actor


class OrderFilter(BaseModel):
    """What ``GET /orders`` narrows by (§4)."""

    status: OrderStatus | None = None
    customer_id: str | None = None
    #: Matched against the number, the notes and the customer's name.
    q: str | None = None
    since: datetime | None = None
    until: datetime | None = None


# -- line items (§3.3) ------------------------------------------------------------------


class Reference(BaseModel):
    """The one way a line item names what to make (§3.3, Ruling 1): a commit, never
    HEAD, with the normalised inputs and the library commits a render at it resolves."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    inputs: dict[str, Any]
    libraries: list[ModelLibrary] = Field(default_factory=list)
    #: A copy, for display after a delete.
    template_name: str
    #: ``render_key`` of F1, or ``inputs_key`` for a pipeline template.
    render_key: str


class ColourSlot(BaseModel):
    """One colour slot of the render; ``filament_id`` is the price book's (phase 2)."""

    model_config = ConfigDict(extra="forbid")

    slot: int = Field(ge=0)
    colour: Colour
    filament_id: str | None = None


class OrderItem(BaseModel):
    id: str
    order_id: str
    position: int
    status: ItemStatus
    quantity: int = Field(ge=1)
    title: str
    notes: str
    reference: Reference
    colours: list[ColourSlot]
    output_id: str | None
    created_at: datetime
    updated_at: datetime


# -- quotes (§3.4) ----------------------------------------------------------------------


class QuoteLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: QuoteLineKind
    label: str
    quantity: int = Field(ge=0)
    unit_minor: Money
    amount_minor: Money


class Quote(BaseModel):
    id: str
    order_item_id: str
    created_at: datetime
    source: QuoteSource
    accepted: bool
    #: Null for a `manual` quote (§3.4's last amendment); phase 2's slice fills them.
    sliced_sha256: str | None
    sliced_blob_key: str | None
    plates: list[dict[str, Any]]
    lines: list[QuoteLine]
    unit_cost_minor: Money
    unit_price_minor: Money
    quantity: int
    total_minor: Money
    created_by: Actor


# -- fulfilment (§6.1 by hand) and shipments (§6.3) ------------------------------------


class Fulfilment(BaseModel):
    id: str
    order_item_id: str
    attempt: int
    quantity: int
    outcome: Outcome
    failure_reason: str | None
    started_at: datetime | None
    finished_at: datetime | None
    notes: str
    accepted_at: datetime | None
    created_by: Actor
    created_at: datetime


class ShipmentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_item_id: str
    quantity: int = Field(ge=1)


class Shipment(BaseModel):
    id: str
    order_id: str
    carrier: str | None
    service: str | None
    tracking: str | None
    shipped_at: datetime | None
    address: AddressSnapshot | None
    cost_minor: Money | None
    notes: str
    created_at: datetime
    items: list[ShipmentItem]
