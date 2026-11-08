"""`/api/v1/orders/{order_id}/payment-requests` (#4957): ask the customer for money,
through a provider's hosted page or by hand. A request is opened through the lifecycle
and linked after the commit (``payments/service.py``); a failed link stays as
``link_error`` and ``…/link`` retries it. A cancel closes the provider's page after the
commit. A refusal is the orders problem
(``orders/problems.py`` ``refused``); a provider that is not offered or not connected is
422 on ``provider``.

The deposit, the two waivers and the shipping option (#5281, storefront spec §8-§9): a
deposit set or applied by the policy cancels the requests it replaces through
``Payments.run_closing``, which closes their pages after the commit, then links the new
request (``link_pending``).

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.orders import check_path
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersComponent, OrdersDep
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import Money, Order
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


class CancelRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Why, for the audit line; cancelling the balance request lets the order ship unpaid.
    reason: str | None = Field(default=None, min_length=1, max_length=500)


@router.post(
    "/{order_id}/payment-requests/{request_id}/cancel",
    response_model=PaymentRequestView,
    summary="Cancel an open or failed payment request",
    description=(
        "`reason` (optional) goes on the audit line. Cancelling the `balance` request lifts"
        " `balance_unpaid`, so the order may ship unpaid; give the reason then."
    ),
)
async def cancel_payment_request(
    order_id: str,
    request_id: str,
    service: PaymentsDep,
    body: CancelRequestBody | None = None,
) -> PaymentRequestView:
    check_path(order_id, request_id)
    by = current_actor()
    reason = None if body is None else body.reason
    try:
        return await service.service.cancel_request(order_id, request_id, by, reason)
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


# -- the deposit, the balance and the shipping option (#5281) ---------------------------------


class DepositBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The deposit, or null for none.
    amount_minor: Money | None = Field(ge=1)


class WaiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class ShippingOptionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: An id from Settings → Orders' shipping options, or null for none.
    option_id: str | None


@router.put(
    "/{order_id}/deposit",
    response_model=Order,
    summary="Set the order's deposit",
    description=(
        "While draft or quoted. It cancels the open and failed deposit requests, closes"
        " their payment pages, clears a waiver, and asks for what of the deposit is not"
        " paid yet on the default provider. 409 `payment_in_flight` while a deposit"
        " request is pending; `order_not_submitted` on a storefront cart."
    ),
)
async def set_deposit(
    order_id: str, body: DepositBody, orders: OrdersDep, service: PaymentsDep
) -> Order:
    check_path(order_id)
    by = current_actor()
    try:
        order = await service.service.run_closing(
            order_id, lambda: orders.lifecycle.set_deposit(order_id, by, body.amount_minor)
        )
    except LifecycleError as error:
        raise refused(error) from None
    await service.service.link_pending(order_id)
    return order


@router.post(
    "/{order_id}/deposit/policy",
    response_model=Order,
    summary="Set the deposit the shop's policy asks",
    description=(
        "As `PUT …/deposit`, with the amount the deposit policy asks of the order now. A"
        " fixed amount or a `total_above` rule is in the shop's currency: on an order in"
        " another currency it is skipped and named on the `deposit.set` audit row"
        " (`skipped`, its index in the policy), and when only such rules would apply the"
        " answer is 409 `deposit_policy_currency` and nothing is written."
    ),
)
async def apply_deposit_policy(order_id: str, orders: OrdersDep, service: PaymentsDep) -> Order:
    check_path(order_id)
    by = current_actor()
    try:
        order = await service.service.run_closing(
            order_id, lambda: orders.lifecycle.apply_deposit_policy(order_id, by)
        )
    except LifecycleError as error:
        raise refused(error) from None
    await service.service.link_pending(order_id)
    return order


@router.post(
    "/{order_id}/deposit/waive",
    response_model=Order,
    summary="Proceed without the deposit",
    description="The work goes on unpaid, for `reason`. The deposit request stays open.",
)
async def waive_deposit(order_id: str, body: WaiveBody, orders: OrdersDep) -> Order:
    check_path(order_id)
    by = current_actor()
    try:
        return await asyncio.to_thread(orders.lifecycle.waive_deposit, order_id, by, body.reason)
    except LifecycleError as error:
        raise refused(error) from None


@router.post(
    "/{order_id}/balance/waive",
    response_model=Order,
    summary="Ship without full payment",
    description=(
        "Once accepted, the order ships with its balance unpaid, for `reason`. The balance"
        " request stays open."
    ),
)
async def waive_balance(order_id: str, body: WaiveBody, orders: OrdersDep) -> Order:
    check_path(order_id)
    by = current_actor()
    try:
        return await asyncio.to_thread(orders.lifecycle.waive_balance, order_id, by, body.reason)
    except LifecycleError as error:
        raise refused(error) from None


@router.put(
    "/{order_id}/shipping-option",
    response_model=Order,
    summary="Choose the order's shipping option",
    description=(
        "Writes the order's one `shipping` charge from the option (null removes it), while"
        " draft or quoted. An option that ships needs the order's shipping address"
        " (`shipping_address_required`); a pickup does not."
    ),
)
async def set_shipping_option(
    order_id: str, body: ShippingOptionBody, orders: OrdersDep, service: PaymentsDep
) -> Order:
    check_path(order_id)
    by = current_actor()
    try:
        return await service.service.run_linking(
            order_id, lambda: orders.lifecycle.set_shipping_option(order_id, by, body.option_id)
        )
    except LifecycleError as error:
        raise refused(error) from None
