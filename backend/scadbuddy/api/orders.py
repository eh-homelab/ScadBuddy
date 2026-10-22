"""`/api/v1/orders` (#4249, spec 2026-10-18 §3.2-3.5, §4, §6.1 by hand, §6.3; plan
Rulings 1-3, 8-12): orders, their line items, manual quotes, fulfilment attempts,
shipments, payments and the audit line.

Plain reads and writes: nothing here does work an operation kind would run (the slice
quote is phase 2). Every write goes through the lifecycle (``orders/lifecycle.py``),
which locks the order, derives its status and publishes ``order.changed`` /
``order_item.changed`` after commit; a refusal arrives as a `LifecycleError` and leaves
as a 409 problem typed ``https://scadbuddy.dev/problems/orders/<slug>`` with the ids in
the way (``items``, ``shipments``), or a 404 for a row that is not there. A reference
that cannot be made is 422 naming the field (``orders/reference.py``). ``created_by`` and
every audit row's ``by`` come from the author header, never a body (Ruling 12).

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response, status
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
from scadbuddy.api.runtime import apply_runtime
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsPatch
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.lifecycle import (
    FulfilmentInput,
    LifecycleError,
    ManualQuoteInput,
    OrderEdit,
    ShipmentEdit,
    ShipmentInput,
)
from scadbuddy.orders.models import (
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
from scadbuddy.orders.reference import ReferenceError, TemplateDeletedError, resolve_reference
from scadbuddy.orders.store import UNSET, FilamentExistsError, UnsetType

router = APIRouter(prefix="/orders", tags=["orders"])

#: The lifecycle's refusals, typed by their slug.
PROBLEMS = "https://scadbuddy.dev/problems/orders/"
#: Ruling 9: cursor-paged, as the Activity page is.
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
LIST_PARAMS = frozenset({"status", "customer_id", "q", "from", "to", "cursor", "limit"})


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
    quantity: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=200)
    notes: str = ""


class ItemPatch(BaseModel):
    """The reference, replaced whole, and the quantity while `draft` or `quoted` (each
    drops the accepted quote); title and notes in any open state."""

    model_config = ConfigDict(extra="forbid")

    reference: ReferenceBody | None = None
    output_id: str | None = None
    quantity: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=200)
    notes: str | None = None


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
    """§3.4: the accepted quotes' totals, the charges on top, what was paid."""

    subtotal_minor: Money
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


class OrderPage(BaseModel):
    items: list[Order]
    next_cursor: str | None


# -- errors ------------------------------------------------------------------------------------


def _refused(error: LifecycleError) -> ApiError:
    extensions: dict[str, Any] = {}
    if error.items:
        extensions["items"] = error.items
    if error.shipments:
        extensions["shipments"] = error.shipments
    return ApiError(
        status.HTTP_404_NOT_FOUND if error.not_found else status.HTTP_409_CONFLICT,
        str(error),
        type_=PROBLEMS + error.slug,
        **extensions,
    )


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
        raise _refused(error) from None


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
        problem = error.errors()[0]
        name = str(problem["loc"][0]).removeprefix("pricing_")
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            problem["msg"],
            errors=[{"loc": ["body", name], "msg": problem["msg"]}],
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


def _totals(items: list[ItemDetail], charges: list[Charge], payments: list[Payment]) -> OrderTotals:
    subtotal = sum(
        quote.total_minor
        for item in items
        if item.status != "cancelled"
        for quote in item.quotes
        if quote.accepted
    )
    charged = sum(charge.amount_minor for charge in charges)
    paid = sum(payment.amount_minor for payment in payments)
    return OrderTotals(
        subtotal_minor=subtotal,
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
    "/{order_id}/items/{item_id}/cancel", response_model=OrderItem, summary="Cancel a line item"
)
async def cancel_item(order_id: str, item_id: str, orders: OrdersDep) -> OrderItem:
    _path(order_id, item_id)
    by = current_actor()
    return await _run(lambda: orders.lifecycle.cancel_item(order_id, item_id, by))


# -- quotes (§3.4, manual only in phase 1) --------------------------------------------------


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
