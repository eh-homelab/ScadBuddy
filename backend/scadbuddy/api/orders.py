"""`/api/v1/orders` (#4249, spec 2026-10-18 §3.2-3.5, §4, §6.1 by hand, §6.3; plan
Rulings 1-3, 8-12): orders, their line items, manual quotes, fulfilment attempts,
shipments, payments and the audit line; the price book, the slice quote and the rough
estimate (#4250, §5).

Plain reads and writes, but for the slice quote and an item's print, the
``order_item_quote`` and ``order_item_print`` operations (``orders/operations.py``)
through ``run_operation``. Every write goes through
the lifecycle (``orders/lifecycle.py``), which locks the order, derives its status and
publishes ``order.changed`` / ``order_item.changed`` after commit; a refusal arrives as a
`LifecycleError` and leaves as a 409 problem typed
``https://scadbuddy.dev/problems/orders/<slug>`` with the ids in the way (``items``,
``shipments``), or a 404 for a row that is not there. A reference that cannot be made is
422 naming the field (``orders/reference.py``). ``created_by`` and every audit row's
``by`` come from the author header, never a body (Ruling 12).

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
import logging
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from scadbuddy.api.activity import BAD_CURSOR, _no_nul, _unknown
from scadbuddy.api.deps import (
    CatalogueDep,
    ConfigDep,
    FetcherDep,
    HistoryDep,
    OutputsDep,
    PathsDep,
    SettingsStoreDep,
    StateDep,
)
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.api.printing import asked_by_api
from scadbuddy.api.runtime import apply_runtime
from scadbuddy.bambuddy.print_source import printable
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import require_output
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.operations.component import OperationsDep
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.archives import AttemptArchives, attempt_archives, mirror_archives
from scadbuddy.orders.bambuddy import ServedLibrary
from scadbuddy.orders.component import OrdersComponent, OrdersDep
from scadbuddy.orders.costs import (
    CostRates,
    MarginState,
    OrderCosts,
    OrderMargin,
    order_costs,
    order_margins,
)
from scadbuddy.orders.estimate import EstimatedPart, OrderEstimate, filament_estimate
from scadbuddy.orders.history import UnitCostHistory, unit_cost_history
from scadbuddy.orders.integrations import ArchiveMedia, NotLinkedError, not_linked
from scadbuddy.orders.lifecycle import (
    FulfilLine,
    FulfilmentInput,
    LabourRate,
    LifecycleError,
    ManualQuoteInput,
    NewCustomItem,
    NewItem,
    NewLibraryItem,
    NewModelItem,
    NewRequestItem,
    OrderEdit,
    ShipmentEdit,
    ShipmentInput,
    check_submitted,
    stale_attempt,
)
from scadbuddy.orders.models import (
    Approval,
    Attachment,
    Charge,
    ChargeInput,
    CostEntry,
    CostEntryInput,
    Filament,
    FilamentInput,
    FilamentPatch,
    Fulfilment,
    ItemKind,
    LibraryFileRef,
    Money,
    Order,
    OrderEvent,
    OrderFilter,
    OrderItem,
    OrderProject,
    OrderSource,
    OrderStatus,
    Payment,
    PriceBook,
    PricingPolicy,
    Product,
    Quote,
    Revision,
    Shipment,
    SpoolImport,
    TimeEntry,
    TimeEntryInput,
)
from scadbuddy.orders.operations import (
    BODY_ONLY,
    CARRIED,
    ORDER_ATTEMPT_COPY,
    ORDER_ITEM_PRINT,
    ORDER_ITEM_QUOTE,
)
from scadbuddy.orders.past_prints import NativeArchiveCopies, copied_file, quote_from_archive
from scadbuddy.orders.printing import ItemPrintRequest, print_request_id
from scadbuddy.orders.problems import PROBLEMS, refused
from scadbuddy.orders.projects import ensure_projects
from scadbuddy.orders.quote import (
    ArchiveQuoteRequest,
    ItemSliceQuoteRequest,
    plate_image,
    subject_of,
)
from scadbuddy.orders.reference import ReferenceError, TemplateDeletedError, resolve_reference
from scadbuddy.orders.report import (
    DEFAULT_DAYS,
    ProfitReport,
    ReportGroup,
    default_period,
    profit_report,
)
from scadbuddy.orders.revisions import RevisionAnswerInput, RevisionInput
from scadbuddy.orders.spool_import import import_spools as import_spools_into
from scadbuddy.orders.store import (
    UNSET,
    FilamentExistsError,
    OrderStore,
    UnsetType,
    decode_cursor,
    encode_cursor,
)
from scadbuddy.orders.storefront.addresses import EmailInvalidError
from scadbuddy.orders.storefront.links import LinkState
from scadbuddy.orders.storefront.mail import ORDER_MAIL_MAX, OrderMail
from scadbuddy.orders.totals import OrderTotals, order_totals
from scadbuddy.payments.component import PaymentsComponent, PaymentsDep
from scadbuddy.payments.credentials import KekMissingError
from scadbuddy.payments.models import PaymentRequestView
from scadbuddy.payments.store import requests_for_order
from scadbuddy.print_library.component import PrintLibrary, PrintLibraryDep
from scadbuddy.print_library.ids import is_native_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])

#: Ruling 9: cursor-paged, as the Activity page is.
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
#: A ``margin_state`` request reads at most this many store pages of this size (#5515).
#: When it reaches the cap, it returns the rows found so far (maybe none) and the cursor
#: it reached, and the client carries on from there.
#: frontend/src/mocks/features/orders/orders.ts mirrors both values: change them together.
SCAN_PAGE_SIZE = MAX_LIMIT
MAX_SCAN_PAGES = 10
LIST_PARAMS = frozenset(
    {
        "status",
        "customer_id",
        "q",
        "from",
        "to",
        "accepted_from",
        "accepted_to",
        "currency",
        "product",
        "margin_state",
        "source",
        "unsubmitted",
        "cursor",
        "limit",
    }
)
REPORT_PARAMS = frozenset({"from", "to", "group"})
#: A line item's largest quantity: `order_items.quantity` is an `integer` column
#: (migration 20261020T0446Z_orders.sql), so a larger one is a 422, never a 500 (#4763).
MAX_QUANTITY = 2**31 - 1


# -- bodies (Ruling 12: no author, no status) ----------------------------------------------


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    notes: str = ""
    due_at: datetime | None = None


class OrderPatch(BaseModel):
    """Notes and the due date in any open state; the address ids (the order's own
    customer's) and the typed charges, replaced whole, while `draft` or `quoted`.
    The auto tax row (#5281) is not part of the list: leave it out and the order keeps it
    and recomputes it; sent back as read (marked by `auto` or its `id`, whatever the
    amount) it stays the auto row and is recomputed. Any other `tax` charge, whatever its
    label, is a typed tax and replaces the auto row. Past that, charges are 409
    ``order_not_editable``, which names `PUT /orders/{id}/charges/after-acceptance`
    (#5351). A field left out is untouched; ``null`` clears the due date or an address."""

    model_config = ConfigDict(extra="forbid")

    notes: str | None = None
    due_at: datetime | None = None
    shipping_address_id: str | None = None
    billing_address_id: str | None = None
    charges: list[ChargeInput] | None = None

    def edit(self) -> OrderEdit:
        given = self.model_fields_set
        return OrderEdit(
            notes=self.notes,
            due_at=self.due_at if "due_at" in given else UNSET,
            shipping_address_id=self.shipping_address_id
            if "shipping_address_id" in given
            else UNSET,
            billing_address_id=self.billing_address_id if "billing_address_id" in given else UNSET,
            charges=self.charges,
        )


class ChargesBody(BaseModel):
    """`PUT /orders/{id}/charges/after-acceptance` (#5351): the charges part of
    `PATCH /orders/{id}`, the typed charges replaced whole. The auto tax row (#5281) is
    not part of the list: leave it out and the order keeps it and recomputes it; sent back
    as read (marked by `auto` or its `id`, whatever the amount) it stays the auto row and
    is recomputed. Any other `tax` charge, whatever its label, is a typed tax and replaces
    the auto row."""

    model_config = ConfigDict(extra="forbid")

    charges: list[ChargeInput]


class ReferenceBody(BaseModel):
    """`{slug, commit, inputs}`. Beside ``output_id`` each may be left out (the output's
    stands in) or given, and then must agree with the output's: ``inputs`` as the whole
    object, ``v`` included, since a missing ``v`` reads as 0 (#4458)."""

    model_config = ConfigDict(extra="forbid")

    slug: str | None = None
    commit: str | None = None
    inputs: dict[str, Any] | None = None


class ItemCreate(ReferenceBody):
    """A ``model`` item (the default) names what to make: ``output_id``, or ``slug``,
    ``commit`` and ``inputs``. A ``custom`` item (#4954) names nothing: it takes a
    ``title`` and a ``unit_price_minor`` instead, and ``requires_printing``. A ``request``
    item (#5280) is what a customer wants made before any design exists: a ``title``,
    ``quantity``, ``notes`` and ``sku`` only; its first revision names the design. A
    ``library_file`` item (#5544) names a file of the library: ``library_file_id``, with
    ``kind`` left out or ``library_file``, and no other source. A past print (#4777) is
    ``archive_id`` alone, with ``kind`` left out: the archive decides the item's kind."""

    kind: ItemKind = "model"
    output_id: str | None = None
    #: A file of the library that serves (#5544), as `print_output` takes one: the item
    #: prints it. Its ``kind`` is ``library_file``.
    library_file_id: int | None = Field(default=None, ge=1)
    archive_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "A Bambuddy archive (a past print, `GET /farm/archives`), alone: the item is"
            " a `model` item of the output that printed it, a `library_file` item of the"
            " library file that printed it, or else a `library_file` item of a copy of its"
            " printed file in the native library (one per archive, reused). It keeps"
            " `origin_archive_ids`. 409 `archive_gone` for an archive Bambuddy no longer"
            " has, `archive_copy_not_native` when a copy is needed and the native library"
            " does not serve."
        ),
    )
    quantity: int = Field(ge=1, le=MAX_QUANTITY)
    title: str | None = Field(default=None, max_length=200)
    notes: str = ""
    sku: str | None = Field(default=None, max_length=100)
    #: A custom item's price per unit: its `manual` quote, accepted as it is saved.
    unit_price_minor: Money | None = Field(default=None, ge=0)
    #: A custom item's: False (the default) skips the print path.
    requires_printing: bool | None = None
    #: Whether that price is accepted as it is saved: true (the browser's) leaves the item
    #: `approved`; false (the agent's `write` tools) leaves it `quoted`, for an `outward`
    #: accept. A custom item's only.
    accept: bool | None = Field(
        default=None,
        description=(
            "A custom item's only: whether its price is accepted as it is saved. True"
            " leaves the item `approved`; false leaves it `quoted`. Unset is true on a"
            " `back_office` order and false on a `storefront` one, where only the customer"
            " accepts a price: true there is 409 `customer_accepts` (#5283). On"
            " `POST /orders/{id}/items/after-acceptance` unset means false, and true is"
            " 422: an item added after acceptance needs its quote accepted on its own."
        ),
    )

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.archive_id is not None:
            named = [
                name
                for name in (
                    "slug",
                    "commit",
                    "inputs",
                    "output_id",
                    "library_file_id",
                    "unit_price_minor",
                    "requires_printing",
                    "accept",
                )
                if getattr(self, name) is not None
            ]
            if "kind" in self.model_fields_set:
                named.append("kind")
            if named:
                raise ValueError(
                    f"archive_id names a past print, the item's only source: drop"
                    f" {', '.join(named)}"
                )
            return self
        if self.library_file_id is not None or self.kind == "library_file":
            if "kind" in self.model_fields_set and self.kind != "library_file":
                raise ValueError(
                    f"library_file_id names a library file item: drop kind {self.kind!r}"
                )
            if self.library_file_id is None:
                raise ValueError("a library_file item needs a library_file_id")
            named = [
                name
                for name in (
                    "slug",
                    "commit",
                    "inputs",
                    "output_id",
                    "unit_price_minor",
                    "requires_printing",
                    "accept",
                )
                if getattr(self, name) is not None
            ]
            if named:
                raise ValueError(
                    f"a library file item names its file only: drop {', '.join(named)}"
                )
            self.kind = "library_file"
            return self
        if self.kind == "custom":
            named = [
                name
                for name in ("slug", "commit", "inputs", "output_id")
                if getattr(self, name) is not None
            ]
            if named:
                raise ValueError(f"a custom item names no source: drop {', '.join(named)}")
            if not (self.title or "").strip():
                raise ValueError("a custom item needs a title")
            if self.unit_price_minor is None:
                raise ValueError("a custom item needs a unit_price_minor")
        elif self.kind == "request":
            named = [
                name
                for name in (
                    "slug",
                    "commit",
                    "inputs",
                    "output_id",
                    "unit_price_minor",
                    "requires_printing",
                    "accept",
                )
                if getattr(self, name) is not None
            ]
            if named:
                raise ValueError(f"a request item takes no {', '.join(named)}")
            if not (self.title or "").strip():
                raise ValueError("a request item needs a title")
        else:
            named = [
                name
                for name in ("unit_price_minor", "requires_printing", "accept")
                if getattr(self, name) is not None
            ]
            if named:
                raise ValueError(f"only a custom item takes {', '.join(named)}")
        return self


#: How many items `POST /orders/{id}/items/batch` adds at most (#4777).
MAX_BATCH_ITEMS = 50
#: The problem a reference to a deleted template is (Ruling 6).
TEMPLATE_DELETED = PROBLEMS + "template_deleted"


class ItemBatch(BaseModel):
    """`POST /orders/{id}/items/batch` (#4777, order operations spec 2026-11-07 §3.1):
    the items of `POST /orders/{id}/items`, added in order, all or none."""

    model_config = ConfigDict(extra="forbid")

    items: list[ItemCreate] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)


class ItemPatch(BaseModel):
    """The reference, replaced whole, and the quantity while `draft` or `quoted`, or past
    that while the item has no accepted quote (#5349; each drops the accepted quote);
    title and notes in any open state."""

    model_config = ConfigDict(extra="forbid")

    reference: ReferenceBody | None = None
    output_id: str | None = None
    quantity: int | None = Field(default=None, ge=1, le=MAX_QUANTITY)
    title: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    #: ``{slot: filament_id | null}`` over the item's ``colours`` (#4250 Ruling 6): the
    #: price-book filament each slot is priced with. A change drops the accepted quote,
    #: as a reference change does; the item's own filaments sent again change nothing.
    filaments: dict[int, str | None] | None = None
    #: ``null`` clears it.
    sku: str | None = Field(default=None, max_length=100)
    #: A custom item's new price per unit (#4954), priced at once, as is its quantity.
    unit_price_minor: Money | None = Field(default=None, ge=0)
    requires_printing: bool | None = None
    #: Whether a custom item's new price is accepted (true) or left `quoted` for an
    #: `outward` accept (false, the agent's); nothing else reads it. Unset is true on a
    #: `back_office` order and false on a `storefront` one, where true is 409
    #: `customer_accepts` (#5283), with or without a new price.
    accept: bool | None = None
    #: Whether the item's design is approved before it is priced (`design_first`) or
    #: with its quote (`together`), in any open state.
    approval: Approval | None = None


class RevisionCreate(RevisionInput):
    """A new revision: the shop's note and colours (``RevisionInput``) and, to change the
    design, a ``reference`` or an ``output_id``, as a line item's patch takes them."""

    reference: ReferenceBody | None = None
    output_id: str | None = None


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_minor: Money
    method: str | None = None
    reference: str | None = None
    received_at: datetime | None = None
    notes: str = ""
    #: The open payment request this payment settles (#4957): it closes as `paid`.
    payment_request_id: str | None = None


class AcceptForCustomerBody(BaseModel):
    """The shop's "Accept for the customer" on a storefront order (#5283, storefront spec
    §6.1): what it accepts, and why the customer did not."""

    model_config = ConfigDict(extra="forbid")

    #: `revision`: the item's newest sent revision, accepted; `quote`: one of its quotes.
    what: Literal["revision", "quote"]
    #: The revision's id or the quote's id.
    target_id: str = Field(min_length=1, max_length=100)
    #: Why the shop accepts for the customer (e.g. "By phone, 9 Nov"), kept in the audit
    #: row `item.accepted_for_customer`.
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def _trimmed(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class CustomerLinkSend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: One address; the customer's email contact when left out.
    to: str | None = Field(default=None, max_length=320)
    #: Replace a live link. Without it, a live link is 409 `link_live` and nothing changes.
    replace: bool = False


# -- views -------------------------------------------------------------------------------------


class ItemAttempt(Fulfilment):
    """An attempt as the order detail shows it. ``stale``: it printed a design earlier
    than the one the accepted quote prices (#5592), so its acceptance is refused
    (`fulfilment_stale`); the rule is `lifecycle.py` `stale_attempt`."""

    stale: bool


class ItemDetail(OrderItem):
    quotes: list[Quote]
    fulfilments: list[ItemAttempt]
    revisions: list[Revision]


def item_detail(store: OrderStore, item: OrderItem) -> ItemDetail:
    quotes = store.quotes(item.id)
    revisions = store.revisions(item.id)
    return ItemDetail(
        **item.model_dump(),
        quotes=quotes,
        fulfilments=[
            ItemAttempt(**f.model_dump(), stale=stale_attempt(f, quotes, revisions))
            for f in store.fulfilments(item.id, purpose=None)
        ],
        revisions=revisions,
    )


class OrderDetail(BaseModel):
    order: Order
    items: list[ItemDetail]
    charges: list[Charge]
    payments: list[Payment]
    shipments: list[Shipment]
    totals: OrderTotals
    #: The order's files and its items' (``item_id``), oldest first (§6.2).
    attachments: list[Attachment]
    #: The Bambuddy projects mirroring the order and its items (§8.1), the order's first;
    #: empty without Bambuddy, or before the order is accepted.
    projects: list[OrderProject] = Field(default_factory=list)
    #: The order's payment requests, oldest first (#4957).
    payment_requests: list[PaymentRequestView] = Field(default_factory=list)


class OrderRow(Order):
    """A row of ``GET /orders``: the order and its margin, the same figures as its
    ``GET /orders/{id}/costs`` (job cost spec §6.2, §7)."""

    #: Charged less real cost; None without a charged figure.
    margin_minor: Money | None = None
    #: Of what was charged, to a tenth; None without a charged figure above zero.
    margin_percent: float | None = None
    #: ``loss``, ``unknown``, ``thin`` or ``ok``; None without a charged figure.
    margin_state: MarginState | None = None
    #: The margin is built on an estimated part (a default labour, a quote's grams).
    margin_estimated: bool = False


class OrderPage(BaseModel):
    items: list[OrderRow]
    next_cursor: str | None


# -- errors ------------------------------------------------------------------------------------


def _bad_reference(error: ReferenceError) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        str(error),
        errors=[{"loc": ["body", error.field], "msg": error.problem}],
    )


async def _run[T](call: Callable[[], T]) -> T:
    """A lifecycle call off the event loop, its refusal as a problem."""
    try:
        return await asyncio.to_thread(call)
    except LifecycleError as error:
        raise refused(error) from None


async def _run_linked[T](call: Callable[[], T], order_id: str, payments: PaymentsComponent) -> T:
    """``_run``, then a link for each request it opened on a provider (#5281): a reopened
    order's balance request, or one a total grown past a paid deposit opens. An older
    request whose link failed is left to ``POST …/link`` (``Payments.run_linking``)."""
    try:
        return await payments.service.run_linking(order_id, call)
    except LifecycleError as error:
        raise refused(error) from None


async def _run_closing_linked[T](
    call: Callable[[], T], order_id: str, orders: OrdersComponent, payments: PaymentsComponent
) -> T:
    """``_run`` for an accept that may accept the order (#5283), with what ``POST …/accept``
    does after its commit: the pages of the requests it cancels are closed, the balance
    request it opens is linked (``Payments.run_closing_linked``), and when it accepted the
    order its Bambuddy projects are made, best effort (``ensure_order_projects``)."""
    before = await asyncio.to_thread(orders.store.order, order_id)
    try:
        result = await payments.service.run_closing_linked(order_id, call)
    except LifecycleError as error:
        raise refused(error) from None
    if before is not None and before.status != "accepted":
        after = await asyncio.to_thread(orders.store.order, order_id)
        if after is not None and after.status == "accepted":
            await ensure_order_projects(orders, after)
    return result


def check_path(*values: str) -> None:
    if any("\x00" in value for value in values):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "a path value holds a NUL byte")


# -- orders ------------------------------------------------------------------------------------


def _cost_rates(settings: SettingsStore, orders: OrdersComponent) -> CostRates:
    """Today's rates; machine time at each printer's machine rate where it has one
    (#4625). Blocking."""
    runtime = settings.snapshot().runtime
    machine_rate, printer_rates, serial_rates = orders.machine_rates.money_rates()
    return CostRates(
        shop_currency=runtime.orders_currency,
        machine_rate_per_hour=machine_rate,
        printer_rates=printer_rates,
        serial_rates=serial_rates,
        labour_per_item=runtime.pricing_labour_per_item,
        thin_margin_percent=runtime.orders_thin_margin_percent,
    )


def _row(order: Order, margin: OrderMargin) -> OrderRow:
    return OrderRow(
        **order.model_dump(exclude={"minor_exponent"}),
        margin_minor=margin.margin_minor,
        margin_percent=margin.margin_percent,
        margin_state=margin.margin_state,
        margin_estimated=bool(margin.estimated),
    )


def _order_page(
    store: OrderStore,
    filters: OrderFilter,
    margin_state: list[MarginState] | None,
    cursor: str | None,
    limit: int,
    rates: CostRates,
) -> OrderPage:
    """A page of rows. With ``margin_state``, the store's pages are read in turn and
    each order's margin computed until ``limit`` orders match, none are left, or
    ``MAX_SCAN_PAGES`` pages were read: a margin is computed on read, never stored (§5),
    so it cannot be a SQL filter. At the cap the page can be short or empty and still
    have a ``next_cursor``."""
    if not margin_state:
        orders, next_cursor = store.orders(filters, cursor, limit)
        margins = order_margins(store, orders, rates)
        return OrderPage(items=[_row(o, margins[o.id]) for o in orders], next_cursor=next_cursor)
    wanted = set(margin_state)
    found: list[OrderRow] = []
    for _ in range(MAX_SCAN_PAGES):
        orders, more = store.orders(filters, cursor, SCAN_PAGE_SIZE)
        margins = order_margins(store, orders, rates)
        for order in orders:
            if margins[order.id].margin_state not in wanted:
                continue
            if len(found) == limit:
                last = found[-1]
                return OrderPage(items=found, next_cursor=encode_cursor(last.created_at, last.id))
            found.append(_row(order, margins[order.id]))
        if more is None:
            return OrderPage(items=found, next_cursor=None)
        cursor = more
    return OrderPage(items=found, next_cursor=cursor)


@router.get("", response_model=OrderPage, summary="List orders")
async def list_orders(
    request: Request,
    orders: OrdersDep,
    settings: SettingsStoreDep,
    status_: Annotated[list[OrderStatus] | None, Query(alias="status")] = None,
    customer_id: str | None = None,
    q: str | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    accepted_from: datetime | None = None,
    accepted_to: datetime | None = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    product: Annotated[str | None, Query(min_length=1, max_length=210)] = None,
    margin_state: Annotated[list[MarginState] | None, Query()] = None,
    source: OrderSource | None = None,
    unsubmitted: bool = False,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> OrderPage:
    """Newest first, a page at a time: pass ``next_cursor`` back as ``cursor``. ``status``
    may be repeated, for orders of any of them (#5276). Each row carries its margin
    (``margin_minor``, ``margin_percent``, ``margin_state``, ``margin_estimated``): the
    figures ``GET /orders/{id}/costs`` gives the order, read from Postgres only (job cost
    spec §6.2, §8). ``margin_state`` may be repeated, for orders in any of those states
    (``margin_state=loss&margin_state=thin`` is the list's "Loss or thin"). An order with
    no charged figure has no state and never matches it. A ``margin_state`` request reads
    a bounded number of orders, so its page can be short or empty while ``next_cursor``
    remains: follow the cursor until it is null. ``accepted_from`` (inclusive),
    ``accepted_to`` (exclusive), ``currency`` and ``product`` (``model:<slug>``,
    ``sku:<sku>`` or ``custom``) are a profit report row's orders (job cost spec §6.4).
    ``source`` is where an order was made: ``back_office`` or ``storefront`` (#5282).
    The storefront's unsubmitted carts are left out; ``unsubmitted=true`` lists only
    them. A cart has no ``number`` and no ``customer_id`` (both null)."""
    _no_nul(request)
    _unknown(request, LIST_PARAMS)
    try:
        wanted = None if product is None else Product.parse(product)
    except ValueError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
            errors=[{"loc": ["query", "product"], "msg": str(error)}],
        ) from None
    filters = OrderFilter(
        status=status_,
        customer_id=customer_id,
        q=q,
        since=from_,
        until=to,
        accepted_since=accepted_from,
        accepted_until=accepted_to,
        currency=currency,
        product=wanted,
        source=source,
        unsubmitted=unsubmitted,
    )
    if cursor is not None:
        try:
            decode_cursor(cursor)
        except ValueError:
            raise ApiError(400, "cursor is not a next_cursor", type_=BAD_CURSOR) from None
    rates = await asyncio.to_thread(_cost_rates, settings, orders)
    return await asyncio.to_thread(
        _order_page, orders.store, filters, margin_state, cursor, limit, rates
    )


@router.post(
    "", response_model=Order, status_code=status.HTTP_201_CREATED, summary="Create an order"
)
async def create_order(body: OrderCreate, orders: OrdersDep, settings: SettingsStoreDep) -> Order:
    """A draft order, numbered, in the currency `orders_currency` names now (Ruling 8)."""
    currency = await asyncio.to_thread(lambda: settings.snapshot().runtime.orders_currency)
    by = current_actor()
    return await _run(
        lambda: orders.lifecycle.create_order(
            body.customer_id, by, currency=currency, notes=body.notes, due_at=body.due_at
        )
    )


# -- the price book (§5.1, #4250 Ruling 4) ---------------------------------------------------
# Before ``/{order_id}``: a path is matched in the order its routes were declared.


class PolicyUpdate(PricingPolicy):
    """`PUT /orders/price-book`: the policy, whole; the estimate's factor when sent."""

    rough_estimate_factor: str | None = None


def _filament_exists(error: FilamentExistsError) -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        f"{error}: {error.existing_id}",
        type_=PROBLEMS + "filament_exists",
        filament_id=error.existing_id,
    )


@router.get("/price-book", response_model=PriceBook, summary="The price book")
async def get_price_book(orders: OrdersDep, include_archived: bool = False) -> PriceBook:
    """The currency the next order copies, the pricing policy, the rough estimate's
    factor and the filament records not archived, in one read (§5.1);
    ``include_archived`` lists the archived ones too, each with its ``archived_at``."""
    return await asyncio.to_thread(orders.price_book.snapshot, include_archived=include_archived)


@router.put("/price-book", response_model=PriceBook, summary="Set the pricing policy")
async def put_price_book(
    body: PolicyUpdate, orders: OrdersDep, store: SettingsStoreDep, state: StateDep
) -> PriceBook:
    """The policy, saved as the stored settings it is (``pricing_*``, and
    ``rough_estimate_factor`` when sent): the next quote is priced with it, and a
    recorded quote keeps the rates its lines carry."""
    fields: dict[str, Any] = {
        "pricing_machine_rate_per_hour": body.machine_rate_per_hour,
        "pricing_setup_fee": body.setup_fee,
        "pricing_labour_per_item": body.labour_per_item,
        "pricing_markup_percent": body.markup_percent,
        "pricing_rounding": body.rounding,
    }
    if body.rough_estimate_factor is not None:
        fields["rough_estimate_factor"] = body.rough_estimate_factor
    try:
        patch = SettingsPatch.model_validate(fields)
    except ValidationError as error:
        # Every bad field at once, so the Settings form can flag them all.
        # A model-level error has no field in its `loc`: it is the body's.
        problems = [
            {
                "loc": ["body", str(p["loc"][0]).removeprefix("pricing_") if p["loc"] else "body"],
                "msg": p["msg"],
            }
            for p in error.errors()
        ]
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "; ".join(f"{p['loc'][1]}: {p['msg']}" for p in problems),
            errors=problems,
        ) from None
    await asyncio.to_thread(store.save, patch)
    snapshot = await asyncio.to_thread(store.snapshot)
    apply_runtime(state, snapshot.runtime)
    return await asyncio.to_thread(orders.price_book.snapshot)


@router.post(
    "/price-book/filaments",
    response_model=Filament,
    status_code=status.HTTP_201_CREATED,
    summary="Add a filament to the price book",
)
async def create_filament(body: FilamentInput, orders: OrdersDep) -> Filament:
    """409 ``filament_exists`` naming the record of the same material, subtype, brand
    and colour (in any case)."""
    try:
        return await asyncio.to_thread(orders.store.insert_filament, body)
    except FilamentExistsError as error:
        raise _filament_exists(error) from None


@router.patch(
    "/price-book/filaments/{filament_id}",
    response_model=Filament,
    summary="Edit a filament of the price book",
)
async def patch_filament(filament_id: str, body: FilamentPatch, orders: OrdersDep) -> Filament:
    """Only what is sent changes. A quote already priced keeps the rate its lines carry."""
    check_path(filament_id)
    try:
        found = await asyncio.to_thread(orders.store.update_filament, filament_id, body)
    except FilamentExistsError as error:
        raise _filament_exists(error) from None
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no filament {filament_id!r}")
    return found


@router.post(
    "/price-book/filaments/{filament_id}/archive",
    response_model=Filament,
    summary="Archive a filament of the price book",
)
async def archive_filament(filament_id: str, orders: OrdersDep) -> Filament:
    """Hidden from the book and refused by a new quote; never deleted, since an item may
    name it."""
    check_path(filament_id)
    found = await asyncio.to_thread(orders.store.archive_filament, filament_id)
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no filament {filament_id!r}")
    return found


@router.post(
    "/price-book/filaments/{filament_id}/unarchive",
    response_model=Filament,
    summary="Unarchive a filament of the price book",
)
async def unarchive_filament(filament_id: str, orders: OrdersDep) -> Filament:
    """Back in the book with its id, so its quotes and items still name it (#4977). 409
    ``filament_exists`` naming the record that holds its identity now, when the same
    filament was added again after the archive."""
    check_path(filament_id)
    try:
        found = await asyncio.to_thread(orders.store.unarchive_filament, filament_id)
    except FilamentExistsError as error:
        raise _filament_exists(error) from None
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no filament {filament_id!r}")
    return found


@router.post(
    "/price-book/import-spools",
    response_model=SpoolImport,
    summary="Import filament records from Bambuddy's spools",
)
async def import_spools(orders: OrdersDep) -> SpoolImport:
    """One filament record per filament of Bambuddy's spool inventory (§8.3, #4253):
    made when the book has none of its material, subtype, brand and colour, else given
    the most recent priced spool's ``cost_per_kg``; a row's ``min_cost_per_kg`` and
    ``max_cost_per_kg`` show the spread when its spools' prices differ. A filament known
    only by its colour's name, or with no priced spool, is ``skipped`` with its
    ``reason``. Run on click only: no quote reads Bambuddy (Ruling 3). 409 ``not_linked``
    without a Bambuddy URL."""
    try:
        groups = await orders.integrations.spool_groups()
    except NotLinkedError:
        raise not_linked() from None
    try:
        return await asyncio.to_thread(import_spools_into, orders.store, groups)
    except FilamentExistsError as error:
        raise _filament_exists(error) from None


# -- the rough estimate (§5.3, #4250 Ruling 9) ---------------------------------------------------


@router.get("/estimate", response_model=OrderEstimate, summary="A rough estimate of an output")
async def estimate(
    output_id: str, filament_id: str, orders: OrdersDep, outputs: OutputsDep
) -> OrderEstimate:
    """The output's parts' enclosed volumes over every plate, times the filament's
    density and the ``rough_estimate_factor`` for walls, infill and purge (§5.3). A
    read: nothing is recorded. 404 for an unknown output or filament, 422
    ``filament_density`` for a filament with no density."""
    check_path(output_id, filament_id)
    record = await asyncio.to_thread(orders.store.filament, filament_id)
    if record is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no filament {filament_id!r}")
    if record.density_g_cm3 is None:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"filament {filament_id} has no density, so there is no estimate",
            type_=PROBLEMS + "filament_density",
            errors=[{"loc": ["query", "filament_id"], "msg": "the filament has no density"}],
        )
    await asyncio.to_thread(require_output, outputs, output_id)

    def measured() -> list[EstimatedPart]:
        parts: list[EstimatedPart] = []
        plate, plates = 1, 1
        while plate <= plates:
            analysis = outputs.geometry(output_id, plate)
            plates = analysis.plates
            parts += [
                EstimatedPart(
                    plate=plate,
                    part=part.part,
                    name=part.name,
                    colour=part.colour,
                    volume_mm3=part.volume_mm3,
                )
                for part in analysis.parts
            ]
            plate += 1
        return parts

    try:
        parts = await asyncio.to_thread(measured)
    except FileNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no 3MF") from None
    except (ValueError, zipfile.BadZipFile) as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the 3MF of output {output_id!r} cannot be analysed: {error}",
        ) from None
    currency, _, factor = await asyncio.to_thread(orders.price_book.policy)
    return filament_estimate(parts, record, currency=currency, factor=factor)


@router.get(
    "/history/unit-cost",
    response_model=UnitCostHistory,
    summary="A model's real cost per unit from its past jobs",
    description=(
        "The quote warning's history (job cost spec §6.3): the items of `slug` that are "
        "fulfilled on an order that is not cancelled, in `currency` (the shop's by "
        "default), the newest 10 whose last accepted attempt is within 365 days. "
        "`unit_cost_minor` is their real cost (as `GET /orders/{id}/costs` gives each "
        "item) over their units. `main_reason` is the cost part that most exceeded what "
        "their accepted quotes priced for it; failed prints, test prints and cost entries "
        "count in full. `suggested_unit_price_minor` is the unit cost marked up by "
        "`pricing_markup_percent`, or with no markup the lowest price that is not thin. "
        "With `unit_price_minor`, `warning` says that price leaves a margin below "
        "`orders_thin_margin_percent`, and `below_cost` that it is below the unit cost "
        "itself (a price above the cost can still warn with a thin margin). Advice only. "
        "With no history, `jobs` is 0 and nothing warns. With `item_id` (the item being "
        "quoted), its parts at today's unit costs (`parts_unit_minor`, #5543) replace the "
        "past jobs' own parts entries in the unit cost and the suggestion."
    ),
)
async def unit_cost(
    orders: OrdersDep,
    settings: SettingsStoreDep,
    slug: Annotated[str, Query(min_length=1, max_length=200)],
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    unit_price_minor: Annotated[int | None, Query(ge=0)] = None,
    item_id: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> UnitCostHistory:
    check_path(slug, *([item_id] if item_id is not None else []))
    runtime = settings.snapshot().runtime
    parts_unit: Decimal | None = None
    if item_id is not None:
        parts_unit = await asyncio.to_thread(
            orders.lifecycle.parts.unit_cost_of_item,
            item_id,
            currency or runtime.orders_currency,
        )
    return await asyncio.to_thread(
        unit_cost_history,
        orders.store,
        slug,
        await asyncio.to_thread(_cost_rates, settings, orders),
        markup_percent=runtime.pricing_markup_percent,
        now=datetime.now(UTC),
        currency=currency,
        unit_price_minor=unit_price_minor,
        parts_unit_minor=parts_unit,
    )


@router.get(
    "/reports/profit",
    response_model=ProfitReport,
    summary="The profit report by model",
    description=(
        "Job cost spec §6.4: the orders accepted at or after `from` and before `to` "
        f"(by default the last {DEFAULT_DAYS} days), cancelled ones too, per currency "
        "(never summed across currencies) and per product: one row per model `slug`, one "
        "per custom item `sku`, one `custom` row for items with neither. Each row has its "
        "orders, units, what was charged, the real cost, the margin and the waste (the "
        "cost of test and failed attempts). Each item's figures are those "
        "`GET /orders/{id}/costs` gives it. The `order` row is the orders' own figures "
        "(their entries, shipments' cost and charges), over the orders that have any, and "
        "the `total` row each currency's orders' own figures summed. A product row's "
        "`product` is its `GET /orders` filter (`custom` also matches an empty SKU). "
        "The report reads at most `max_orders` orders, newest accepted first: "
        "with `complete` false it covers the orders accepted from `read_from` only."
    ),
)
async def profit(
    request: Request,
    orders: OrdersDep,
    settings: SettingsStoreDep,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    group: ReportGroup = "model",
) -> ProfitReport:
    _no_nul(request)
    _unknown(request, REPORT_PARAMS)
    since, until = default_period(to if to is not None else datetime.now(UTC))
    if from_ is not None:
        since = from_
    if since.tzinfo is None or until.tzinfo is None:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "from and to need a time zone")
    if since >= until:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "from must be before to")
    rates = await asyncio.to_thread(_cost_rates, settings, orders)
    return await asyncio.to_thread(profit_report, orders.store, rates, since=since, until=until)


@router.get("/{order_id}", response_model=OrderDetail, summary="One order, whole")
async def get_order(order_id: str, orders: OrdersDep) -> OrderDetail:
    check_path(order_id)
    store = orders.store

    def read() -> OrderDetail | None:
        order = store.order(order_id)
        if order is None:
            return None
        items = [item_detail(store, item) for item in store.items(order.id)]
        charges = store.charges(order.id)
        payments = store.payments(order.id)
        with store.transaction() as conn:
            requests = requests_for_order(conn, order.id)
        return OrderDetail(
            order=order,
            items=items,
            charges=charges,
            payments=payments,
            shipments=store.shipments(order.id),
            totals=order_totals(
                [(item, item.quotes) for item in items],
                charges,
                payments,
                order=order,
                requests=requests,
            ),
            attachments=store.attachments(order.id),
            projects=store.projects(order.id),
            payment_requests=requests,
        )

    detail = await asyncio.to_thread(read)
    if detail is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    return detail


@router.patch("/{order_id}", response_model=Order, summary="Edit an order")
async def patch_order(
    order_id: str, body: OrderPatch, orders: OrdersDep, service: PaymentsDep
) -> Order:
    check_path(order_id)
    by = current_actor()
    try:
        return await _run_linked(
            lambda: orders.lifecycle.edit_order(order_id, by, body.edit()), order_id, service
        )
    except ApiError as error:
        if error.type == PROBLEMS + "address_not_found":
            field = error.detail.split(":", 1)[0]
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                error.detail,
                errors=[{"loc": ["body", field], "msg": error.detail}],
            ) from None
        raise


@router.put(
    "/{order_id}/charges/after-acceptance",
    response_model=Order,
    summary="Change the charges of an accepted order",
)
async def put_charges_after_acceptance(
    order_id: str, body: ChargesBody, orders: OrdersDep, service: PaymentsDep
) -> Order:
    """#5351: the typed charges, replaced whole, on an order that is `accepted`,
    `in_production` or `ready`, as `PATCH /orders/{id}` takes them before that. The auto
    tax row (#5281) is not part of the list: it is kept and recomputed, also when sent
    back marked by `auto` or its `id`; any other `tax` charge is typed and replaces it. It
    changes what the customer pays: a fee or a discount (negative) keeps an agreed total
    when an item is added after acceptance. The audit row
    ``charges.replaced_after_acceptance`` holds the charges before and after. 409
    ``order_not_accepted`` while the order is `draft` or `quoted` (use `PATCH
    /orders/{id}`), ``order_not_editable`` once it is `shipped`,
    ``order_closed``/``order_cancelled``, and ``order_on_hold``."""
    check_path(order_id)
    by = current_actor()
    return await _run_linked(
        lambda: orders.lifecycle.replace_charges_after_acceptance(order_id, by, body.charges),
        order_id,
        service,
    )


@router.get("/{order_id}/events", response_model=list[OrderEvent], summary="The audit line")
async def order_events(order_id: str, orders: OrdersDep) -> list[OrderEvent]:
    check_path(order_id)
    if await asyncio.to_thread(orders.store.order, order_id) is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    return await asyncio.to_thread(orders.store.events, order_id)


@router.get(
    "/{order_id}/costs",
    response_model=OrderCosts,
    summary="The real cost and margin of an order and each of its items",
    description=(
        "Per item, its accepted quote's seconds, grams and filament cost for the item's "
        "quantity beside every attempt, failures included. Each attempt is costed in "
        "filament and machine time: its actual grams and seconds, else its quote's "
        "(`estimated` names which), at the quote's own rates, else at the price book and "
        "today's machine rate (also `estimated`); a figure with no source is left out and "
        "the attempt is `unknown`. Each item adds its labour (its time entries, else a "
        "default from its quote or `pricing_labour_per_item`, `labour_default`) and its "
        "cost entries; the order adds its own entries and its shipments' cost. "
        "`revenue_minor` is what was charged (an item's accepted quote, 0 once cancelled; "
        "the order's items plus every charge but tax), and `margin_state` is `loss`, "
        "`unknown`, `thin` (below `orders_thin_margin_percent`) or `ok`. With Bambuddy "
        "linked, each attempt's `bambuddy_cost` is Bambuddy's cost of its archives, shown "
        "and never in any sum; `bambuddy_unavailable` says some archive could not be read. "
        "Nothing is stored."
    ),
)
async def get_order_costs(
    order_id: str, orders: OrdersDep, settings: SettingsStoreDep
) -> OrderCosts:
    check_path(order_id)
    rates = await asyncio.to_thread(_cost_rates, settings, orders)
    costs = await order_costs(orders.store, orders.integrations, order_id, rates)
    if costs is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    return costs


@router.post(
    "/{order_id}/time-entries",
    response_model=TimeEntry,
    status_code=status.HTTP_201_CREATED,
    summary="Log time on an order or one of its items",
    description=(
        "In any order status, ended and on hold included, except a storefront cart not yet "
        "submitted (409 `order_not_submitted`). The entry copies the labour rate "
        "in force (`pricing_labour_rate_per_hour`) as `rate_minor_per_hour`, so a later "
        "change never rewrites it; for an order in a currency other than the shop's, no "
        "rate can be copied and it is null. With a `source` (a timer's "
        "`timer:<item or order>:<started_at>`), "
        "a second write of the same order and source returns the first entry as it was "
        "recorded, whatever its kind, and writes nothing (200). On the audit line as "
        "`time.added`."
    ),
    responses={status.HTTP_200_OK: {"model": TimeEntry, "description": "Already recorded"}},
)
async def add_time_entry(
    order_id: str,
    body: TimeEntryInput,
    orders: OrdersDep,
    settings: SettingsStoreDep,
    response: Response,
) -> TimeEntry:
    check_path(order_id)
    by = current_actor()

    runtime = settings.snapshot().runtime
    rate = LabourRate(Decimal(runtime.pricing_labour_rate_per_hour), runtime.orders_currency)
    entry, written = await _run(
        lambda: orders.lifecycle.add_time_entry(order_id, body, by, labour_rate=rate)
    )
    if not written:
        response.status_code = status.HTTP_200_OK
    return entry


@router.delete(
    "/{order_id}/time-entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a time entry",
)
async def remove_time_entry(order_id: str, entry_id: str, orders: OrdersDep) -> Response:
    check_path(order_id, entry_id)
    by = current_actor()
    await _run(lambda: orders.lifecycle.remove_time_entry(order_id, entry_id, by))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{order_id}/cost-entries",
    response_model=CostEntry,
    status_code=status.HTTP_201_CREATED,
    summary="Add a cost to an order or one of its items",
    description=(
        "Money the job cost in the order's currency: the assistant's spend, a payment "
        "fee, materials, anything else. Shipping is a shipment's `cost_minor`, never an "
        "entry. In any order status, except a storefront cart not yet submitted (409 "
        "`order_not_submitted`). With a `source`, a second write of the same order, "
        "kind and source returns the first entry as it was recorded, whatever this "
        "call's amount, note or item, and writes nothing (200). On the audit "
        "line as `cost.added`."
    ),
    responses={status.HTTP_200_OK: {"model": CostEntry, "description": "Already recorded"}},
)
async def add_cost_entry(
    order_id: str, body: CostEntryInput, orders: OrdersDep, response: Response
) -> CostEntry:
    check_path(order_id)
    by = current_actor()
    entry, written = await _run(lambda: orders.lifecycle.add_cost_entry(order_id, body, by))
    if not written:
        response.status_code = status.HTTP_200_OK
    return entry


@router.delete(
    "/{order_id}/cost-entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a cost entry",
)
async def remove_cost_entry(order_id: str, entry_id: str, orders: OrdersDep) -> Response:
    check_path(order_id, entry_id)
    by = current_actor()
    await _run(lambda: orders.lifecycle.remove_cost_entry(order_id, entry_id, by))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


#: How long an accept waits for its Bambuddy projects (§8.1) before answering anyway.
PROJECTS_WAIT_SECONDS = 20.0


@router.post(
    "/{order_id}/accept",
    response_model=Order,
    summary="Accept an order",
    description=(
        "With Bambuddy linked, the order then gets a Bambuddy project named by its number, "
        "and each item that prints one nested under it (`OrderDetail.projects`). That is "
        "best effort: the accept stands without them, and an item's print makes what is "
        "missing."
    ),
)
async def accept_order(order_id: str, orders: OrdersDep, service: PaymentsDep) -> Order:
    """The requests the accept cancels have their pages closed after the commit; the
    balance request it opens is then linked."""
    check_path(order_id)
    by = current_actor()
    try:
        accepted = await service.service.run_closing(
            order_id, lambda: orders.lifecycle.accept_order(order_id, by)
        )
    except LifecycleError as error:
        raise refused(error) from None
    await service.service.link_pending(accepted.id)
    await ensure_order_projects(orders, accepted)
    return accepted


async def ensure_order_projects(orders: OrdersComponent, accepted: Order) -> None:
    """The accepted order's Bambuddy projects, best effort: waited for at most
    `PROJECTS_WAIT_SECONDS`, then left to finish."""
    # Waited for, never cancelled: a create cut off after Bambuddy made the project would
    # leave it unrecorded, and the next ensure would make a second of the same name.
    ensuring = asyncio.create_task(ensure_projects(orders.store, orders.integrations, accepted.id))
    _ENSURING.add(ensuring)
    ensuring.add_done_callback(lambda task: _ensured(task, accepted.label))
    await asyncio.wait({ensuring}, timeout=PROJECTS_WAIT_SECONDS)


#: The accepts' project ensures still running past the wait, kept from the collector.
_ENSURING: set[asyncio.Task[Any]] = set()


def _ensured(task: asyncio.Task[Any], number: str) -> None:
    _ENSURING.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning(
            "could not make the Bambuddy projects of %s", number, exc_info=task.exception()
        )


@router.post("/{order_id}/cancel", response_model=Order, summary="Cancel an order")
async def cancel_order(
    order_id: str, orders: OrdersDep, service: PaymentsDep, body: CancelBody | None = None
) -> Order:
    """The open and failed payment requests end with the order (a pending one refuses the
    cancel, `payment_in_flight`); their pages close after the commit."""
    check_path(order_id)
    by = current_actor()
    reason = body.reason if body else None
    try:
        return await service.service.cancel_order(
            order_id, lambda: orders.lifecycle.cancel_order(order_id, by, reason)
        )
    except LifecycleError as error:
        raise refused(error) from None


@router.post("/{order_id}/hold", response_model=Order, summary="Put an order on hold")
async def hold_order(order_id: str, orders: OrdersDep) -> Order:
    check_path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.hold(order_id, by))


@router.post("/{order_id}/release", response_model=Order, summary="Release an order from hold")
async def release_order(order_id: str, orders: OrdersDep) -> Order:
    check_path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.release(order_id, by))


@router.post("/{order_id}/close", response_model=Order, summary="Close an order")
async def close_order(order_id: str, orders: OrdersDep) -> Order:
    """§3.5's close rule: refused with ``order_not_closable`` naming every item still
    open and every fulfilled item no shipment covers."""
    check_path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.close_order(order_id, by))


@router.post(
    "/{order_id}/reopen",
    response_model=Order,
    summary="Reopen a closed or cancelled order",
    description=(
        "Undoes a close or a cancel. A closed order takes the status its items and "
        "shipments give it (`shipped`, as it closed, unless a row changed since). A "
        "cancelled one goes back to the status it was cancelled from, with the items "
        "that cancel took (an item cancelled on its own before stays cancelled), and "
        "then follows its rows. A cancelled order back past `quoted` that had asked for "
        "money asks for the balance again: a `balance` request opens and is linked "
        "(#5281). 409 `order_not_ended` for an order neither closed nor cancelled."
    ),
)
async def reopen_order(order_id: str, orders: OrdersDep, service: PaymentsDep) -> Order:
    check_path(order_id)
    by = current_actor()
    return await _run_linked(lambda: orders.lifecycle.reopen_order(order_id, by), order_id, service)


# -- the customer link (#5283, storefront spec §5.2) ---------------------------------------


def _to_problem(slug: str, detail: str) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail,
        type_=PROBLEMS + slug,
        errors=[{"loc": ["body", "to"], "msg": detail}],
    )


@router.get(
    "/{order_id}/customer-link",
    response_model=LinkState,
    summary="The state of an order's customer link",
    description=(
        "Whether the order's newest customer link is live, when it was made, the address "
        "its mail went to, when the customer first opened it and when it was revoked; all "
        "null with `live: false` when the order never had one. Never the link's token."
    ),
)
async def get_customer_link(order_id: str, orders: OrdersDep) -> LinkState:
    check_path(order_id)
    return await _run(lambda: orders.lifecycle.customer_link(order_id))


@router.get(
    "/{order_id}/mail",
    response_model=Annotated[list[OrderMail], Field(max_length=ORDER_MAIL_MAX)],
    summary="The customer mail of an order",
    description=(
        "The order's rows in the storefront mail outbox, newest first: the event, the "
        "address, when it was queued, sent or given up, the attempts charged to it (a "
        "recipient's refusals, and server failures answered to its own mail), the last error "
        f"and when the next try is due. At most the newest {ORDER_MAIL_MAX}. Never the body, the "
        "context, a code, a link or a token. A sign-in code has no order and never shows."
    ),
)
async def get_order_mail(order_id: str, orders: OrdersDep) -> list[OrderMail]:
    check_path(order_id)
    return await _run(lambda: orders.lifecycle.order_mail(order_id))


@router.post(
    "/{order_id}/customer-link",
    response_model=LinkState,
    summary="Send the customer a new order link",
    description=(
        "Mails the customer a link that opens this order on the storefront without an "
        "account. With `replace` true, every live link of the order is revoked first, so an "
        "old one stops working. With `replace` false (the default) and a live link, it "
        "answers 409 `link_live` and changes nothing. The mail goes to `to` (one address), "
        "or else to the customer's first email contact that is one address, the primary "
        "one first. A back-office order gets one too. Answers the link's state, never its "
        "token. Audit row `order.customer_link_sent`. 422 `email_invalid` for a `to` that "
        "is not one address, 422 `no_customer` for an order with no customer (set one "
        "first), 422 `no_email` with no `to` and no email contact that is one address (a "
        "malformed contact is skipped), 409 `order_not_submitted` for a cart, 409 "
        "`kek_missing` with no key to seal the link with."
    ),
)
async def send_customer_link(
    order_id: str, orders: OrdersDep, body: CustomerLinkSend | None = None
) -> LinkState:
    check_path(order_id)
    by = current_actor()
    to = body.to if body else None
    replace = body.replace if body else False
    try:
        return await asyncio.to_thread(
            lambda: orders.lifecycle.send_customer_link(order_id, by, to, replace=replace)
        )
    except LifecycleError as error:
        if error.slug == "no_customer":
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "This order has no customer. Set its customer before you send a link.",
                type_=PROBLEMS + "no_customer",
            ) from None
        if error.slug == "no_email":
            raise _to_problem(
                "no_email",
                "The customer has no email contact that is one address. Give the address"
                " to send the link to.",
            ) from None
        raise refused(error) from None
    except EmailInvalidError:
        raise _to_problem(
            "email_invalid", "Give one email address, such as ada@example.com."
        ) from None
    except KekMissingError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT, str(error), type_=PROBLEMS + "kek_missing"
        ) from None


@router.delete(
    "/{order_id}/customer-link",
    response_model=LinkState,
    summary="Revoke an order's customer link",
    description=(
        "Revokes every live customer link of the order: each stops opening the order at "
        "once. Answers the newest link's state. Audit row `order.customer_link_revoked`, "
        "only when a link was live."
    ),
)
async def revoke_customer_link(order_id: str, orders: OrdersDep) -> LinkState:
    check_path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.revoke_customer_links(order_id, by))


@router.post(
    "/{order_id}/payments",
    response_model=Payment,
    status_code=status.HTTP_201_CREATED,
    summary="Record a payment",
)
async def add_payment(
    order_id: str, body: PaymentCreate, orders: OrdersDep, service: PaymentsDep
) -> Payment:
    """A payment that pays open or failed requests closes their pages after the commit."""
    check_path(order_id)
    by = current_actor()
    try:
        return await service.service.add_payment(
            order_id,
            lambda: orders.lifecycle.add_payment(
                order_id,
                by,
                amount_minor=body.amount_minor,
                method=body.method,
                reference=body.reference,
                received_at=body.received_at,
                notes=body.notes,
                payment_request_id=body.payment_request_id,
            ),
        )
    except LifecycleError as error:
        raise refused(error) from None


# -- items (§3.3) ------------------------------------------------------------------------------


async def _resolved(
    body: ReferenceBody,
    output_id: str | None,
    *,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
) -> Any:
    try:
        return await resolve_reference(
            catalogue,
            history,
            outputs,
            paths=paths,
            config=config,
            fetcher=fetcher,
            slug=body.slug,
            commit=body.commit,
            inputs=body.inputs,
            output_id=output_id,
        )
    except ReferenceError as error:
        raise _bad_reference(error) from None
    except TemplateDeletedError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"template {error.slug!r} was deleted; restore it before ordering it",
            type_=TEMPLATE_DELETED,
            slug=error.slug,
        ) from None


@router.post(
    "/{order_id}/items",
    response_model=OrderItem,
    status_code=status.HTTP_201_CREATED,
    summary="Add a line item",
)
async def add_item(
    order_id: str,
    body: ItemCreate,
    orders: OrdersDep,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    settings: SettingsStoreDep,
    library: PrintLibraryDep,
    service: PaymentsDep,
) -> OrderItem:
    """From an output (``output_id``: its commit, inputs, libraries and colours), or by
    hand (``slug``, ``commit``, ``inputs``); either way the reference is checked
    against the catalogue and the commit's schema (Ruling 6). A ``custom`` item (#4954)
    is a title, a quantity and a price, its price a `manual` quote: accepted as it is
    saved, so the item is `approved` (409 ``order_on_hold`` on hold), unless ``accept`` is
    false (the agent's), which leaves it `quoted`. A ``request`` item (#5280) is a title,
    a quantity, notes and a sku, approved design first: it has no design until its first
    revision. Only while the order is `draft` or `quoted`: past that, 409
    ``item_locked`` names `POST /orders/{id}/items/after-acceptance`. A library file
    item (#5544) names ``library_file_id``, a file of the library that serves, read as
    `POST /print/library/{file_id}/run` reads it: a file it does not have (deleted, or
    the other library's) or one it does not print is 422; the item records the file's
    id, name and library, starts `draft`, and is priced by a slice quote of the file or a
    manual quote."""
    return await _add_item(
        order_id,
        body,
        orders,
        after_acceptance=False,
        catalogue=catalogue,
        history=history,
        outputs=outputs,
        paths=paths,
        config=config,
        fetcher=fetcher,
        settings=settings,
        library=library,
        payments=service,
    )


@router.post(
    "/{order_id}/items/after-acceptance",
    response_model=OrderItem,
    status_code=status.HTTP_201_CREATED,
    summary="Add a line item to an accepted order",
)
async def add_item_after_acceptance(
    order_id: str,
    body: ItemCreate,
    orders: OrdersDep,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    settings: SettingsStoreDep,
    library: PrintLibraryDep,
    service: PaymentsDep,
) -> OrderItem:
    """#5349: the same body as `POST /orders/{id}/items`, on an order that is
    `accepted`, `in_production` or `ready`. It changes what the customer gets. The item
    starts unpriced (a custom item `quoted`, its price not accepted; ``accept: true`` is
    422), and needs its own quote and that quote's acceptance before it is printed or
    fulfilled. The order's accepted prices and charges do not change; its status follows
    `derive` (a `ready` order goes back to `in_production`). The audit row is
    ``item.added_after_acceptance``. 409 ``order_not_accepted`` while the order is
    `draft` or `quoted` (use `POST /orders/{id}/items`), ``item_locked`` once it is
    `shipped`, ``order_closed``/``order_cancelled``, and ``order_on_hold``."""
    return await _add_item(
        order_id,
        body,
        orders,
        after_acceptance=True,
        catalogue=catalogue,
        history=history,
        outputs=outputs,
        paths=paths,
        config=config,
        fetcher=fetcher,
        settings=settings,
        library=library,
        payments=service,
    )


@router.post(
    "/{order_id}/items/batch",
    response_model=list[OrderItem],
    status_code=status.HTTP_201_CREATED,
    summary="Add several line items",
)
async def add_items(
    order_id: str,
    body: ItemBatch,
    orders: OrdersDep,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    settings: SettingsStoreDep,
    library: PrintLibraryDep,
    service: PaymentsDep,
) -> list[OrderItem]:
    """#4777 (order operations spec 2026-11-07 §3.1): up to 50 items, each the body of
    `POST /orders/{id}/items` and added as that route adds it, in order, in one
    transaction: any refusal adds none. A body that is refused before the add (a bad
    reference, a library file the library lacks) is a problem that names its place,
    `item_index`, and says `items[<i>]` in its detail. Only while the order is `draft`
    or `quoted`, as the single add; past that use
    `POST /orders/{id}/items/after-acceptance/batch`."""
    return await _add_items(
        order_id,
        body.items,
        orders,
        after_acceptance=False,
        catalogue=catalogue,
        history=history,
        outputs=outputs,
        paths=paths,
        config=config,
        fetcher=fetcher,
        settings=settings,
        library=library,
        payments=service,
        batch=True,
    )


@router.post(
    "/{order_id}/items/after-acceptance/batch",
    response_model=list[OrderItem],
    status_code=status.HTTP_201_CREATED,
    summary="Add several line items to an accepted order",
)
async def add_items_after_acceptance(
    order_id: str,
    body: ItemBatch,
    orders: OrdersDep,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    settings: SettingsStoreDep,
    library: PrintLibraryDep,
    service: PaymentsDep,
) -> list[OrderItem]:
    """`POST /orders/{id}/items/batch` with the rules of
    `POST /orders/{id}/items/after-acceptance` (#5349) for every item."""
    return await _add_items(
        order_id,
        body.items,
        orders,
        after_acceptance=True,
        catalogue=catalogue,
        history=history,
        outputs=outputs,
        paths=paths,
        config=config,
        fetcher=fetcher,
        settings=settings,
        library=library,
        payments=service,
        batch=True,
    )


async def _add_item(
    order_id: str,
    body: ItemCreate,
    orders: OrdersComponent,
    *,
    after_acceptance: bool,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    settings: SettingsStore,
    library: PrintLibrary,
    payments: PaymentsComponent,
) -> OrderItem:
    [item] = await _add_items(
        order_id,
        [body],
        orders,
        after_acceptance=after_acceptance,
        catalogue=catalogue,
        history=history,
        outputs=outputs,
        paths=paths,
        config=config,
        fetcher=fetcher,
        settings=settings,
        library=library,
        payments=payments,
        batch=False,
    )
    return item


async def _add_items(
    order_id: str,
    bodies: list[ItemCreate],
    orders: OrdersComponent,
    *,
    after_acceptance: bool,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    settings: SettingsStore,
    library: PrintLibrary,
    payments: PaymentsComponent,
    batch: bool,
) -> list[OrderItem]:
    """Each body read as `POST /orders/{id}/items` reads it, then all added in one
    transaction. In a batch a refused body's problem names its place, ``item_index``."""
    check_path(order_id)
    if await asyncio.to_thread(orders.store.order, order_id) is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    by = current_actor()
    entries: list[NewItem] = []
    for index, body in enumerate(bodies):
        try:
            entries.append(
                await _new_item(
                    body,
                    orders,
                    after_acceptance=after_acceptance,
                    catalogue=catalogue,
                    history=history,
                    outputs=outputs,
                    paths=paths,
                    config=config,
                    fetcher=fetcher,
                    settings=settings,
                    library=library,
                )
            )
        except ApiError as error:
            if not batch:
                raise
            raise ApiError(
                error.status,
                f"items[{index}]: {error.detail}",
                title=error.title,
                type_=error.type,
                headers=error.headers,
                **{**error.extensions, **_in_item(error.extensions, index)},
                item_index=index,
            ) from None
    return await _run_linked(
        lambda: orders.lifecycle.add_items(
            order_id, by, entries, after_acceptance=after_acceptance
        ),
        order_id,
        payments,
    )


def _in_item(extensions: dict[str, Any], index: int) -> dict[str, Any]:
    """A field refusal's ``errors`` with each body ``loc`` inside ``items[index]``."""
    errors = extensions.get("errors")
    if not isinstance(errors, list):
        return {}
    return {
        "errors": [
            {**e, "loc": ["body", "items", index, *e["loc"][1:]]}
            if isinstance(e, dict) and list(e.get("loc", []))[:1] == ["body"]
            else e
            for e in errors
        ]
    }


async def _new_item(
    body: ItemCreate,
    orders: OrdersComponent,
    *,
    after_acceptance: bool,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    settings: SettingsStore,
    library: PrintLibrary,
) -> NewItem:
    """What one body adds: a library file read from the library that serves, a model
    item's reference resolved, a past print's item of the kind its archive gives."""
    if body.archive_id is not None:
        return await _archive_item(
            body,
            body.archive_id,
            orders,
            catalogue=catalogue,
            history=history,
            outputs=outputs,
            paths=paths,
            config=config,
            fetcher=fetcher,
            settings=settings,
            library=library,
        )
    if body.kind == "library_file":
        assert body.library_file_id is not None
        file = await _library_file(body.library_file_id, settings, library)
        return NewLibraryItem(
            file=file, quantity=body.quantity, title=body.title, notes=body.notes, sku=body.sku
        )
    if body.kind == "custom":
        assert body.title is not None and body.unit_price_minor is not None
        # After acceptance an unset accept means false: the price waits for its own accept.
        # Before, unset is the order's default (`price_accepted`): accepted on a
        # back-office order, left for the customer on a storefront one (#5283).
        accept = (body.accept is True) if after_acceptance else body.accept
        return NewCustomItem(
            title=body.title.strip(),
            quantity=body.quantity,
            unit_price_minor=body.unit_price_minor,
            notes=body.notes,
            sku=body.sku,
            requires_printing=bool(body.requires_printing),
            accept=accept,
        )
    if body.kind == "request":
        assert body.title is not None
        return NewRequestItem(
            title=body.title.strip(), quantity=body.quantity, notes=body.notes, sku=body.sku
        )
    resolved = await _resolved(
        body,
        body.output_id,
        catalogue=catalogue,
        history=history,
        outputs=outputs,
        paths=paths,
        config=config,
        fetcher=fetcher,
    )
    return NewModelItem(
        resolved=resolved,
        output_id=body.output_id,
        quantity=body.quantity,
        title=body.title,
        notes=body.notes,
        sku=body.sku,
    )


async def _archive_item(
    body: ItemCreate,
    archive_id: int,
    orders: OrdersComponent,
    *,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    settings: SettingsStore,
    library: PrintLibrary,
) -> NewItem:
    """A past print's item (#4777, order operations spec §3.2): a model item of the output
    that printed the archive, else a library file item of the file that printed it, else
    one of a copy of its printed file in the native library. An output or file gone since,
    or an output whose template was deleted, falls through to the copy. Each keeps the
    archives of its print run."""
    store = orders.store
    run = await asyncio.to_thread(store.archive_run_plates, archive_id)
    origin_ids = tuple(sorted({found for _, found in run if found is not None} | {archive_id}))
    origin = await asyncio.to_thread(store.archive_origin, archive_id)
    common: dict[str, Any] = {
        "quantity": body.quantity,
        "title": body.title,
        "notes": body.notes,
        "sku": body.sku,
        "origin_archive_ids": origin_ids,
    }
    if origin is not None and origin.output_id is not None:
        try:
            resolved = await _resolved(
                ReferenceBody(),
                origin.output_id,
                catalogue=catalogue,
                history=history,
                outputs=outputs,
                paths=paths,
                config=config,
                fetcher=fetcher,
            )
        except ApiError as error:
            # An output gone since (422), or kept while its template is deleted (409
            # `template_deleted`), falls through: the archive's file is still Bambuddy's.
            if error.status != status.HTTP_422_UNPROCESSABLE_CONTENT and error.type != (
                TEMPLATE_DELETED
            ):
                raise
        else:
            return NewModelItem(resolved=resolved, output_id=origin.output_id, **common)
    if origin is not None and origin.library_file_id is not None:
        try:
            file = await _library_file(origin.library_file_id, settings, library)
        except ApiError as error:
            if error.status != status.HTTP_422_UNPROCESSABLE_CONTENT:
                raise
        else:
            return NewLibraryItem(file=file, **common)
    native = await library.serving_native()
    if native is None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"archive {archive_id} was printed by nothing of ScadBuddy's, so its file is"
            " copied into the native library, and the native library does not serve",
            type_=PROBLEMS + "archive_copy_not_native",
            archive_id=archive_id,
        )
    file = await copied_file(
        store, orders.integrations, NativeArchiveCopies(native), archive_id, native.spool
    )
    return NewLibraryItem(file=file, **common)


async def _library_file(
    file_id: int, settings: SettingsStore, library: PrintLibrary
) -> LibraryFileRef:
    """The file a library file item names (#5544), read from the library that serves as
    `POST /print/library/{file_id}/run` reads it: 422 ``library_file_id`` for a file it
    does not have (deleted, trashed, or the other library's) or one it does not print."""
    found = await ServedLibrary(settings, library.serving_native).file(file_id)
    if found is None:
        raise _bad_library_file(f"no library file {file_id} in the library that serves")
    if not printable(found.file_type):
        raise _bad_library_file(
            f"library file {found.filename} is a {found.file_type or 'file of unknown type'}:"
            " ScadBuddy prints only 3MF and STL files"
        )
    return LibraryFileRef(
        id=file_id,
        filename=found.filename,
        library="native" if is_native_id(file_id) else "bambuddy",
    )


def _bad_library_file(message: str) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        message,
        errors=[{"loc": ["body", "library_file_id"], "msg": message}],
    )


@router.patch("/{order_id}/items/{item_id}", response_model=OrderItem, summary="Edit a line item")
async def patch_item(
    order_id: str,
    item_id: str,
    body: ItemPatch,
    orders: OrdersDep,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    service: PaymentsDep,
) -> OrderItem:
    check_path(order_id, item_id)
    given = body.model_fields_set
    resolved = None
    if body.reference is not None or ("output_id" in given and body.output_id is not None):
        resolved = await _resolved(
            body.reference or ReferenceBody(),
            body.output_id,
            catalogue=catalogue,
            history=history,
            outputs=outputs,
            paths=paths,
            config=config,
            fetcher=fetcher,
        )
    output_id: str | UnsetType | None = body.output_id if "output_id" in given else UNSET
    by = current_actor()
    return await _run_linked(
        lambda: orders.lifecycle.update_item(
            order_id,
            item_id,
            by,
            reference=resolved,
            output_id=output_id,
            quantity=body.quantity,
            title=body.title,
            notes=body.notes,
            filaments=body.filaments,
            unit_price_minor=body.unit_price_minor,
            sku=body.sku if "sku" in given else UNSET,
            requires_printing=body.requires_printing,
            accept=body.accept,
            approval=body.approval,
        ),
        order_id,
        service,
    )


@router.delete(
    "/{order_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a draft line item",
)
async def delete_item(
    order_id: str, item_id: str, orders: OrdersDep, service: PaymentsDep
) -> Response:
    """Only while the item is `draft` (Ruling 10): else 409 ``item_not_draft`` names the
    cancel route, and the caller decides. A draft item with time or cost entries is 409
    ``item_has_entries`` (#5326): remove them, or cancel it."""
    check_path(order_id, item_id)
    by = current_actor()
    await _run_linked(
        lambda: orders.lifecycle.delete_item(order_id, item_id, by), order_id, service
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{order_id}/items/{item_id}/reopen",
    response_model=OrderItem,
    summary="Reopen a cancelled line item",
    description=(
        "Undoes a cancel: the item takes the state its quotes and attempts give it "
        "(`approved`, `failed`, `quoted` or `draft`). 409 `item_not_cancelled`, "
        "`item_not_priced` (a `shipped` order takes back a priced item only), and "
        "`order_closed`/`order_cancelled`."
    ),
)
async def reopen_item(
    order_id: str, item_id: str, orders: OrdersDep, service: PaymentsDep
) -> OrderItem:
    check_path(order_id, item_id)
    by = current_actor()
    return await _run_linked(
        lambda: orders.lifecycle.reopen_item(order_id, item_id, by), order_id, service
    )


@router.post(
    "/{order_id}/items/{item_id}/cancel", response_model=OrderItem, summary="Cancel a line item"
)
async def cancel_item(
    order_id: str, item_id: str, orders: OrdersDep, service: PaymentsDep
) -> OrderItem:
    check_path(order_id, item_id)
    by = current_actor()
    return await _run_linked(
        lambda: orders.lifecycle.cancel_item(order_id, item_id, by), order_id, service
    )


# -- design revisions (#5280, spec 2026-11-02 §4.3) -----------------------------------------


@router.post(
    "/{order_id}/items/{item_id}/revisions",
    response_model=Revision,
    status_code=status.HTTP_201_CREATED,
    summary="Add a design revision",
    description=(
        "A new design for the item while its order is open: the `reference` or "
        "`output_id` to change it, or neither to repeat the current one (new photos, a "
        "new note). 404 `item_not_found`; 409 `item_custom` (a custom item has no "
        "design), `item_has_no_design` (a request's first revision must name one), "
        "`item_not_open`, `item_locked` (the order is accepted and the item's price with "
        "it; an item added after acceptance with no accepted quote still takes one, and "
        "so does an item revised after acceptance, #5350; revise an accepted item with "
        "`POST .../revisions/after-acceptance`) and `order_closed`/`order_cancelled`. "
        "Allowed on hold: a revision advances nothing."
    ),
)
async def add_revision(
    order_id: str,
    item_id: str,
    body: RevisionCreate,
    orders: OrdersDep,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    service: PaymentsDep,
) -> Revision:
    return await _add_revision(
        order_id,
        item_id,
        body,
        orders,
        after_acceptance=False,
        catalogue=catalogue,
        history=history,
        outputs=outputs,
        paths=paths,
        config=config,
        fetcher=fetcher,
        payments=service,
    )


@router.post(
    "/{order_id}/items/{item_id}/revisions/after-acceptance",
    response_model=Revision,
    status_code=status.HTTP_201_CREATED,
    summary="Revise an accepted item's design",
)
async def add_revision_after_acceptance(
    order_id: str,
    item_id: str,
    body: RevisionCreate,
    orders: OrdersDep,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    service: PaymentsDep,
) -> Revision:
    """#5350: the same body as `POST /orders/{id}/items/{item}/revisions`, for an item
    with an accepted quote on an order that is `accepted`, `in_production` or `ready`.
    It changes what the customer gets. The accepted quote stays accepted: the order's
    total keeps the agreed price until a quote of the new revision is accepted. The item
    is `draft` again and `revised_after_acceptance`, so it takes a quote and that quote's
    acceptance, and is not printed or fulfilled before. The audit row is
    ``revision.added_after_acceptance``. 409 ``order_not_accepted`` while the order is
    `draft` or `quoted` (use the plain route), ``order_closed``/``order_cancelled``,
    ``order_on_hold``, ``item_not_open`` for a fulfilled or cancelled item (every item of
    a `ready` or `shipped` order is one, so the route never works there and its
    ``item_locked`` for `shipped` is not reached), ``item_not_priced`` for an item with
    no accepted quote (use the plain route), ``item_custom``, ``print_in_flight``
    while a print of it runs, and ``item_partly_fulfilled`` once an attempt of it is
    accepted (#5592)."""
    return await _add_revision(
        order_id,
        item_id,
        body,
        orders,
        after_acceptance=True,
        catalogue=catalogue,
        history=history,
        outputs=outputs,
        paths=paths,
        config=config,
        fetcher=fetcher,
        payments=service,
    )


async def _add_revision(
    order_id: str,
    item_id: str,
    body: RevisionCreate,
    orders: OrdersComponent,
    *,
    after_acceptance: bool,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    payments: PaymentsComponent,
) -> Revision:
    check_path(order_id, item_id)
    given = body.model_fields_set
    resolved = None
    if body.reference is not None or ("output_id" in given and body.output_id is not None):
        resolved = await _resolved(
            body.reference or ReferenceBody(),
            body.output_id,
            catalogue=catalogue,
            history=history,
            outputs=outputs,
            paths=paths,
            config=config,
            fetcher=fetcher,
        )
    output_id: str | UnsetType | None = body.output_id if "output_id" in given else UNSET
    by = current_actor()
    return await _run_linked(
        lambda: orders.lifecycle.add_revision(
            order_id,
            item_id,
            by,
            resolved,
            output_id=output_id,
            filaments=body.filaments,
            note=body.note,
            after_acceptance=after_acceptance,
        ),
        order_id,
        payments,
    )


@router.post(
    "/{order_id}/items/{item_id}/revisions/{revision_id}/send",
    response_model=Revision,
    summary="Send a revision to the customer",
    description=(
        "Phase 1 stamps `sent_at` only; nothing is delivered to the customer. Sending a "
        "revision already sent and unanswered returns it. 404 `revision_not_found`; 409 "
        "`revision_not_newest`, `revision_answered`, `revision_empty` (no file on it), "
        "`revision_not_quoted` (a `together` item needs a quote that names it), "
        "`item_not_open` and `order_closed`/`order_cancelled`."
    ),
)
async def send_revision(
    order_id: str, item_id: str, revision_id: str, orders: OrdersDep
) -> Revision:
    check_path(order_id, item_id, revision_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.send_revision(order_id, item_id, revision_id, by))


@router.post(
    "/{order_id}/items/{item_id}/revisions/{revision_id}/answer",
    response_model=Revision,
    summary="Record the customer's answer to a revision",
    description=(
        "`accepted` or `changes_requested` (which needs a note) on the newest sent "
        "revision; on a `together` item an acceptance also accepts the quote that names "
        "it. 404 `revision_not_found`; 409 `revision_not_newest`, `revision_not_sent`, "
        "`revision_answered`, `revision_not_quoted`, `item_not_open` and "
        "`order_closed`/`order_cancelled`/`order_on_hold`. On a `storefront` order an "
        "`accepted` is the customer's: the shop's is 409 `customer_accepts` (use "
        "`POST …/accept-for-customer`); the customer's accept that leaves every open "
        "item `approved` accepts the order."
    ),
)
async def answer_revision(
    order_id: str,
    item_id: str,
    revision_id: str,
    body: RevisionAnswerInput,
    orders: OrdersDep,
    service: PaymentsDep,
) -> Revision:
    check_path(order_id, item_id, revision_id)
    by = current_actor()
    return await _run_closing_linked(
        lambda: orders.lifecycle.answer_revision(order_id, item_id, revision_id, body, by),
        order_id,
        orders,
        service,
    )


@router.post(
    "/{order_id}/items/{item_id}/accept-for-customer",
    response_model=OrderItem,
    summary="Accept a revision or a quote for a storefront customer",
    description=(
        "Storefront spec §6.1: on a `storefront` order only the customer accepts a revision "
        "or a quote, and the shop's own accept is 409 `customer_accepts`. This accepts "
        "`target_id` (the newest sent revision, or a quote of the item) for the customer, "
        "as their accept would, and keeps `reason` in the audit row "
        "`item.accepted_for_customer`. The accept that leaves every open item `approved` "
        "accepts the order. 422 for a blank `reason`; 404 `revision_not_found`, "
        "`quote_not_found`; 409 `order_not_storefront` (a back-office order: accept it "
        "directly), and the refusals of the revision answer and the quote accept."
    ),
)
async def accept_for_customer(
    order_id: str,
    item_id: str,
    body: AcceptForCustomerBody,
    orders: OrdersDep,
    service: PaymentsDep,
) -> OrderItem:
    check_path(order_id, item_id, body.target_id)
    by = current_actor()
    return await _run_closing_linked(
        lambda: orders.lifecycle.accept_for_customer(
            order_id, item_id, body.what, body.target_id, body.reason, by
        ),
        order_id,
        orders,
        service,
    )


# -- quotes (§3.4: manual, or from a slice since phase 2) ----------------------------------


@router.post(
    "/{order_id}/items/{item_id}/quotes",
    response_model=Quote,
    status_code=status.HTTP_201_CREATED,
    summary="Record a typed price",
)
async def record_quote(
    order_id: str, item_id: str, body: ManualQuoteInput, orders: OrdersDep, service: PaymentsDep
) -> Quote:
    check_path(order_id, item_id)
    by = current_actor()
    return await _run_linked(
        lambda: orders.lifecycle.record_manual_quote(order_id, item_id, body, by),
        order_id,
        service,
    )


@router.post(
    "/{order_id}/items/{item_id}/quotes/archive",
    response_model=Quote,
    status_code=status.HTTP_201_CREATED,
    summary="Quote an item from a past print",
)
async def quote_item_from_archive(
    order_id: str, item_id: str, body: ArchiveQuoteRequest, orders: OrdersDep, service: PaymentsDep
) -> Quote:
    """An `archive` quote (#4777, order operations spec §3.2): priced from what a past
    print used, at the price book in force, with no re-slice. Every plate archive of the
    print run is read (grams per slot as the slicer estimated them, the actual seconds,
    else the estimate); the body's filaments are written on the item as a slice quote's
    are. Refused as a slice quote's check is (404, 409 ``order_not_open``,
    ``item_not_open``, 422 ``filaments``, ``price_book``), and 409
    ``archive_plates_missing`` (a plate of the run has no archive),
    ``archive_not_succeeded`` (a failed or cancelled print), ``archive_gone``,
    ``not_linked``; 422 ``archive_id`` for an item taken from no past print and none
    named, or an archive that is not the item's print (not taken from it, and printed by
    neither its output nor its library file), ``archive_plate`` for an archive that records
    no grams or time."""
    check_path(order_id, item_id)
    by = current_actor()
    before = await service.service.request_ids(order_id)
    quote = await quote_from_archive(
        orders.lifecycle, orders.integrations, orders.price_book, order_id, item_id, body, by
    )
    await service.service.link_opened(order_id, before)
    return quote


@router.post(
    "/{order_id}/items/{item_id}/quote",
    response_model=Quote,
    summary="Quote an item from its slice",
    responses=OPERATION_RESPONSES,
)
async def quote_item(
    order_id: str,
    item_id: str,
    body: ItemSliceQuoteRequest,
    response: Response,
    ops: OperationsDep,
    orders: OrdersDep,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    service: PaymentsDep,
    idempotency_key: IdempotencyKey = None,
) -> Quote | JSONResponse:
    """The ``order_item_quote`` operation (#4250, §5.2): render the item if it has no
    matching output, slice every plate with the print dialog's choices, read each
    plate's grams and seconds, price them with the book in force and record a `slice`
    quote. Answered 202 once recorded; follow ``GET /operations/{id}`` to the quote.
    Refused before anything is written: 404 for an unknown order or item, 409
    ``order_not_open`` and ``item_not_open``, 422 ``filaments`` (a slot with no
    filament, or one archived) and ``price_book`` (a book that prices nothing).

    With ``inputs`` or ``commit`` that differ from the item's (#4673, order operations
    spec §5.1) it first makes a revision of them, as ``POST …/revisions`` does, or as
    ``…/revisions/after-acceptance`` does for an item with an accepted quote on an
    accepted order: that path's refusals apply unchanged (``item_locked``,
    ``order_on_hold``, ``print_in_flight``, ``deposit_unpaid``, …), and 422 on
    ``inputs`` or ``commit`` as a revision's. The quote is of that revision; one made
    after it refuses the quote (409 ``revision_superseded``: slice again). The body's
    own checks above run before the revision, so their refusals write nothing; a
    storefront cart is refused first (``order_not_submitted``). A replayed key is
    answered from its operation with no revision made, and the same design again makes
    none, so two first sends of one key racing make one. The new revision's slots are
    known only once it renders: each takes the filament the item had on that slot
    before, unless the body names one. A slot the new design adds that neither names
    is refused by the quote's run (422 ``filaments``) after the revision is written:
    the revision stays, and a slice that names its filament quotes it."""
    check_path(order_id, item_id)
    kind = ops.kinds[ORDER_ITEM_QUOTE]
    subject = subject_of(item_id)
    request: dict[str, Any] = {
        "order_id": order_id,
        "item_id": item_id,
        "request": body.model_dump(mode="json", exclude={"inputs", "commit"}),
        # Ruling 12: who it is for comes from the author header, never the body.
        "by": current_actor().model_dump(mode="json"),
        "revision_id": None,
    }
    before_start = None
    if body.inputs is not None or body.commit is not None:
        # #4673: the design keys the request; the revision it makes does not (`unkeyed`).
        request["design"] = body.model_dump(mode="json", include={"inputs", "commit"})

        async def revise() -> None:
            # Only when no record answers the key and its operation is not running
            # (#5761 review): the body's own checks first, so a refusal writes nothing.
            await kind.check({**request, BODY_ONLY: True})
            request["revision_id"], request[CARRIED] = await _slice_revision(
                order_id,
                item_id,
                body,
                orders,
                catalogue=catalogue,
                history=history,
                outputs=outputs,
                paths=paths,
                config=config,
                fetcher=fetcher,
                payments=service,
            )

        before_start = revise
    result = await run_operation(
        ops,
        response,
        kind=kind,
        subject=subject,
        request=request,
        idempotency_key=idempotency_key,
        before_start=before_start,
    )
    return operation_answer(result, Quote)


async def _slice_revision(
    order_id: str,
    item_id: str,
    body: ItemSliceQuoteRequest,
    orders: OrdersComponent,
    *,
    catalogue: CatalogueDep,
    history: HistoryDep,
    outputs: OutputsDep,
    paths: PathsDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    payments: PaymentsComponent,
) -> tuple[str | None, dict[str, str]]:
    """The revision a slice with new ``inputs`` or ``commit`` quotes (#4673): made
    through the revise path, or the item's current one when the design is unchanged;
    and the filaments the item had pinned, by slot, for the new revision's render."""
    store = orders.store
    order = await asyncio.to_thread(store.order, order_id)
    if order is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    try:
        # A visitor's cart first: a visitor-facing path never reads a revision.
        check_submitted(order)
    except LifecycleError as error:
        raise refused(error) from None
    item = await asyncio.to_thread(store.item, item_id)
    if item is None or item.order_id != order.id:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order_id!r}")
    current = item.reference
    if current is None:
        # Only a model item's design takes new inputs; the revise path names why not.
        slug = {"custom": "item_custom", "library_file": "item_library_file"}.get(
            item.kind, "item_has_no_design"
        )
        raise refused(
            LifecycleError(slug, f"item {item_id} has no design to slice with new inputs")
        )
    resolved = await _resolved(
        ReferenceBody(
            slug=current.slug,
            commit=body.commit if body.commit is not None else current.commit,
            inputs=body.inputs if body.inputs is not None else current.inputs,
        ),
        None,
        catalogue=catalogue,
        history=history,
        outputs=outputs,
        paths=paths,
        config=config,
        fetcher=fetcher,
    )
    pinned = {str(slot.slot): slot.filament_id for slot in item.colours if slot.filament_id}
    if (resolved.reference.commit, resolved.reference.inputs) == (current.commit, current.inputs):
        return item.current_revision_id, pinned
    by = current_actor()
    revision = await _run_linked(
        lambda: orders.lifecycle.add_revision(order_id, item_id, by, resolved, reuse_current=True),
        order_id,
        payments,
    )
    return revision.id, pinned


@router.post(
    "/{order_id}/items/{item_id}/quotes/{quote_id}/accept",
    response_model=OrderItem,
    summary="Accept a quote",
    description=(
        "On a `storefront` order only the customer accepts a quote: the shop's accept is "
        "409 `customer_accepts` (use `POST …/accept-for-customer`)."
    ),
)
async def accept_quote(
    order_id: str, item_id: str, quote_id: str, orders: OrdersDep, service: PaymentsDep
) -> OrderItem:
    check_path(order_id, item_id, quote_id)
    by = current_actor()
    return await _run_closing_linked(
        lambda: orders.lifecycle.accept_quote(order_id, item_id, quote_id, by),
        order_id,
        orders,
        service,
    )


@router.get(
    "/{order_id}/items/{item_id}/quotes/{quote_id}/plates/{plate}/thumbnail",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}, 404: {"description": "No such image"}},
    summary="A slice quote's plate image",
    description=(
        "The image the quote's sliced file holds for plate `plate` (its plates' `index`), "
        "as the item's print setups show it (#4673). 404 for a quote of another item, a "
        "plate it did not slice, a quote with no sliced file (manual, archive) or one "
        "whose file holds no image."
    ),
)
async def quote_plate_thumbnail(
    order_id: str, item_id: str, quote_id: str, plate: int, orders: OrdersDep, state: StateDep
) -> Response:
    check_path(order_id, item_id, quote_id)
    item = await asyncio.to_thread(orders.store.item, item_id)
    quote = await asyncio.to_thread(orders.store.quote, quote_id)
    if item is None or item.order_id != order_id or quote is None or quote.order_item_id != item_id:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no quote {quote_id!r} of item {item_id!r}")
    image = await plate_image(state.store.blobs, quote, plate)
    if image is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"quote {quote_id!r} has no image of plate {plate}"
        )
    # A quote's sliced file never changes: its key is its digest.
    return Response(
        image,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


# -- printing an item (§5.4) ------------------------------------------------------------------


@router.post(
    "/{order_id}/items/{item_id}/print",
    response_model=Fulfilment,
    summary="Print an item from its accepted quote",
    responses=OPERATION_RESPONSES,
)
async def print_item(
    order_id: str,
    item_id: str,
    body: ItemPrintRequest,
    response: Response,
    http: Request,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Fulfilment | JSONResponse:
    """The ``order_item_print`` operation (#4719, §5.4): a print run of the item's output
    with the accepted quote's print setup (#4673: its printer, path, choices, tower,
    supports, sequence and spools; each the body leaves out is the quote's, so ``{}``
    prints what was quoted), started as ``POST /print/outputs/{id}/run`` starts one,
    for every plate and the quantity still to print (the body's ``copies``, plates and
    ``request_id`` are the item's, not the caller's). Answers with the attempt it
    recorded, the item `queued`; the item then follows the run (`printing`, `printed` or
    `failed`), and a print whose end could not be read is settled by ``PATCH``ing the
    attempt. Refused before anything starts: 404 for an unknown order or item; 409
    ``order_not_accepted``, ``order_on_hold``, ``order_closed``/``order_cancelled``,
    ``item_custom``, ``item_not_open``,
    ``item_not_approved`` (one printing already), ``item_made`` (its attempts made its
    quantity: accept them), ``print_in_flight`` (a test print of the item is still
    open), ``print_request_conflict`` (the key's print is another item's, or the key's
    test print), and ``requote_needed`` (no accepted quote, its output gone,
    or a slice quote whose sliced file is gone or no longer the file it priced, or a
    ``part_settings``, ``printer_id``, ``printer``, ``choices``, ``prime_tower``,
    ``supports`` or ``print_sequence`` sent that the quote was not sliced with; each one
    omitted, and an omitted ``path``, is the quote's, #5571, #4673; another ``path``
    slices again on its route, #5352); 422 ``print_setup_missing`` when the quote records
    no setup (manual, archive) and the body sends no ``choices`` or ``filament_plan``;
    and the print run's own refusals, as the print route answers them."""
    check_path(order_id, item_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[ORDER_ITEM_PRINT],
        subject=subject_of(item_id),
        request={
            "order_id": order_id,
            "item_id": item_id,
            "request": body.model_dump(mode="json", exclude_unset=True),
            # The print run's key: a re-send (the same Idempotency-Key) repeats its run.
            # Without a key `run_operation` answers 428 before this is read.
            "request_id": print_request_id(item_id, idempotency_key or ""),
            # Ruling 12: who it is for comes from the author header, never the body.
            "by": current_actor().model_dump(mode="json"),
            # #5602: a caller other than the page is told of `path` in a refusal.
            **({"api": True} if asked_by_api(http) else {}),
        },
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, Fulfilment)


@router.post(
    "/{order_id}/items/{item_id}/revisions/{revision_id}/test-print",
    response_model=Fulfilment,
    summary="Test-print a revision of an item",
    responses=OPERATION_RESPONSES,
)
async def test_print_item(
    order_id: str,
    item_id: str,
    revision_id: str,
    body: ItemPrintRequest,
    response: Response,
    http: Request,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Fulfilment | JSONResponse:
    """A test print of the item's current revision (storefront spec §4.4): the
    ``order_item_print`` operation as ``POST …/print`` runs it, for one copy, recorded as
    an attempt of purpose `test` naming the revision. It counts for nothing: the item's
    status, its made quantity and the order's status stay as they were, and it is never
    accepted. Allowed on an order not yet accepted and for an item in any open state.
    Refused before anything starts: 404 for an unknown order, item or revision
    (``revision_not_found``); 409 ``order_on_hold``, ``order_closed``/``order_cancelled``,
    ``item_custom``, ``item_has_no_design``, ``item_not_open``, ``revision_not_newest``
    (only the current revision prints: the print is of the item's output, its render),
    ``print_in_flight`` (a print of the item, of either purpose, is still open),
    ``print_request_conflict`` (the key's print is another item's or a production
    print), and ``requote_needed`` (no slice quote of the revision,
    its sliced file gone or no longer the file it priced, the output gone, or a
    ``part_settings``, ``printer_id``, ``printer``, ``choices``, ``prime_tower``,
    ``supports`` or ``print_sequence`` sent that the quote was not sliced with; each one
    omitted, and an omitted ``path``, is the quote's, #5571, #4673; another ``path``
    slices again on its route, #5352); 422 ``print_setup_missing`` as ``POST …/print``
    answers it; and the print run's own refusals, as the print route answers them."""
    check_path(order_id, item_id, revision_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[ORDER_ITEM_PRINT],
        subject=subject_of(item_id),
        request={
            "order_id": order_id,
            "item_id": item_id,
            "revision_id": revision_id,
            "request": body.model_dump(mode="json", exclude_unset=True),
            "request_id": print_request_id(item_id, idempotency_key or ""),
            "by": current_actor().model_dump(mode="json"),
            **({"api": True} if asked_by_api(http) else {}),
        },
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, Fulfilment)


class DuplicateBody(BaseModel):
    """A line item's duplicate (#4955)."""

    model_config = ConfigDict(extra="forbid")

    #: On an accepted, in-production or ready order (#5349): the copy is added after
    #: acceptance, as `POST /orders/{id}/items/after-acceptance` adds one.
    after_acceptance: bool = False


@router.post(
    "/{order_id}/items/{item_id}/duplicate",
    response_model=OrderItem,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate a line item",
    description=(
        "#4955 (order operations spec 2026-11-07 §4.2): a new item with the item's kind, "
        "current design (reference, colours and output, or library file), past print "
        "archives, quantity, title, notes and SKU. No quote is copied, except a custom "
        "item's price, which is its own field (#4954). Refused as an add is: 409 "
        "`item_locked` past `quoted` without `after_acceptance`, `order_not_accepted` "
        "with it before acceptance, `order_on_hold`, `order_not_submitted` on a visitor's "
        "cart; `template_deleted` for a model item whose template was deleted since (the "
        "copy could not be quoted); 404 for an item not the order's. The copy uses the "
        "parts the item uses per unit."
    ),
)
async def duplicate_item(
    order_id: str,
    item_id: str,
    body: DuplicateBody,
    orders: OrdersDep,
    catalogue: CatalogueDep,
    service: PaymentsDep,
) -> OrderItem:
    check_path(order_id, item_id)
    by = current_actor()
    found = await asyncio.to_thread(orders.store.item, item_id)
    slug = (
        found.reference.slug if found and found.reference and found.order_id == order_id else None
    )
    # A model item names its template, as an add checks it (Ruling 6): a copy of a deleted
    # one could not be quoted. The lifecycle refuses the rest (404 included).
    if slug is not None and not await asyncio.to_thread(catalogue.exists, slug):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"template {slug!r} was deleted; restore it before ordering it",
            type_=TEMPLATE_DELETED,
            slug=slug,
        )
    return await _run_linked(
        lambda: orders.lifecycle.duplicate_item(
            order_id, item_id, by, after_acceptance=body.after_acceptance
        ),
        order_id,
        service,
    )


# -- fulfilment attempts (§6.1 by hand) ------------------------------------------------------


async def _with_archive_facts(orders: OrdersComponent, attempt: Fulfilment) -> Fulfilment:
    """A hand attempt with what its archives say copied onto it (#6132): job cost needs
    its grams and seconds when the quote has none. Logged, never raised: the attempt is
    recorded, and its archives are read again when they are shown."""
    if not attempt.archive_ids or attempt.print_run_id is not None:
        return attempt
    try:
        written, _ = await mirror_archives(orders.lifecycle, orders.integrations, attempt)
    except Exception:
        logger.warning("could not copy a hand attempt's print archives", exc_info=True)
        return attempt
    return written


@router.post(
    "/{order_id}/items/{item_id}/fulfilments",
    response_model=Fulfilment,
    status_code=status.HTTP_201_CREATED,
    summary="Record a fulfilment attempt",
    description=(
        "An attempt by hand. With `archive_id`, the archive's grams, seconds and print "
        "times are copied onto the attempt at once when a Bambuddy is linked (#6132); a "
        "failed read records the attempt without them."
    ),
)
async def record_fulfilment(
    order_id: str, item_id: str, body: FulfilmentInput, orders: OrdersDep
) -> Fulfilment:
    check_path(order_id, item_id)
    by = current_actor()
    attempt = await _run(lambda: orders.lifecycle.record_fulfilment(order_id, item_id, body, by))
    return await _with_archive_facts(orders, attempt)


@router.patch(
    "/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}",
    response_model=Fulfilment,
    summary="Edit an attempt not yet accepted",
    description=(
        "An edit keeps the attempt's archives. An attempt by hand with archives copies "
        "their grams and seconds again when a Bambuddy is linked (#6132), as a read of "
        "its archives does. Only the first copy writes their print times: after it, a time "
        "the edit sets stays. A failed read keeps the edit."
    ),
)
async def update_fulfilment(
    order_id: str, item_id: str, fulfilment_id: str, body: FulfilmentInput, orders: OrdersDep
) -> Fulfilment:
    check_path(order_id, item_id, fulfilment_id)
    by = current_actor()
    attempt = await _run(
        lambda: orders.lifecycle.update_fulfilment(order_id, item_id, fulfilment_id, body, by)
    )
    return await _with_archive_facts(orders, attempt)


class ArchiveCopy(BaseModel):
    """What of the archive to copy (§8.2)."""

    model_config = ConfigDict(extra="forbid")

    what: ArchiveMedia
    #: A ``finish_`` photo of the archive (``AttemptArchives``' ``finish_photos``);
    #: left out, its latest. Only for ``finished_photo``.
    name: str | None = Field(
        default=None, max_length=255, pattern=r"^finish_[A-Za-z0-9_-]+\.[a-z]+$"
    )

    @model_validator(mode="after")
    def _name_for_a_photo(self) -> Self:
        if self.name is not None and self.what != "finished_photo":
            raise ValueError("`name` names a photo: it goes with `finished_photo` only")
        return self


@router.get(
    "/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/archives",
    response_model=AttemptArchives,
    summary="The Bambuddy archives of an attempt's print",
)
async def get_attempt_archives(
    order_id: str, item_id: str, fulfilment_id: str, orders: OrdersDep
) -> AttemptArchives:
    """The archives the attempt's print run made in Bambuddy (one per plate, §8.2), with
    what each offers to copy: its ``finish_`` photos and whether it has a timelapse.
    Reading them copies what they say onto the attempt once its print settled
    (``archive_ids``, the printer's times, ``actual_seconds``, ``actual_grams``).
    ``linked`` is false, with no archives, without Bambuddy; an attempt by hand naming no
    archive, or one Bambuddy has linked no archive to yet, has none. 404 for an attempt
    not the item's."""
    check_path(order_id, item_id, fulfilment_id)
    return await attempt_archives(
        orders.lifecycle, orders.integrations, order_id, item_id, fulfilment_id
    )


@router.post(
    "/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/archives/{archive_id}/copy",
    response_model=Attachment,
    summary="Copy an archive's finish photo or timelapse onto the attempt",
    responses={
        404: {"description": "No such attempt of the item"},
        413: {"description": "Larger than the upload limit in Settings"},
        **OPERATION_RESPONSES,
    },
)
async def copy_archive_media(
    order_id: str,
    item_id: str,
    fulfilment_id: str,
    archive_id: Annotated[int, Path(ge=1)],
    body: ArchiveCopy,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Attachment | JSONResponse:
    """The ``order_attempt_copy`` operation (§8.2, §6.2): the archive's ``finish_``
    photo (``name``, else its latest) or its timelapse pulled once from Bambuddy and
    kept as ScadBuddy's own file, an attachment of the attempt (listed under its item)
    with the role ``finished`` or ``timelapse``; the same bytes again are the attachment
    already there. Refused
    with 409: ``not_linked`` (no Bambuddy), ``archive_not_attempts`` (not one of the
    attempt's prints), ``archive_gone`` (deleted in Bambuddy), ``no_finish_photo``,
    ``no_timelapse`` (pull it off the printer first, ``POST
    /prints/{archive_id}/timelapse/pull``). Allowed in any state of the order, as an
    upload is."""
    check_path(order_id, item_id, fulfilment_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[ORDER_ATTEMPT_COPY],
        subject=subject_of(item_id),
        request={
            "order_id": order_id,
            "item_id": item_id,
            "fulfilment_id": fulfilment_id,
            "archive_id": archive_id,
            "what": body.what,
            "name": body.name,
            # Ruling 12: who added it comes from the author header, never the body.
            "by": current_actor().model_dump(mode="json"),
        },
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, Attachment)


@router.post(
    "/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/accept",
    response_model=Fulfilment,
    summary="Count an attempt's quantity as made",
    description=(
        "The attempt's quantity counts as made; the item is fulfilled once its accepted "
        "attempts reach its quantity. 409 `fulfilment_accepted`, `fulfilment_failed`, "
        "`item_not_open`, and `fulfilment_stale` for an attempt that printed a revision "
        "earlier than the one the accepted quote prices (an item revised after acceptance, "
        "#5350): it printed an earlier design."
    ),
)
async def accept_fulfilment(
    order_id: str, item_id: str, fulfilment_id: str, orders: OrdersDep
) -> Fulfilment:
    check_path(order_id, item_id, fulfilment_id)
    by = current_actor()
    return await _run(
        lambda: orders.lifecycle.accept_fulfilment(order_id, item_id, fulfilment_id, by)
    )


class FulfilBody(BaseModel):
    """A bulk fulfil (#4955): how many of each item were made."""

    model_config = ConfigDict(extra="forbid")

    items: list[FulfilLine] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)


@router.post(
    "/{order_id}/fulfil",
    response_model=list[Fulfilment],
    status_code=status.HTTP_201_CREATED,
    summary="Fulfil several line items at once",
    description=(
        "#4955 (order operations spec 2026-11-07 §4.2): for each item, an attempt by hand "
        "of `quantity` that `succeeded`, recorded and accepted, in one transaction; an item "
        "is fulfilled once its accepted attempts reach its quantity. A refusal of any item "
        "writes nothing: 409 `order_not_accepted`, `order_on_hold`, `item_not_open` (settled) "
        "or `item_not_approved` (not `approved` or `failed`; a printed item's print attempt "
        "is accepted on its own), naming the item in `items`; 422 `items` for an item named "
        "twice; 404 for an item not the order's. The attempts, in the body's order."
    ),
)
async def fulfil_items(order_id: str, body: FulfilBody, orders: OrdersDep) -> list[Fulfilment]:
    check_path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.fulfil_items(order_id, body.items, by))


@router.post(
    "/{order_id}/items/{item_id}/unfulfil",
    response_model=OrderItem,
    summary="Undo a line item's fulfilment",
    description=(
        "Marks the item's latest accepted attempt not accepted (the attempt stays) and "
        "returns the item to the status its latest attempt gives it (`approved` or "
        "`failed`); the order's status follows. 409 `item_not_fulfilled`, "
        "`order_item_shipped` (a shipment carries it: its `shipments` name them), "
        "`order_on_hold`, `order_closed`/`order_cancelled`, and "
        "`unfulfil_disabled` while the stored setting `orders_allow_unfulfil` is off."
    ),
)
async def unfulfil_item(
    order_id: str, item_id: str, orders: OrdersDep, settings: SettingsStoreDep
) -> OrderItem:
    check_path(order_id, item_id)
    by = current_actor()

    def unfulfil() -> OrderItem:
        allowed = settings.snapshot().runtime.orders_allow_unfulfil
        return orders.lifecycle.unfulfil_item(order_id, item_id, by, allowed=allowed)

    return await _run(unfulfil)


# -- shipments (§6.3) --------------------------------------------------------------------------


@router.post(
    "/{order_id}/shipments",
    response_model=Shipment,
    status_code=status.HTTP_201_CREATED,
    summary="Record a shipment",
)
async def add_shipment(order_id: str, body: ShipmentInput, orders: OrdersDep) -> Shipment:
    check_path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.add_shipment(order_id, body, by))


@router.patch(
    "/{order_id}/shipments/{shipment_id}", response_model=Shipment, summary="Edit a shipment"
)
async def update_shipment(
    order_id: str, shipment_id: str, body: ShipmentEdit, orders: OrdersDep
) -> Shipment:
    check_path(order_id, shipment_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.update_shipment(order_id, shipment_id, body, by))
