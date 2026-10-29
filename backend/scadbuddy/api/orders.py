"""`/api/v1/orders` (#4249, spec 2026-10-18 §3.2-3.5, §4, §6.1 by hand, §6.3; plan
Rulings 1-3, 8-12): orders, their line items, manual quotes, fulfilment attempts,
shipments, payments and the audit line; the price book, the slice quote and the rough
estimate (#4250, §5).

Plain reads and writes, but for the slice quote, which is the ``order_item_quote``
operation (``orders/operations.py``) through ``run_operation``. Every write goes through
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
import zipfile
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import require_output
from scadbuddy.library.settings_store import SettingsPatch
from scadbuddy.operations.component import OperationsDep
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.lifecycle import (
    FulfilmentInput,
    LifecycleError,
    ManualQuoteInput,
    OrderEdit,
    ShipmentEdit,
    ShipmentInput,
)
from scadbuddy.orders.models import (
    Attachment,
    Charge,
    ChargeInput,
    Filament,
    FilamentInput,
    FilamentPatch,
    Fulfilment,
    Money,
    Order,
    OrderEvent,
    OrderFilter,
    OrderItem,
    OrderStatus,
    Payment,
    PriceBook,
    PricingPolicy,
    Quote,
    Shipment,
)
from scadbuddy.orders.operations import ORDER_ITEM_QUOTE
from scadbuddy.orders.pricing import estimate_grams, to_minor
from scadbuddy.orders.problems import PROBLEMS, refused
from scadbuddy.orders.quote import SliceQuoteRequest, subject_of
from scadbuddy.orders.reference import ReferenceError, TemplateDeletedError, resolve_reference
from scadbuddy.orders.store import UNSET, FilamentExistsError, UnsetType

router = APIRouter(prefix="/orders", tags=["orders"])

#: Ruling 9: cursor-paged, as the Activity page is.
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
LIST_PARAMS = frozenset({"status", "customer_id", "q", "from", "to", "cursor", "limit"})
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
    output_id: str | None = None
    quantity: int = Field(ge=1, le=MAX_QUANTITY)
    title: str | None = Field(default=None, max_length=200)
    notes: str = ""


class ItemPatch(BaseModel):
    """The reference, replaced whole, and the quantity while `draft` or `quoted` (each
    drops the accepted quote); title and notes in any open state."""

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


class OrderPage(BaseModel):
    items: list[Order]
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


@router.get("", response_model=OrderPage, summary="List orders")
async def list_orders(
    request: Request,
    orders: OrdersDep,
    status_: Annotated[OrderStatus | None, Query(alias="status")] = None,
    customer_id: str | None = None,
    q: str | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> OrderPage:
    """Newest first, a page at a time: pass ``next_cursor`` back as ``cursor``."""
    _no_nul(request)
    _unknown(request, LIST_PARAMS)
    filters = OrderFilter(status=status_, customer_id=customer_id, q=q, since=from_, until=to)
    try:
        rows, next_cursor = await asyncio.to_thread(orders.store.orders, filters, cursor, limit)
    except ValueError:
        raise ApiError(400, "cursor is not a next_cursor", type_=BAD_CURSOR) from None
    return OrderPage(items=rows, next_cursor=next_cursor)


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
async def get_price_book(orders: OrdersDep) -> PriceBook:
    """The currency the next order copies, the pricing policy, the rough estimate's
    factor and the filament records not archived, in one read (§5.1)."""
    return await asyncio.to_thread(orders.price_book.snapshot)


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
                fulfilments=store.fulfilments(item.id),
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


@router.post("/{order_id}/accept", response_model=Order, summary="Accept an order")
async def accept_order(order_id: str, orders: OrdersDep) -> Order:
    _path(order_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.accept_order(order_id, by))


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
    against the catalogue and the commit's schema (Ruling 6)."""
    _path(order_id)
    if await asyncio.to_thread(orders.store.order, order_id) is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
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
    by = current_actor()
    return await _run(
        lambda: orders.lifecycle.add_item(
            order_id,
            by,
            resolved,
            output_id=body.output_id,
            quantity=body.quantity,
            title=body.title,
            notes=body.notes,
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
        )
    )


@router.delete(
    "/{order_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a draft line item",
)
async def delete_item(order_id: str, item_id: str, orders: OrdersDep) -> Response:
    """Only while the item is `draft` (Ruling 10): else 409 ``item_not_draft`` names the
    cancel route, and the caller decides."""
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
        "`item_not_priced` (an order past `quoted` takes back a priced item only), and "
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
        "`order_on_hold`, `order_not_accepted`, `order_closed`/`order_cancelled`, and "
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
