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
from typing import Annotated, Any, Self

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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
from scadbuddy.api.runtime import apply_runtime
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import require_output
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.operations.component import OperationsDep
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.archives import AttemptArchives, attempt_archives
from scadbuddy.orders.component import OrdersComponent, OrdersDep
from scadbuddy.orders.costs import (
    CostRates,
    MarginState,
    OrderCosts,
    OrderMargin,
    order_costs,
    order_margins,
)
from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.history import UnitCostHistory, unit_cost_history
from scadbuddy.orders.integrations import ArchiveMedia, NotLinkedError, not_linked
from scadbuddy.orders.lifecycle import (
    FulfilmentInput,
    LabourRate,
    LifecycleError,
    ManualQuoteInput,
    OrderEdit,
    ShipmentEdit,
    ShipmentInput,
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
    Money,
    Order,
    OrderEvent,
    OrderFilter,
    OrderItem,
    OrderProject,
    OrderStatus,
    Payment,
    PriceBook,
    PricingPolicy,
    Quote,
    Revision,
    Shipment,
    SpoolImport,
    TimeEntry,
    TimeEntryInput,
)
from scadbuddy.orders.operations import ORDER_ATTEMPT_COPY, ORDER_ITEM_PRINT, ORDER_ITEM_QUOTE
from scadbuddy.orders.pricing import estimate_grams, to_minor
from scadbuddy.orders.printing import print_request_id
from scadbuddy.orders.problems import PROBLEMS, refused
from scadbuddy.orders.projects import ensure_projects
from scadbuddy.orders.quote import SliceQuoteRequest, subject_of
from scadbuddy.orders.reference import ReferenceError, TemplateDeletedError, resolve_reference
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
    {"status", "customer_id", "q", "from", "to", "margin_state", "cursor", "limit"}
)
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
    customer's) and the charges, replaced whole, while `draft` or `quoted`. A field
    left out is untouched; ``null`` clears the due date or an address."""

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
    ``quantity``, ``notes`` and ``sku`` only; its first revision names the design."""

    kind: ItemKind = "model"
    output_id: str | None = None
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
            "A custom item's only: whether its price is accepted as it is saved. Unset or"
            " true leaves the item `approved`; false leaves it `quoted`. On"
            " `POST /orders/{id}/items/after-acceptance` unset means false, and true is"
            " 422: an item added after acceptance needs its quote accepted on its own."
        ),
    )

    @model_validator(mode="after")
    def _shape(self) -> Self:
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
    #: Whether a custom item's new price is accepted (true, the default) or left
    #: `quoted` for an `outward` accept (false, the agent's); nothing else reads it.
    accept: bool = True
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


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


# -- views -------------------------------------------------------------------------------------


class ItemDetail(OrderItem):
    quotes: list[Quote]
    fulfilments: list[Fulfilment]
    revisions: list[Revision]


class OrderTotals(BaseModel):
    """§3.4: the items' prices, the charges on top, what was paid. Every item not
    cancelled counts at its accepted quote's total or, before one is accepted (a draft
    or quoted order), at its newest quote's unit price times its quantity now, so a
    draft adds up as it is entered; ``pending_minor`` is the part of the subtotal no
    accepted quote stands behind yet, and ``unpriced_items`` the items with no quote."""

    subtotal_minor: Money
    #: The part of ``subtotal_minor`` from quotes not yet accepted.
    pending_minor: Money = 0
    #: The items not cancelled with no quote at all, in no sum.
    unpriced_items: list[str] = Field(default_factory=list)
    charges_minor: Money
    total_minor: Money
    paid_minor: Money
    balance_minor: Money


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


def _path(*values: str) -> None:
    if any("\x00" in value for value in values):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "a path value holds a NUL byte")


# -- orders ------------------------------------------------------------------------------------


def _cost_rates(settings: SettingsStore) -> CostRates:
    runtime = settings.snapshot().runtime
    return CostRates(
        shop_currency=runtime.orders_currency,
        machine_rate_per_hour=runtime.pricing_machine_rate_per_hour,
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
    margin_state: Annotated[list[MarginState] | None, Query()] = None,
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
    remains: follow the cursor until it is null."""
    _no_nul(request)
    _unknown(request, LIST_PARAMS)
    filters = OrderFilter(status=status_, customer_id=customer_id, q=q, since=from_, until=to)
    if cursor is not None:
        try:
            decode_cursor(cursor)
        except ValueError:
            raise ApiError(400, "cursor is not a next_cursor", type_=BAD_CURSOR) from None
    rates = await asyncio.to_thread(_cost_rates, settings)
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
    _path(filament_id)
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
    _path(filament_id)
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
    _path(filament_id)
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


class EstimatedPart(BaseModel):
    """One part of one plate: its enclosed volume, or ``None`` when it is not checked
    and closed (``render/geometry.py`` measures only those), and so not counted."""

    plate: int
    part: int
    name: str
    colour: str
    volume_mm3: float | None


class OrderEstimate(BaseModel):
    """A rough estimate (§5.3): never a quote, never stored, never accepted."""

    #: Always true: this is an estimate.
    estimate: bool = True
    #: Decimal string, to the hundredth of a gram.
    grams: str
    #: The grams at the filament's cost per kilogram, rounded as a quote line is.
    filament_minor: Money
    currency: str
    minor_exponent: int
    #: ``rough_estimate_factor`` as it stood.
    factor: str
    parts: list[EstimatedPart]
    #: How many parts have no volume and are left out of ``grams``.
    unknown_parts: int


@router.get("/estimate", response_model=OrderEstimate, summary="A rough estimate of an output")
async def estimate(
    output_id: str, filament_id: str, orders: OrdersDep, outputs: OutputsDep
) -> OrderEstimate:
    """The output's parts' enclosed volumes over every plate, times the filament's
    density and the ``rough_estimate_factor`` for walls, infill and purge (§5.3). A
    read: nothing is recorded. 404 for an unknown output or filament, 422
    ``filament_density`` for a filament with no density."""
    _path(output_id, filament_id)
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
    grams = estimate_grams(
        [part.volume_mm3 for part in parts if part.volume_mm3 is not None],
        record.density_g_cm3,
        factor,
    )
    return OrderEstimate(
        grams=format(grams, "f"),
        filament_minor=to_minor(grams * Decimal(record.cost_per_kg) / 1000, currency),
        currency=currency,
        minor_exponent=minor_exponent(currency),
        factor=factor,
        parts=parts,
        unknown_parts=sum(1 for part in parts if part.volume_mm3 is None),
    )


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
        "With no history, `jobs` is 0 and nothing warns."
    ),
)
async def unit_cost(
    orders: OrdersDep,
    settings: SettingsStoreDep,
    slug: Annotated[str, Query(min_length=1, max_length=200)],
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    unit_price_minor: Annotated[int | None, Query(ge=0)] = None,
) -> UnitCostHistory:
    _path(slug)
    runtime = settings.snapshot().runtime
    return await asyncio.to_thread(
        unit_cost_history,
        orders.store,
        slug,
        _cost_rates(settings),
        markup_percent=runtime.pricing_markup_percent,
        now=datetime.now(UTC),
        currency=currency,
        unit_price_minor=unit_price_minor,
    )


def item_price(item: ItemDetail) -> tuple[Money, bool] | None:
    """The item's price and whether an accepted quote stands behind it: the accepted
    quote's total, else the newest quote's unit price times the item's quantity now;
    None with no quote."""
    accepted = next((quote for quote in item.quotes if quote.accepted), None)
    if accepted is not None:
        return accepted.total_minor, True
    newest = max(item.quotes, key=lambda quote: (quote.created_at, quote.id), default=None)
    if newest is None:
        return None
    return newest.unit_price_minor * item.quantity, False


def _totals(items: list[ItemDetail], charges: list[Charge], payments: list[Payment]) -> OrderTotals:
    subtotal = pending = 0
    unpriced: list[str] = []
    for item in items:
        if item.status == "cancelled":
            continue
        priced = item_price(item)
        if priced is None:
            unpriced.append(item.id)
            continue
        amount, accepted = priced
        subtotal += amount
        if not accepted:
            pending += amount
    charged = sum(charge.amount_minor for charge in charges)
    paid = sum(payment.amount_minor for payment in payments)
    return OrderTotals(
        subtotal_minor=subtotal,
        pending_minor=pending,
        unpriced_items=unpriced,
        charges_minor=charged,
        total_minor=subtotal + charged,
        paid_minor=paid,
        balance_minor=subtotal + charged - paid,
    )


@router.get("/{order_id}", response_model=OrderDetail, summary="One order, whole")
async def get_order(order_id: str, orders: OrdersDep) -> OrderDetail:
    _path(order_id)
    store = orders.store

    def read() -> OrderDetail | None:
        order = store.order(order_id)
        if order is None:
            return None
        items = [
            ItemDetail(
                **item.model_dump(),
                quotes=store.quotes(item.id),
                fulfilments=store.fulfilments(item.id, purpose=None),
                revisions=store.revisions(item.id),
            )
            for item in store.items(order.id)
        ]
        charges = store.charges(order.id)
        payments = store.payments(order.id)
        return OrderDetail(
            order=order,
            items=items,
            charges=charges,
            payments=payments,
            shipments=store.shipments(order.id),
            totals=_totals(items, charges, payments),
            attachments=store.attachments(order.id),
            projects=store.projects(order.id),
        )

    detail = await asyncio.to_thread(read)
    if detail is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    return detail


@router.patch("/{order_id}", response_model=Order, summary="Edit an order")
async def patch_order(order_id: str, body: OrderPatch, orders: OrdersDep) -> Order:
    _path(order_id)
    by = current_actor()
    try:
        return await _run(lambda: orders.lifecycle.edit_order(order_id, by, body.edit()))
    except ApiError as error:
        if error.type == PROBLEMS + "address_not_found":
            field = error.detail.split(":", 1)[0]
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                error.detail,
                errors=[{"loc": ["body", field], "msg": error.detail}],
            ) from None
        raise


@router.get("/{order_id}/events", response_model=list[OrderEvent], summary="The audit line")
async def order_events(order_id: str, orders: OrdersDep) -> list[OrderEvent]:
    _path(order_id)
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
    _path(order_id)
    rates = _cost_rates(settings)
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
        "In any order status, ended and on hold included. The entry copies the labour rate "
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
    _path(order_id)
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
    _path(order_id, entry_id)
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
        "entry. In any order status. With a `source`, a second write of the same order, "
        "kind and source returns the first entry as it was recorded, whatever this "
        "call's amount, note or item, and writes nothing (200). On the audit "
        "line as `cost.added`."
    ),
    responses={status.HTTP_200_OK: {"model": CostEntry, "description": "Already recorded"}},
)
async def add_cost_entry(
    order_id: str, body: CostEntryInput, orders: OrdersDep, response: Response
) -> CostEntry:
    _path(order_id)
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
    _path(order_id, entry_id)
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
async def accept_order(order_id: str, orders: OrdersDep) -> Order:
    _path(order_id)
    by = current_actor()
    accepted = await _run(lambda: orders.lifecycle.accept_order(order_id, by))
    # Waited for, never cancelled: a create cut off after Bambuddy made the project would
    # leave it unrecorded, and the next ensure would make a second of the same name.
    ensuring = asyncio.create_task(ensure_projects(orders.store, orders.integrations, accepted.id))
    _ENSURING.add(ensuring)
    ensuring.add_done_callback(lambda task: _ensured(task, accepted.number))
    await asyncio.wait({ensuring}, timeout=PROJECTS_WAIT_SECONDS)
    return accepted


#: The accepts' project ensures still running past the wait, kept from the collector.
_ENSURING: set[asyncio.Task[Any]] = set()


def _ensured(task: asyncio.Task[Any], number: str) -> None:
    _ENSURING.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning(
            "could not make the Bambuddy projects of %s", number, exc_info=task.exception()
        )


@router.post("/{order_id}/cancel", response_model=Order, summary="Cancel an order")
async def cancel_order(order_id: str, orders: OrdersDep, body: CancelBody | None = None) -> Order:
    _path(order_id)
    by = current_actor()
    reason = body.reason if body else None
    return await _run(lambda: orders.lifecycle.cancel_order(order_id, by, reason))


@router.post("/{order_id}/hold", response_model=Order, summary="Put an order on hold")
async def hold_order(order_id: str, orders: OrdersDep) -> Order:
    _path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.hold(order_id, by))


@router.post("/{order_id}/release", response_model=Order, summary="Release an order from hold")
async def release_order(order_id: str, orders: OrdersDep) -> Order:
    _path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.release(order_id, by))


@router.post("/{order_id}/close", response_model=Order, summary="Close an order")
async def close_order(order_id: str, orders: OrdersDep) -> Order:
    """§3.5's close rule: refused with ``order_not_closable`` naming every item still
    open and every fulfilled item no shipment covers."""
    _path(order_id)
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
        "then follows its rows. 409 `order_not_ended` for an order neither closed nor "
        "cancelled."
    ),
)
async def reopen_order(order_id: str, orders: OrdersDep) -> Order:
    _path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.reopen_order(order_id, by))


@router.post(
    "/{order_id}/payments",
    response_model=Payment,
    status_code=status.HTTP_201_CREATED,
    summary="Record a payment",
)
async def add_payment(order_id: str, body: PaymentCreate, orders: OrdersDep) -> Payment:
    _path(order_id)
    by = current_actor()
    return await _run(
        lambda: orders.lifecycle.add_payment(
            order_id,
            by,
            amount_minor=body.amount_minor,
            method=body.method,
            reference=body.reference,
            received_at=body.received_at,
            notes=body.notes,
        )
    )


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
            type_=PROBLEMS + "template_deleted",
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
) -> OrderItem:
    """From an output (``output_id``: its commit, inputs, libraries and colours), or by
    hand (``slug``, ``commit``, ``inputs``); either way the reference is checked
    against the catalogue and the commit's schema (Ruling 6). A ``custom`` item (#4954)
    is a title, a quantity and a price, its price a `manual` quote: accepted as it is
    saved, so the item is `approved` (409 ``order_on_hold`` on hold), unless ``accept`` is
    false (the agent's), which leaves it `quoted`. A ``request`` item (#5280) is a title,
    a quantity, notes and a sku, approved design first: it has no design until its first
    revision. Only while the order is `draft` or `quoted`: past that, 409
    ``item_locked`` names `POST /orders/{id}/items/after-acceptance`."""
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
) -> OrderItem:
    _path(order_id)
    if await asyncio.to_thread(orders.store.order, order_id) is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    by = current_actor()
    if body.kind == "custom":
        assert body.title is not None and body.unit_price_minor is not None
        title, price = body.title.strip(), body.unit_price_minor
        # After acceptance an unset accept means false: the price waits for its own accept.
        accept = body.accept is True if after_acceptance else body.accept is not False
        return await _run(
            lambda: orders.lifecycle.add_custom_item(
                order_id,
                by,
                title=title,
                quantity=body.quantity,
                unit_price_minor=price,
                notes=body.notes,
                sku=body.sku,
                requires_printing=bool(body.requires_printing),
                accept=accept,
                after_acceptance=after_acceptance,
            )
        )
    if body.kind == "request":
        assert body.title is not None
        request_title = body.title.strip()
        return await _run(
            lambda: orders.lifecycle.add_request_item(
                order_id,
                by,
                title=request_title,
                quantity=body.quantity,
                notes=body.notes,
                sku=body.sku,
                after_acceptance=after_acceptance,
            )
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
    return await _run(
        lambda: orders.lifecycle.add_item(
            order_id,
            by,
            resolved,
            output_id=body.output_id,
            quantity=body.quantity,
            title=body.title,
            notes=body.notes,
            sku=body.sku,
            after_acceptance=after_acceptance,
        )
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
) -> OrderItem:
    _path(order_id, item_id)
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
    return await _run(
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
        )
    )


@router.delete(
    "/{order_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a draft line item",
)
async def delete_item(order_id: str, item_id: str, orders: OrdersDep) -> Response:
    """Only while the item is `draft` (Ruling 10): else 409 ``item_not_draft`` names the
    cancel route, and the caller decides. A draft item with time or cost entries is 409
    ``item_has_entries`` (#5326): remove them, or cancel it."""
    _path(order_id, item_id)
    by = current_actor()
    await _run(lambda: orders.lifecycle.delete_item(order_id, item_id, by))
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
async def reopen_item(order_id: str, item_id: str, orders: OrdersDep) -> OrderItem:
    _path(order_id, item_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.reopen_item(order_id, item_id, by))


@router.post(
    "/{order_id}/items/{item_id}/cancel", response_model=OrderItem, summary="Cancel a line item"
)
async def cancel_item(order_id: str, item_id: str, orders: OrdersDep) -> OrderItem:
    _path(order_id, item_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.cancel_item(order_id, item_id, by))


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
        "it; an item added after acceptance with no accepted quote still takes one) and "
        "`order_closed`/`order_cancelled`. Allowed on hold: a revision advances nothing."
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
) -> Revision:
    _path(order_id, item_id)
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
    return await _run(
        lambda: orders.lifecycle.add_revision(
            order_id,
            item_id,
            by,
            resolved,
            output_id=output_id,
            filaments=body.filaments,
            note=body.note,
        )
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
    _path(order_id, item_id, revision_id)
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
        "`order_closed`/`order_cancelled`/`order_on_hold`."
    ),
)
async def answer_revision(
    order_id: str,
    item_id: str,
    revision_id: str,
    body: RevisionAnswerInput,
    orders: OrdersDep,
) -> Revision:
    _path(order_id, item_id, revision_id)
    by = current_actor()
    return await _run(
        lambda: orders.lifecycle.answer_revision(order_id, item_id, revision_id, body, by)
    )


# -- quotes (§3.4: manual, or from a slice since phase 2) ----------------------------------


@router.post(
    "/{order_id}/items/{item_id}/quotes",
    response_model=Quote,
    status_code=status.HTTP_201_CREATED,
    summary="Record a typed price",
)
async def record_quote(
    order_id: str, item_id: str, body: ManualQuoteInput, orders: OrdersDep
) -> Quote:
    _path(order_id, item_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.record_manual_quote(order_id, item_id, body, by))


@router.post(
    "/{order_id}/items/{item_id}/quote",
    response_model=Quote,
    summary="Quote an item from its slice",
    responses=OPERATION_RESPONSES,
)
async def quote_item(
    order_id: str,
    item_id: str,
    body: SliceQuoteRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Quote | JSONResponse:
    """The ``order_item_quote`` operation (#4250, §5.2): render the item if it has no
    matching output, slice every plate with the print dialog's choices, read each
    plate's grams and seconds, price them with the book in force and record a `slice`
    quote. Answered 202 once recorded; follow ``GET /operations/{id}`` to the quote.
    Refused before anything is written: 404 for an unknown order or item, 409
    ``order_not_open`` and ``item_not_open``, 422 ``filaments`` (a slot with no
    filament, or one archived) and ``price_book`` (a book that prices nothing)."""
    _path(order_id, item_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[ORDER_ITEM_QUOTE],
        subject=subject_of(item_id),
        request={
            "order_id": order_id,
            "item_id": item_id,
            "request": body.model_dump(mode="json"),
            # Ruling 12: who it is for comes from the author header, never the body.
            "by": current_actor().model_dump(mode="json"),
        },
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, Quote)


@router.post(
    "/{order_id}/items/{item_id}/quotes/{quote_id}/accept",
    response_model=OrderItem,
    summary="Accept a quote",
)
async def accept_quote(order_id: str, item_id: str, quote_id: str, orders: OrdersDep) -> OrderItem:
    _path(order_id, item_id, quote_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.accept_quote(order_id, item_id, quote_id, by))


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
    body: PrintRunRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Fulfilment | JSONResponse:
    """The ``order_item_print`` operation (#4719, §5.4): a print run of the item's output
    with the print dialog's spools and choices in the body, started as
    ``POST /print/outputs/{id}/run`` starts one, for every plate and the quantity still
    to print (the body's ``copies``, plates and ``request_id`` are the item's, not the
    caller's). Answers with the attempt it recorded, the item `queued`; the item then
    follows the run (`printing`, `printed` or `failed`), and a print whose end could not
    be read is settled by ``PATCH``ing the attempt. Refused before anything starts: 404
    for an unknown order or item; 409 ``order_not_accepted``, ``order_on_hold``,
    ``order_closed``/``order_cancelled``, ``item_custom``, ``item_not_open``,
    ``item_not_approved`` (one printing already), ``item_made`` (its attempts made its
    quantity: accept them), ``print_in_flight`` (a test print of the item is still
    open), ``print_request_conflict`` (the key's print is another item's, or the key's
    test print), and ``requote_needed`` (no accepted quote, its output gone,
    or a slice quote whose sliced file is gone or no longer the file it priced); and the
    print run's own refusals, as the print route answers them."""
    _path(order_id, item_id)
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
    body: PrintRunRequest,
    response: Response,
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
    its sliced file gone or no longer the file it priced, or the output gone); and the
    print run's own refusals, as the print route answers them."""
    _path(order_id, item_id, revision_id)
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
        },
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, Fulfilment)


# -- fulfilment attempts (§6.1 by hand) ------------------------------------------------------


@router.post(
    "/{order_id}/items/{item_id}/fulfilments",
    response_model=Fulfilment,
    status_code=status.HTTP_201_CREATED,
    summary="Record a fulfilment attempt",
)
async def record_fulfilment(
    order_id: str, item_id: str, body: FulfilmentInput, orders: OrdersDep
) -> Fulfilment:
    _path(order_id, item_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.record_fulfilment(order_id, item_id, body, by))


@router.patch(
    "/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}",
    response_model=Fulfilment,
    summary="Edit an attempt not yet accepted",
)
async def update_fulfilment(
    order_id: str, item_id: str, fulfilment_id: str, body: FulfilmentInput, orders: OrdersDep
) -> Fulfilment:
    _path(order_id, item_id, fulfilment_id)
    by = current_actor()
    return await _run(
        lambda: orders.lifecycle.update_fulfilment(order_id, item_id, fulfilment_id, body, by)
    )


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
    _path(order_id, item_id, fulfilment_id)
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
    _path(order_id, item_id, fulfilment_id)
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
)
async def accept_fulfilment(
    order_id: str, item_id: str, fulfilment_id: str, orders: OrdersDep
) -> Fulfilment:
    _path(order_id, item_id, fulfilment_id)
    by = current_actor()
    return await _run(
        lambda: orders.lifecycle.accept_fulfilment(order_id, item_id, fulfilment_id, by)
    )


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
    _path(order_id, item_id)
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
    _path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.add_shipment(order_id, body, by))


@router.patch(
    "/{order_id}/shipments/{shipment_id}", response_model=Shipment, summary="Edit a shipment"
)
async def update_shipment(
    order_id: str, shipment_id: str, body: ShipmentEdit, orders: OrdersDep
) -> Shipment:
    _path(order_id, shipment_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.update_shipment(order_id, shipment_id, body, by))
