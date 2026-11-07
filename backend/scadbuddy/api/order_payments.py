"""`/api/v1/orders/{order_id}/payment-requests` (#4957): ask the customer for money,
through a provider's hosted page or by hand. A request is opened through the lifecycle
and linked after the commit (``payments/service.py``); a failed link stays as
``link_error`` and ``…/link`` retries it. A cancel closes the provider's page after the
commit. A refusal is the orders problem
(``orders/problems.py`` ``refused``); a provider that is not offered or not connected is
422 on ``provider``.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio

from fastapi import APIRouter, status

from scadbuddy.api.orders import check_path
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersComponent, OrdersDep
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.problems import refused
from scadbuddy.payments import store as payments
from scadbuddy.payments.component import PaymentsDep
from scadbuddy.payments.models import PaymentRequestView
from scadbuddy.payments.service import PaymentRequestCreate, ProviderNotFoundError

router = APIRouter(prefix="/orders", tags=["orders"])


def _order_requests(orders: OrdersComponent, order_id: str) -> list[PaymentRequestView]:
    with orders.store.transaction() as conn:
        if orders.store.order(order_id, conn=conn) is None:
            raise LifecycleError("order_not_found", f"no order {order_id}", not_found=True)
        return payments.requests_for_order(conn, order_id)


def _request_of(orders: OrdersComponent, order_id: str, request_id: str) -> None:
    """404 unless ``request_id`` is a request of ``order_id``."""
    if not any(r.id == request_id for r in _order_requests(orders, order_id)):
        raise LifecycleError(
            "payment_request_not_found",
            f"no payment request {request_id} of order {order_id}",
            not_found=True,
        )


@router.get(
    "/{order_id}/payment-requests",
    response_model=list[PaymentRequestView],
    summary="An order's payment requests, oldest first",
)
async def list_payment_requests(order_id: str, orders: OrdersDep) -> list[PaymentRequestView]:
    check_path(order_id)
    try:
        return await asyncio.to_thread(_order_requests, orders, order_id)
    except LifecycleError as error:
        raise refused(error) from None


@router.post(
    "/{order_id}/payment-requests",
    response_model=PaymentRequestView,
    status_code=status.HTTP_201_CREATED,
    summary="Request a payment",
    description=(
        "Opens a request for `amount_minor` (the balance when left out) and, with a"
        " provider (the default one when `provider` is left out; null collects by hand),"
        " asks it for a hosted payment page. A provider that fails leaves the request open"
        " with `link_error`; `…/link` tries again."
    ),
)
async def request_payment(
    order_id: str, body: PaymentRequestCreate, service: PaymentsDep
) -> PaymentRequestView:
    check_path(order_id)
    by = current_actor()
    try:
        return await service.service.request_payment(order_id, by, body)
    except ProviderNotFoundError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
            errors=[{"loc": ["body", "provider"], "msg": str(error)}],
        ) from None
    except LifecycleError as error:
        raise refused(error) from None


@router.post(
    "/{order_id}/payment-requests/{request_id}/cancel",
    response_model=PaymentRequestView,
    summary="Cancel an open payment request",
)
async def cancel_payment_request(
    order_id: str, request_id: str, service: PaymentsDep
) -> PaymentRequestView:
    check_path(order_id, request_id)
    by = current_actor()
    try:
        return await service.service.cancel_request(order_id, request_id, by)
    except LifecycleError as error:
        raise refused(error) from None


@router.post(
    "/{order_id}/payment-requests/{request_id}/link",
    response_model=PaymentRequestView,
    summary="Ask the provider for the request's payment page again",
    description=(
        "For an open request whose link failed. A request with no provider, one already"
        " linked and one no longer open are returned as they are."
    ),
)
async def link_payment_request(
    order_id: str, request_id: str, orders: OrdersDep, service: PaymentsDep
) -> PaymentRequestView:
    check_path(order_id, request_id)
    try:
        await asyncio.to_thread(_request_of, orders, order_id, request_id)
        return await service.service.link(request_id)
    except LifecycleError as error:
        raise refused(error) from None
