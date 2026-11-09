"""`/api/v1/orders/{order_id}/bank-deposits` (#4961): the deposits of the Plaid-linked account
suggested for an order (``payments/deposits.py``). A suggestion records nothing; the shop
confirms it ("Record this deposit as payment") or dismisses it ("Not this order"), each a
person's own (``auth/routes.py`` ``PERSON_ENDPOINTS``). A confirmation is
``Lifecycle.add_payment`` with method ``bank_transfer`` and the transaction id as
``reference``, through ``Payments.add_payment``, so the requests it pays close their pages
after the commit. A refusal is the orders problem (``orders/problems.py`` ``refused``).

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio

from fastapi import APIRouter, Response, status

from scadbuddy.api.orders import check_path
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersComponent, OrdersDep
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import Payment
from scadbuddy.orders.problems import refused
from scadbuddy.payments.component import PaymentsDep
from scadbuddy.payments.deposits import (
    DepositSuggestion,
    dismiss_deposit,
    record_deposit,
    suggestions,
)

router = APIRouter(prefix="/orders", tags=["orders"])


def _suggestions(orders: OrdersComponent, order_id: str) -> list[DepositSuggestion]:
    if orders.store.order(order_id) is None:
        raise LifecycleError("order_not_found", f"no order {order_id}", not_found=True)
    return suggestions(orders.store, order_id)


@router.get(
    "/{order_id}/bank-deposits",
    response_model=list[DepositSuggestion],
    summary="The bank deposits suggested for an order",
    description=(
        "Deposits of the Plaid-linked account whose amount equals the order's balance or"
        " whose text names its number; none dismissed or recorded. A suggestion records"
        " nothing."
    ),
)
async def list_order_bank_deposits(order_id: str, orders: OrdersDep) -> list[DepositSuggestion]:
    check_path(order_id)
    try:
        return await asyncio.to_thread(_suggestions, orders, order_id)
    except LifecycleError as error:
        raise refused(error) from None


@router.post(
    "/{order_id}/bank-deposits/{transaction_id}/record",
    response_model=Payment,
    status_code=status.HTTP_201_CREATED,
    summary="Record a suggested deposit as the order's payment",
    description=(
        "Records the deposit's amount once, method `bank_transfer`, reference the Plaid"
        " transaction id. 404 `deposit_not_suggested`; 409 `deposit_recorded` when it is"
        " recorded on any order; 409 `deposit_currency` when its currency is not the"
        " order's; 409 `deposit_changed` when a sync changed its amount meanwhile (reload)."
    ),
)
async def record_order_bank_deposit(
    order_id: str, transaction_id: str, orders: OrdersDep, service: PaymentsDep
) -> Payment:
    check_path(order_id)
    check_path(transaction_id)
    by = current_actor()
    try:
        return await service.service.add_payment(
            order_id,
            lambda: record_deposit(
                orders.store,
                order_id,
                transaction_id,
                lambda **fields: orders.lifecycle.add_payment(order_id, by, **fields),
            ),
        )
    except LifecycleError as error:
        raise refused(error) from None


@router.post(
    "/{order_id}/bank-deposits/{transaction_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Dismiss a suggested deposit: not this order",
)
async def dismiss_order_bank_deposit(
    order_id: str, transaction_id: str, orders: OrdersDep
) -> Response:
    check_path(order_id)
    check_path(transaction_id)
    try:
        await asyncio.to_thread(dismiss_deposit, orders.store, order_id, transaction_id)
    except LifecycleError as error:
        raise refused(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
