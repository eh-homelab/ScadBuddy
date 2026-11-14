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

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationInfo,
    computed_field,
    field_validator,
)

from scadbuddy.core.settings import decimal_string
from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.orders.currency import minor_exponent

#: Integer minor units in the order's currency (§3.6).
Money = int

OrderStatus = Literal[
    "draft", "quoted", "accepted", "in_production", "ready", "shipped", "closed", "cancelled"
]


def _listed(types: frozenset[str]) -> list[JsonValue]:
    """A set as the spec carries it, sorted so the export is stable."""
    return list[JsonValue](sorted(types))


#: The states an order's items and price may still change in (§4's `PATCH` rows).
OPEN_FOR_EDITS: frozenset[OrderStatus] = frozenset({"draft", "quoted"})
#: The states past acceptance an order still takes a new item in (#5349): an item added
#: then starts unpriced, and needs its own quote and that quote's acceptance.
TAKES_ITEMS: frozenset[OrderStatus] = frozenset({"accepted", "in_production", "ready"})
#: Every §3.5 state: `queued`, `printing` and `printed` are the print path's
#: (``order_item_print``, #4719), which the item follows from its run.
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
#: A ``slice`` quote is priced from the item's slice; ``manual`` is typed; ``archive``
#: (#4777) is priced from what a past print used, with no re-slice.
QuoteSource = Literal["slice", "manual", "archive"]
#: The sources whose plates hold measured grams and seconds, which job cost prices by.
MEASURED_SOURCES: frozenset[str] = frozenset({"slice", "archive"})
#: What an item is (#4954): a ``model`` names a template render (its ``reference``); a
#: ``custom`` item is a title, a quantity and a typed price with nothing behind it; a
#: ``library_file`` item (#5544) names a file of the library (its ``library_file``).
ItemKind = Literal["model", "custom", "request", "library_file"]
#: Which library a ``library_file`` item's file is in (#5544): Bambuddy's, or
#: ScadBuddy's own (``library_backend=native``). The file's id says which.
ItemLibrary = Literal["bambuddy", "native"]
#: When the customer approves a design (storefront spec §4): ``design_first`` waits for the
#: design to be accepted before the item is quoted or printed; ``together`` approves it
#: with the quote.
Approval = Literal["design_first", "together"]
#: A design revision's answer: the customer's, or ``superseded`` by a newer revision.
RevisionAnswer = Literal["accepted", "changes_requested", "superseded"]
#: Why an attempt was printed: the order's ``production`` run, or a ``test`` of a revision.
FulfilmentPurpose = Literal["production", "test"]
#: ``rounding`` is ``to_major``'s difference (#4250 Ruling 5), so the lines always add.
QuoteLineKind = Literal[
    "filament", "machine_time", "setup", "labour", "markup", "rounding", "manual"
]
Outcome = Literal["succeeded", "failed", "partial", "unknown"]

#: The ``#RRGGBB`` of a colour slot as rendered (§3.3).
Colour = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
#: The ``rgba`` alpha byte of a filament (#6209): 255 is opaque, below it translucent.
Alpha = Annotated[int, Field(ge=0, le=255)]
#: One of a multi-colour filament's stops, as Bambuddy's ``extra_colors`` holds them
#: (6 or 8 hex digits), upper-cased with a ``#``.
ColourStop = Annotated[
    str, Field(pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$"), AfterValidator(str.upper)
]
#: At most 8, as Bambuddy's ``MAX_EXTRA_COLOR_STOPS``.
ColourStops = Annotated[list[ColourStop], Field(max_length=8)]
#: Bambuddy's ``effect_type`` values (``ALLOWED_EFFECT_TYPES``, backend/app/schemas/spool.py
#: at https://github.com/maziggy/bambuddy/blob/1aa7bce166682fde929fec0d535bdaaa22685aad/backend/app/schemas/spool.py).
FilamentEffect = Literal[
    "sparkle",
    "wood",
    "marble",
    "glow",
    "matte",
    "silk",
    "galaxy",
    "rainbow",
    "metal",
    "translucent",
    "gradient",
    "dual-color",
    "tri-color",
    "multicolor",
]
#: Who set a filament's colour, alpha, stops and effect (#6209): the spool ``import``,
#: which sets them again at each import, or a person (``manual``), which it never does.
ColourSource = Literal["import", "manual"]


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
    authored as, exactly as ``api/flows.py`` ``_started_by`` reads it. A ``visitor`` is a
    storefront visitor (#5282), ``storefront_visitor:<row id>``, which only the CRUD side
    builds (``orders/storefront/visitors.py`` ``visitor_actor``). A ``customer`` is the
    customer of a submitted storefront order (#5283), ``customer:<customer id>`` with the
    visitor's or account's row as ``session`` (``orders/storefront/customers.py``
    ``customer_actor``)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["browser", "agent", "provider", "visitor", "customer"]
    principal: str | None = None
    session: str | None = None
    #: The flow run the agent's call was made from (#4252, spec §7.4), when it was.
    run_id: str | None = None


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


#: One field of an address a storefront visitor types (#5283).
_StoreLine = Annotated[str, Field(max_length=200)]


class SubmitAddress(BaseModel):
    """The address a storefront visitor gives at submit (#5283, spec §5.5): an address's
    fields with every length bounded, and no id, label or default flag, which are the
    shop's."""

    model_config = ConfigDict(extra="forbid")

    recipient: str = Field(min_length=1, max_length=200)
    lines: list[_StoreLine] = Field(default_factory=list, max_length=6)
    locality: _StoreLine | None = None
    region: _StoreLine | None = None
    postal_code: Annotated[str, Field(max_length=40)] | None = None
    country: Country | None = None
    phone: Annotated[str, Field(max_length=40)] | None = None

    def as_input(self) -> AddressInput:
        """The address as a new customer's default shipping address."""
        return AddressInput(**self.model_dump(), default_shipping=True)


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
    #: The shipping option a ``shipping`` charge was chosen from (#5281), if any; no
    #: other kind names one (``order_charges_option_is_shipping``).
    shipping_option_id: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,40}$")
    #: A charge sent back as it was read (#5281): the auto tax row, marked by ``auto`` or
    #: its ``id`` (any amount), is never written as a typed one; the order keeps its own
    #: and recomputes it. Any other ``id`` is ignored: the list is replaced whole.
    id: str | None = Field(default=None, max_length=100)
    auto: bool | None = None

    @field_validator("shipping_option_id")
    @classmethod
    def _option_is_shipping(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and info.data.get("kind") != "shipping":
            raise ValueError("only a shipping charge names a shipping option")
        return value


class Charge(ChargeInput):
    id: str
    order_id: str
    position: int
    #: The tax ScadBuddy computes from ``orders_tax_percent`` (#5281): no body writes it,
    #: and a save of the charges keeps it.
    auto: bool = False


class Payment(BaseModel):
    id: str
    order_id: str
    amount_minor: Money
    method: str | None
    reference: str | None
    received_at: datetime | None
    notes: str
    created_at: datetime
    provider: str | None = None
    payment_request_id: str | None = None


class OrderEvent(BaseModel):
    """One line of the order's audit line (§3.2, Ruling 12)."""

    id: int
    order_id: str
    at: datetime
    by: Actor
    kind: str
    detail: dict[str, Any]


#: Where an order was made (#5282, storefront spec §6.1): the back office, or a visitor's
#: cart on the storefront.
OrderSource = Literal["back_office", "storefront"]


class Order(BaseModel):
    id: str
    #: None only on an unsubmitted storefront draft (a visitor's cart, #5282): it takes
    #: its number and customer when it is submitted.
    number: str | None
    customer_id: str | None
    #: The spec carries `OPEN_FOR_EDITS` and `TAKES_ITEMS`, so the browser's copy
    #: (``afterAcceptance.ts``) is checked against them (``afterAcceptance.test.ts``, #5349).
    status: OrderStatus = Field(
        json_schema_extra={
            "x-open-for-edits": _listed(OPEN_FOR_EDITS),
            "x-takes-items": _listed(TAKES_ITEMS),
        }
    )
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
    #: What the order asks before production (#5281), in its currency; None asks none.
    deposit_minor: Money | None = None
    #: Why the shop went on without the deposit ("Proceed without deposit")...
    deposit_waived_reason: str | None = None
    #: ...and why it ships without full payment ("Ship without full payment").
    balance_waived_reason: str | None = None
    source: OrderSource = "back_office"
    #: When the visitor submitted the cart; None on every back-office order and on a draft.
    submitted_at: datetime | None = None
    #: The visitor whose cart it is (``storefront_visitors.id``); None once that row goes.
    storefront_visitor_id: str | None = None

    @property
    def label(self) -> str:
        """How a message names the order: its number, or ``cart <id>`` for a cart."""
        return self.number if self.number is not None else f"cart {self.id}"

    @property
    def unsubmitted(self) -> bool:
        """A storefront draft: the shop may read it, but never act on it."""
        return self.source == "storefront" and self.submitted_at is None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minor_exponent(self) -> int:
        """The decimal places of ``currency``'s minor unit (#4250 Ruling 5): every
        ``*_minor`` of the order is ``amount * 10 ** minor_exponent``."""
        return minor_exponent(self.currency)


#: A profit report row's products: an item with a model ``slug``; else one with a ``sku``;
#: else ``custom`` (a custom item with no SKU, or a request with no design yet).
ProductKind = Literal["model", "sku", "custom"]


class Product(BaseModel):
    """A profit report row's key (job cost spec §6.4), and the orders list's ``product``
    filter: ``model:<slug>``, ``sku:<sku>`` or ``custom``."""

    kind: ProductKind
    key: str | None = None

    @classmethod
    def parse(cls, value: str) -> Product:
        """``ValueError`` for anything but the three forms."""
        if value == "custom":
            return cls(kind="custom")
        kind, sep, key = value.partition(":")
        if not sep or not key or kind not in ("model", "sku"):
            raise ValueError("product is model:<slug>, sku:<sku> or custom")
        return cls(kind="model" if kind == "model" else "sku", key=key)

    def __str__(self) -> str:
        return "custom" if self.kind == "custom" else f"{self.kind}:{self.key}"


class OrderFilter(BaseModel):
    """What ``GET /orders`` narrows by (§4)."""

    #: Any of these (#5276); None or empty is every status.
    status: list[OrderStatus] | None = None
    customer_id: str | None = None
    #: Matched against the number, the notes and the customer's name.
    q: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    #: The profit report's row (job cost spec §6.4, #5329): orders accepted at or after
    #: ``accepted_since`` and before ``accepted_until``, in ``currency``, with an item of
    #: ``product``.
    accepted_since: datetime | None = None
    accepted_until: datetime | None = None
    currency: str | None = None
    product: Product | None = None
    source: OrderSource | None = None
    #: False leaves out the unsubmitted storefront drafts; True lists only those.
    unsubmitted: bool = False


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


class LibraryFileRef(BaseModel):
    """The library file a ``library_file`` item prints (#5544): its id, its name when the
    item was added (for display after a delete or a rename), and which library it is
    in. It has no template commit and no customizer inputs."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    filename: str
    library: ItemLibrary


class OrderItem(BaseModel):
    id: str
    order_id: str
    position: int
    status: ItemStatus
    quantity: int = Field(ge=1)
    title: str
    notes: str
    kind: ItemKind = "model"
    #: What to make; None for a ``custom`` item, which names nothing (#4954), for a
    #: ``request`` item until its first design, and for a ``library_file`` item.
    reference: Reference | None
    #: A ``library_file`` item's file (#5544); None for every other kind.
    library_file: LibraryFileRef | None = None
    #: A ``custom`` item's own flag: False skips the print path (it is marked done by
    #: hand). Always True for a ``model`` or ``library_file`` item.
    requires_printing: bool = True
    #: The shop's stock code, when it keeps one.
    sku: str | None = None
    colours: list[ColourSlot]
    output_id: str | None
    #: The image attachment a person marked as what was actually delivered (§6.2), one
    #: of the item's own; None until one is marked, and again once it is removed.
    delivered_attachment_id: str | None = None
    #: The design revision the reference and colours are a copy of; None before the first.
    current_revision_id: str | None = None
    approval: Approval = "together"
    #: True while an accepted item's newest revision, added after acceptance (#5350),
    #: waits for its own quote to be accepted. The old accepted quote stays accepted (the
    #: price the customer agreed) until then; accepting a quote clears it.
    revised_after_acceptance: bool = False
    #: The Bambuddy archives a past print's item was taken from (#4777, order operations
    #: spec §3.2): the one picked and its run's other plates; empty for any other source.
    origin_archive_ids: list[int] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class Revision(BaseModel):
    """One design of an item: a reference and colours, numbered per item, with the
    customer's answer once it is sent and answered."""

    id: str
    item_id: str
    number: int = Field(ge=1)
    reference: Reference
    colours: list[ColourSlot]
    note: str
    created_at: datetime
    created_by: Actor
    sent_at: datetime | None = None
    answer: RevisionAnswer | None = None
    answer_note: str | None = None
    answered_at: datetime | None = None
    answered_by: Actor | None = None


# -- quotes (§3.4) ----------------------------------------------------------------------


class QuoteLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: QuoteLineKind
    label: str
    quantity: int = Field(ge=0)
    unit_minor: Money
    amount_minor: Money


# -- a machine rate (#4625, order operations spec 2026-11-07 §6.2) ----------------------


#: Where a rate's task intervals came from: Bambuddy now (``bambuddy``), the last ones
#: read from it because it did not answer (``stored``), typed in ScadBuddy for a printer
#: Bambuddy does not track (``typed``), or none at all (``none``: no profile).
IntervalSource = Literal["bambuddy", "stored", "typed", "none"]
#: Why a task is listed and not counted.
#: ``not_read``: a linked printer's task whose interval Bambuddy has not given (yet).
NotCounted = Literal["calendar", "disabled", "no_interval", "not_in_bambuddy", "not_read"]


class MaintenancePart(BaseModel):
    task: str
    interval_hours: str | None
    interval_type: Literal["hours", "days"] = "hours"
    minutes: str
    consumables: str
    per_hour: str
    counted: bool
    why: NotCounted | None = None


class WearPartRate(BaseModel):
    name: str
    cost: str
    life_hours: str
    per_hour: str


class MachineRateBreakdown(BaseModel):
    """Each part of a machine rate, as a quote records it (``machine_rate_breakdown``)
    and Settings shows it: decimal strings in ``currency``."""

    #: The registry printer and its name when the rate was computed.
    printer: str | None = None
    printer_name: str | None = None
    #: ``profile``: derived from the printer's inputs; ``price_book``: the printer has
    #: no profile, so it is the book's ``machine_rate_per_hour`` and every part is 0.
    source: Literal["profile", "price_book"]
    rate_per_hour: str
    currency: str
    labour_rate_per_hour: str | None = None
    maintenance: list[MaintenancePart] = Field(default_factory=list)
    maintenance_per_hour: str = "0"
    wear: list[WearPartRate] = Field(default_factory=list)
    wear_per_hour: str = "0"
    watts: str = "0"
    kwh_price: str = "0"
    power_per_hour: str = "0"
    purchase_price: str = "0"
    life_hours: str | None = None
    depreciation_per_hour: str = "0"
    intervals: IntervalSource = "none"
    #: When the stored intervals were read from Bambuddy (``intervals`` ``stored``).
    intervals_read_at: datetime | None = None


class Quote(BaseModel):
    id: str
    order_item_id: str
    #: The design revision it was quoted for, when the item had one.
    revision_id: str | None = None
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
    #: A slice quote's settings per filament slot (#5571), as it sliced with them:
    #: `{"<slot>": {"<key>": value}}`. Null for none. An item's print that sends none
    #: prints with these (`orders/operations.py` `print_body`).
    part_settings: dict[str, dict[str, Any]] | None = None
    #: The registry printer a slice quote was priced for (#4625), and each part of the
    #: machine rate it was priced at. Null on a manual quote and on one recorded before.
    printer: str | None = None
    machine_rate_breakdown: MachineRateBreakdown | None = None
    #: A slice quote's print setup (#4673, order operations spec §5.1), as it sliced:
    #: `printer_id`, `printer`, `path`, `choices`, `prime_tower`, `supports` and
    #: `print_sequence`, each in `PrintRunRequest`'s shape (`orders/quote.py`
    #: `print_setup`). Null for a manual or archive quote, and one recorded before.
    choices: dict[str, Any] | None = None
    #: The spool per slot a slice quote sliced with (`PrintRunRequest.filament_plan`).
    filament_plan: dict[str, Any] | None = None


# -- fulfilment (§6.1 by hand) and shipments (§6.3) ------------------------------------


class Fulfilment(BaseModel):
    id: str
    order_item_id: str
    attempt: int
    #: A ``test`` attempt prints a revision for the shop, and names it.
    purpose: FulfilmentPurpose = "production"
    #: The revision the attempt printed: a test print's, or for a production attempt the
    #: item's current revision when it was recorded (#5704). None before the item had one.
    revision_id: str | None = None
    quantity: int
    outcome: Outcome
    failure_reason: str | None
    started_at: datetime | None
    finished_at: datetime | None
    notes: str
    accepted_at: datetime | None
    created_by: Actor
    created_at: datetime
    #: The print run that queued or started it (§5.4, #4719); None for an attempt
    #: recorded by hand. The item follows the run while ``finished_at`` is unset.
    print_run_id: str | None = None
    #: The accepted quote's sliced file the run printed from (§6.1), held by
    #: ``(order_item_fulfilment, <id>)``; None for a ``manual`` quote, which names no file.
    sliced_sha256: str | None = None
    sliced_blob_key: str | None = None
    #: The registry printer the run named, when it named one.
    printer: str | None = None
    #: That printer as the registry named it when the run was attached (§8.4): its serial,
    #: name and Bambuddy printer id, kept past a rename or delete.
    printer_serial: str | None = None
    printer_name: str | None = None
    bambuddy_printer_id: int | None = None
    #: How many the run was sent; ``quantity`` is what it made, set when it settles.
    copies: int | None = None
    #: The print run's ``request_id`` this attempt was claimed under before its print
    #: started (#4719); a retried operation finds its claim by it.
    print_request_id: str | None = None
    #: The Bambuddy archives its run's queue items made, one per plate (§8.2), copied
    #: once the print settled (an attempt by hand: the one it named); empty for an attempt
    #: by hand naming none, or one not linked yet.
    archive_ids: list[int] = Field(default_factory=list)
    #: The archives' printing time and filament, summed (§8.2).
    actual_seconds: int | None = None
    actual_grams: float | None = None


class OrderProject(BaseModel):
    """The Bambuddy project mirroring the order, or one of its items nested under it
    (§8.1)."""

    #: ``order:<id>`` or ``order_item:<id>``.
    subject: str
    project_id: int
    #: The item's id for an item's project; None for the order's own.
    item_id: str | None = None


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


# -- time and cost entries (job cost spec 2026-11-05 §4.3, §4.4, #5326) ----------------

#: What a time entry was spent on.
TimeKind = Literal["design", "setup", "post_processing", "packing", "other"]
#: What a cost entry paid for. Shipping is a shipment's `cost_minor`, never an entry.
CostKind = Literal["assistant", "payment_fee", "material", "other"]
#: The longest time entry, in minutes (about two years): a typo, never a real entry.
MAX_ENTRY_MINUTES = 1_000_000
#: The largest cost entry: `order_cost_entries.amount_minor` is a `bigint`.
MAX_ENTRY_MINOR = 2**63 - 1
#: An entry's ``source``: ``<writer>:<id>``.
ENTRY_SOURCE = r"^[a-z_]+:\S{1,200}$"


class TimeEntryInput(BaseModel):
    """Time spent on the order (``order_item_id`` None) or one of its items."""

    model_config = ConfigDict(extra="forbid")

    order_item_id: str | None = None
    kind: TimeKind
    minutes: int = Field(ge=1, le=MAX_ENTRY_MINUTES)
    note: str = Field(default="", max_length=1000)
    #: When the timer started, when a timer wrote it.
    started_at: datetime | None = None
    #: What wrote it (``timer:<item or order>:<started_at>``, ``manual:<uuid>``): one entry
    #: per order and source, whatever its kind, so a retry whose first reply was lost
    #: writes once.
    source: str | None = Field(default=None, pattern=ENTRY_SOURCE)


class TimeEntry(TimeEntryInput):
    id: str
    order_id: str
    #: The labour rate in force when it was written, in the order's minor units per hour;
    #: None when the order's currency is not the shop's, so no rate could be copied.
    rate_minor_per_hour: Money | None
    created_by: Actor
    created_at: datetime


class CostEntryInput(BaseModel):
    """Money spent on the order or one of its items, in the order's currency."""

    model_config = ConfigDict(extra="forbid")

    order_item_id: str | None = None
    kind: CostKind
    amount_minor: Money = Field(ge=1, le=MAX_ENTRY_MINOR)
    note: str = Field(default="", max_length=1000)
    #: What wrote it (``ai_session:<id>``, ``payment:<id>``, a form's ``manual:<uuid>``):
    #: one entry per order, kind and source, so a writer that runs twice writes once.
    source: str | None = Field(default=None, pattern=ENTRY_SOURCE)


class CostEntry(CostEntryInput):
    id: str
    order_id: str
    created_by: Actor
    created_at: datetime


# -- attachments (§6.2, #4251) ----------------------------------------------------------

#: What a file is, from its bytes (``orders/attachments.py`` ``sniff``): an ``image`` or
#: ``video`` the browser can show, a ``document`` (a PDF), or ``other``, any file at all,
#: which is only ever downloaded.
AttachmentKind = Literal["image", "video", "document", "other"]
#: Why it is kept: the customer's ``reference`` (a sketch, a logo, a drawing), a print's
#: ``issue``, the ``finished`` object, the ``packed`` box, a ``timelapse``, or ``other``.
AttachmentRole = Literal[
    "reference", "issue", "finished", "packed", "timelapse", "render", "test_print", "other"
]
#: The roles only a design revision's file takes (storefront spec §4): its ``render``
#: and the photo of its ``test_print``.
REVISION_ROLES: frozenset[AttachmentRole] = frozenset({"render", "test_print"})
#: The longest caption, in characters, as a template's media (``library/media.py``).
MAX_ATTACHMENT_CAPTION = 1000

#: The types served as themselves, inline: what a browser shows and never runs. Every
#: other file is a download (``api/order_attachments.py``).
INLINE = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "image/avif",
        "image/heic",
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "application/pdf",
    }
)
#: The image types a browser draws: the UI shows them, and only one of them may be
#: marked delivered.
SHOWN_IMAGES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"})
#: The ones the server shrinks to a thumbnail; a GIF or AVIF is shown as it is.
THUMBNAILED = frozenset({"image/png", "image/jpeg", "image/webp"})


class Attachment(BaseModel):
    id: str
    order_id: str
    #: ``order:<id>``, ``order_item:<id>``, ``order_item_fulfilment:<id>`` or
    #: ``order_item_revision:<id>``: what it is
    #: attached to.
    subject: str
    #: The line item it is attached to, or the attempt's item; None for the order's own.
    item_id: str | None
    #: The fulfilment attempt it is attached to (#4251), or None.
    fulfilment_id: str | None = None
    #: The design revision it is attached to (an ``order_item_revision:<id>`` subject).
    revision_id: str | None = None
    kind: AttachmentKind
    role: AttachmentRole
    #: The name it was sent with, for display and the download.
    filename: str
    #: What its bytes are (sniffed), or what its name says for ``other``. The spec
    #: carries the three type sets, so the browser's copy is tested against them
    #: (``attachmentMedia.test.ts``, #4679).
    media_type: str = Field(
        json_schema_extra={
            "x-inline": _listed(INLINE),
            "x-shown-images": _listed(SHOWN_IMAGES),
            "x-thumbnailed": _listed(THUMBNAILED),
        }
    )
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
    #: #6209: what Bambuddy keeps beside the RGB. ``colour`` stays ``#RRGGBB``: a clear
    #: filament is its RGB with ``alpha`` below 255.
    alpha: Alpha = 255
    extra_colours: ColourStops = Field(default_factory=list)
    effect: FilamentEffect | None = None


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
    alpha: Alpha | None = None
    extra_colours: ColourStops | None = None
    #: ``null`` clears it.
    effect: FilamentEffect | None = None
    #: ``import`` hands the colour back to the spool import, which sets it at its next
    #: run; a colour, alpha, stops or effect sent that differs from the record's makes
    #: it ``manual`` (#6209).
    colour_source: ColourSource | None = None


class Filament(BaseModel):
    """A filament record, its decimals as strings on the wire (§3.6)."""

    #: Every field is on the wire, so the clients' types need no fallback (#6209).
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: str
    material: str
    subtype: str | None
    brand: str | None
    colour_name: str | None
    colour: str
    cost_per_kg: str
    density_g_cm3: str | None
    #: Where ``density_g_cm3`` came from when the spool import, "Fill densities" or a
    #: manual add with no density read it from Bambuddy's link to the slicer preset
    #: (#6136, `orders/density.py`): the preset and the id that named it. ``None`` for
    #: a density a person set, or none.
    density_profile: str | None = None
    #: #6209: the ``rgba`` alpha byte (255 opaque), the multi-colour stops and
    #: Bambuddy's effect. ``colour`` alone is what a render splits parts by.
    alpha: int = 255
    extra_colours: list[str] = Field(default_factory=list)
    effect: FilamentEffect | None = None
    #: Who set the colour, alpha, stops and effect: the spool ``import`` or a person.
    colour_source: ColourSource = "manual"
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
    #: Whether a Bambuddy is linked, so ``POST …/import-spools`` can run (§8.3, #4253).
    spool_import: bool = False


SpoolImportAction = Literal["created", "updated", "unchanged", "skipped"]


class SpoolImportRow(BaseModel):
    """One filament of the spool inventory and what the import did with it (§8.3)."""

    action: SpoolImportAction
    #: Why a group was skipped: ``no_colour`` (its spools carry only a colour's name,
    #: and a record's colour is ``#RRGGBB``), ``no_price`` (no spool has a price),
    #: ``too_long`` (a name past a record's 100 characters) or ``colour_taken`` (another
    #: record of its material, subtype and brand, linked to other spools, has its RGB as
    #: the colour a person set, #6209). On a matched record, ``colour_taken`` says its
    #: colour, alpha, stops and effect were kept: another record of its names has the
    #: spools' RGB.
    reason: Literal["no_colour", "no_price", "too_long", "colour_taken"] | None = None
    #: The newest spool's names. A matched record keeps its own: only its price changes.
    material: str
    subtype: str | None = None
    brand: str | None = None
    colour_name: str | None = None
    #: The record's colour after the import: the spools' unless a person set it
    #: (``colour_source`` ``manual``, #6209); the spools' for a skipped group.
    colour: str | None = None
    alpha: int | None = None
    extra_colours: list[str] = Field(default_factory=list)
    effect: FilamentEffect | None = None
    colour_source: ColourSource | None = None
    #: The record made, updated or matched.
    filament_id: str | None = None
    #: The most recent priced spool's (the highest id), as the record now has it.
    cost_per_kg: str | None = None
    #: What a matched record had before.
    previous_cost_per_kg: str | None = None
    #: The spread of the group's prices, only when its spools' prices differ.
    min_cost_per_kg: str | None = None
    max_cost_per_kg: str | None = None
    spool_ids: list[int]
    #: The record's density after the import (#6136): its own, or the one the import
    #: read through Bambuddy when it had none.
    density_g_cm3: str | None = None
    #: The record's ``density_profile`` after the import: where this import read
    #: ``density_g_cm3`` from, or where a density it kept was read from before;
    #: ``None`` for a density a person set, or none.
    density_profile: str | None = None


class DensityFillRow(BaseModel):
    """One record of the book that had no density, and what filled it (#6136)."""

    filament_id: str
    material: str
    subtype: str | None = None
    brand: str | None = None
    colour_name: str | None = None
    colour: str
    #: The density written; ``None`` when no source gives one, so the record still has
    #: none and an estimate of it is refused.
    density_g_cm3: str | None = None
    #: Where it was read from.
    density_profile: str | None = None


class DensityFill(BaseModel):
    """`POST /orders/price-book/fill-densities`: one row per record that had no
    density, in the book's order."""

    rows: list[DensityFillRow]


class SpoolImport(BaseModel):
    """`POST /orders/price-book/import-spools`: one row per filament the inventory
    holds, in ``_filament_key`` order."""

    rows: list[SpoolImportRow]
