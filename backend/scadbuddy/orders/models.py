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
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, computed_field

from scadbuddy.core.settings import decimal_string
from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.orders.currency import minor_exponent

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
#: ``rounding`` is ``to_major``'s difference (#4250 Ruling 5), so the lines always add.
QuoteLineKind = Literal[
    "filament", "machine_time", "setup", "labour", "markup", "rounding", "manual"
]
Outcome = Literal["succeeded", "failed", "partial", "unknown"]

#: The ``#RRGGBB`` of a colour slot as rendered (§3.3).
Colour = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]


def _rate(value: str) -> str:
    return decimal_string(value, "the value")


def _places(places: int, below: int, *, positive: bool = False) -> AfterValidator:
    """A decimal string its column holds as it is: ``numeric(p, places)`` rounds a finer
    one silently and refuses one past ``below``, so both are refused here instead."""

    def check(value: str) -> str:
        text = decimal_string(value, "the value", positive=positive)
        number = Decimal(text)
        exponent = number.as_tuple().exponent
        if isinstance(exponent, int) and -exponent > places:
            raise ValueError(f"at most {places} decimal places")
        if number >= below:
            raise ValueError(f"must be less than {below}")
        return text

    return AfterValidator(check)


#: A non-negative decimal string (§3.6: a rate is never a float), as a policy holds one.
Rate = Annotated[str, AfterValidator(_rate)]
#: ``filaments.cost_per_kg``'s ``numeric(12,4)``.
CostPerKg = Annotated[str, _places(4, 10**8)]
#: ``filaments.density_g_cm3``'s ``numeric(6,3)``, above 0.
Density = Annotated[str, _places(3, 1000, positive=True)]
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minor_exponent(self) -> int:
        """The decimal places of ``currency``'s minor unit (#4250 Ruling 5): every
        ``*_minor`` of the order is ``amount * 10 ** minor_exponent``."""
        return minor_exponent(self.currency)


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
    #: The image attachment a person marked as what was actually delivered (§6.2), one
    #: of the item's own; None until one is marked, and again once it is removed.
    delivered_attachment_id: str | None = None
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
    #: Null for a `manual` quote (§3.4's last amendment); phase 2's slice fills them with
    #: the FIRST plate's file alone (one sliced file per plate, plan Ruling 3): every
    #: plate's own `blob_key` and `sha256` are in `plates`, never summed up here.
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


# -- attachments (§6.2, #4251) ----------------------------------------------------------

#: What a file is, from its bytes (``orders/attachments.py`` ``sniff``): an ``image`` or
#: ``video`` the browser can show, a ``document`` (a PDF), or ``other``, any file at all,
#: which is only ever downloaded.
AttachmentKind = Literal["image", "video", "document", "other"]
#: Why it is kept: the customer's ``reference`` (a sketch, a logo, a drawing), a print's
#: ``issue``, the ``finished`` object, the ``packed`` box, a ``timelapse``, or ``other``.
AttachmentRole = Literal["reference", "issue", "finished", "packed", "timelapse", "other"]
#: The longest caption, in characters, as a template's media (``library/media.py``).
MAX_ATTACHMENT_CAPTION = 1000


class Attachment(BaseModel):
    id: str
    order_id: str
    #: ``order:<id>`` or ``order_item:<id>``: what it is attached to.
    subject: str
    #: The line item it is attached to, or None for the order's own.
    item_id: str | None
    kind: AttachmentKind
    role: AttachmentRole
    #: The name it was sent with, for display and the download.
    filename: str
    #: What its bytes are (sniffed), or what its name says for ``other``.
    media_type: str
    size_bytes: int
    sha256: str
    caption: str
    created_at: datetime
    created_by: Actor


class AttachmentPatch(BaseModel):
    """`PATCH …/attachments/{id}`: the caption and the role; a field left out stays."""

    model_config = ConfigDict(extra="forbid")

    caption: str | None = Field(default=None, max_length=MAX_ATTACHMENT_CAPTION)
    role: AttachmentRole | None = None


class DeliveredBody(BaseModel):
    """`PUT …/items/{item}/delivered`: the item's image attachment that shows what was
    delivered, or None to clear the mark."""

    model_config = ConfigDict(extra="forbid")

    attachment_id: str | None


# -- the price book (§5.1, #4250) -----------------------------------------------------


class FilamentInput(BaseModel):
    """A filament record as a body carries it: what a kilogram costs (§5.1). Its
    identity is material, subtype, brand and colour, upper-cased (Ruling 4)."""

    model_config = ConfigDict(extra="forbid")

    material: str = Field(min_length=1, max_length=100)
    subtype: str | None = Field(default=None, max_length=100)
    brand: str | None = Field(default=None, max_length=100)
    colour_name: str | None = Field(default=None, max_length=100)
    colour: Colour
    #: In the shop's currency, per kilogram; a decimal string.
    cost_per_kg: CostPerKg
    #: For the rough estimate (§5.3); none gives no estimate.
    density_g_cm3: Density | None = None


class FilamentPatch(BaseModel):
    """`PATCH …/filaments/{id}`: only the fields sent change (``model_fields_set``);
    ``null`` clears ``subtype``, ``brand``, ``colour_name`` or ``density_g_cm3``."""

    model_config = ConfigDict(extra="forbid")

    material: str | None = Field(default=None, min_length=1, max_length=100)
    subtype: str | None = Field(default=None, max_length=100)
    brand: str | None = Field(default=None, max_length=100)
    colour_name: str | None = Field(default=None, max_length=100)
    colour: Colour | None = None
    cost_per_kg: CostPerKg | None = None
    density_g_cm3: Density | None = None


class Filament(BaseModel):
    """A filament record, its decimals as strings on the wire (§3.6)."""

    id: str
    material: str
    subtype: str | None
    brand: str | None
    colour_name: str | None
    colour: str
    cost_per_kg: str
    density_g_cm3: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


Rounding = Literal["to_minor_unit", "to_major"]


class PricingPolicy(BaseModel):
    """The policy a quote is priced under (§5.1), in the order's currency: decimal
    strings, each at least 0. ``markup_percent`` is a percentage ("15" is 15 %)."""

    model_config = ConfigDict(extra="forbid")

    machine_rate_per_hour: Rate = "0"
    #: Per plate.
    setup_fee: Rate = "0"
    labour_per_item: Rate = "0"
    markup_percent: Rate = "0"
    rounding: Rounding = "to_minor_unit"


class PriceBook(BaseModel):
    """The price book in force (§5.1): what the next quote is priced with."""

    #: ``orders_currency``: the next order's; a quote is priced in its own order's.
    currency: str
    minor_exponent: int
    policy: PricingPolicy
    #: The rough estimate's factor (§5.3), a decimal string.
    rough_estimate_factor: str
    #: The records not archived, by material, brand and colour.
    filaments: list[Filament]
