"""`/api/v1/orders/{order_id}/purchases` (#6133, spec 2026-11-13 §10, §11): the things a
storefront customer asked the shop to buy for their project. Each is a purchase link of a
request item (`order_item_sources`, purpose `purchase`). The shop records what it paid
(one cost entry and one `purchase` charge, written together), changes it, or marks the
link not needed. A person or the agent may call each route. A refusal is the orders
problem (``orders/problems.py`` ``refused``). A record, a change or a decline recomputes
the handling fee and the auto tax, so it links any balance request it opens after the
commit, as the charges routes do (``api/orders.py`` ``_run_linked``).

A record repeats safely without an ``Idempotency-Key``: its cost entry's source
``purchase:<source id>`` and the link's state make a second record return the first, as
``POST …/cost-entries`` does with a ``source``.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio

from fastapi import APIRouter, Response, status

from scadbuddy.api.orders import _run_linked, check_path
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersComponent, OrdersDep
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import (
    PurchaseDeclineInput,
    PurchaseRecordInput,
    PurchaseSource,
    PurchaseUpdateInput,
)
from scadbuddy.orders.problems import refused
from scadbuddy.payments.component import PaymentsDep

router = APIRouter(prefix="/orders", tags=["orders"])


def _purchases(orders: OrdersComponent, order_id: str) -> list[PurchaseSource]:
    if orders.store.order(order_id) is None:
        raise LifecycleError("order_not_found", f"no order {order_id}", not_found=True)
    return orders.store.purchase_sources(order_id)


@router.get(
    "/{order_id}/purchases",
    response_model=list[PurchaseSource],
    summary="An order's purchase links",
    description=(
        "The things the customer asked the shop to buy for their project, by item: each "
        "link as text (nothing fetches it), the customer's note, its status (`pending`, "
        "`recorded`, `not_needed`), a recorded one's amount and cost kind, and the shop's "
        "note to the customer."
    ),
)
async def list_purchases(order_id: str, orders: OrdersDep) -> list[PurchaseSource]:
    check_path(order_id)
    try:
        return await asyncio.to_thread(_purchases, orders, order_id)
    except LifecycleError as error:
        raise refused(error) from None


@router.post(
    "/{order_id}/purchases/{source_id}/record",
    response_model=PurchaseSource,
    status_code=status.HTTP_201_CREATED,
    summary="Record what the shop paid for a purchase link",
    description=(
        "One write: a cost entry on the link's item (`material` for parts, `other` for a "
        "paid model file; source `purchase:<source id>`), a `purchase` charge of the same "
        'amount that the customer pays, labelled "Bought for your project: <the '
        "customer's note, or the link's host>\", and the link `recorded`. The handling fee "
        "(Settings → Orders) and the auto tax are recomputed. A second record of a "
        "recorded link returns it as it is and writes nothing (200). 409 "
        "`purchase_locked` once the order is past `quoted`, `purchase_not_pending` for a "
        "link marked not needed, `order_not_submitted` on a storefront cart; 404 "
        "`source_not_found`."
    ),
    responses={status.HTTP_200_OK: {"model": PurchaseSource, "description": "Already recorded"}},
)
async def record_purchase(
    order_id: str,
    source_id: str,
    body: PurchaseRecordInput,
    orders: OrdersDep,
    service: PaymentsDep,
    response: Response,
) -> PurchaseSource:
    check_path(order_id, source_id)
    by = current_actor()
    source, written = await _run_linked(
        lambda: orders.lifecycle.record_purchase(order_id, source_id, body, by),
        order_id,
        service,
    )
    if not written:
        response.status_code = status.HTTP_200_OK
    return source


@router.patch(
    "/{order_id}/purchases/{source_id}",
    response_model=PurchaseSource,
    summary="Change a recorded purchase",
    description=(
        "The cost entry and the `purchase` charge change together: each field given "
        "replaces its value, and the amount is both. The handling fee and the auto tax are "
        "recomputed. 409 `purchase_not_recorded` for a link not recorded, "
        "`purchase_locked` once the order is past `quoted` (change what it charges with "
        "`PUT …/charges/after-acceptance`)."
    ),
)
async def update_purchase(
    order_id: str,
    source_id: str,
    body: PurchaseUpdateInput,
    orders: OrdersDep,
    service: PaymentsDep,
) -> PurchaseSource:
    check_path(order_id, source_id)
    by = current_actor()
    return await _run_linked(
        lambda: orders.lifecycle.update_purchase(order_id, source_id, body, by),
        order_id,
        service,
    )


@router.post(
    "/{order_id}/purchases/{source_id}/decline",
    response_model=PurchaseSource,
    summary="Mark a purchase link not needed",
    description=(
        "A recorded link loses its cost entry and its charge; the link is `not_needed` "
        "with `shop_note`, which the customer reads. The handling fee and the auto tax are "
        "recomputed. A link already not needed is returned as it is. 409 "
        "`purchase_locked` once the order is past `quoted`."
    ),
)
async def decline_purchase(
    order_id: str,
    source_id: str,
    body: PurchaseDeclineInput,
    orders: OrdersDep,
    service: PaymentsDep,
) -> PurchaseSource:
    check_path(order_id, source_id)
    by = current_actor()
    return await _run_linked(
        lambda: orders.lifecycle.decline_purchase(order_id, source_id, body.shop_note, by),
        order_id,
        service,
    )
