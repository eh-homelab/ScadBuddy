"""A visitor submits its cart as an order (#5283, spec 2026-11-02 §5.5, plan Task 3).

`submit_draft` runs `Lifecycle.submit_order` in one transaction with its
``Idempotency-Key``'s ledger row (`idempotent_write`), so a submit the edge sends again
after a lost answer makes one order, takes one number and queues one mail. As the draft
writes do, it looks its key up before any other work (`stored`): a retry of a committed
submit answers the reply that submit stored, whatever changed since. The reply names the
order and where its link went, never the link's token: the ledger keeps replies in clear,
and only the mail carries the link.

The body names the cart the page shows (``draft_id``), so a page left open on a cart that
was submitted meanwhile never submits the visitor's newer one. A cart that is not the
visitor's, or no cart, is 404 ``draft_not_found``, checked under the order's lock.
``account_id`` stays None until the storefront's accounts give the CRUD side a session to
read it from; a body never names it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent_write, stored
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.lifecycle import LifecycleError, SubmitInput
from scadbuddy.orders.models import Order
from scadbuddy.orders.storefront.addresses import EmailInvalidError
from scadbuddy.orders.storefront.listings import problem
from scadbuddy.orders.storefront.visitors import VisitorStore, token_sha256, visitor_actor
from scadbuddy.orders.storefront.wire import SubmitBody, SubmittedOrder
from scadbuddy.payments.credentials import KekMissingError
from scadbuddy.storefront.wire import ITEM_ID

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

#: The ledger's name of the route (`idempotent_write`).
SUBMIT_ROUTE: Final = "POST /storefront/draft/submit"


def draft_not_found() -> ApiError:
    """404 ``draft_not_found``: no cart, and another visitor's cart alike."""
    return problem(status.HTTP_404_NOT_FOUND, "draft_not_found", "There is no such order.")


def _refusal(error: LifecycleError) -> ApiError:
    """What a visitor reads for the lifecycle's refusal of its submit."""
    if error.not_found:
        return draft_not_found()
    if error.unprocessable:
        return problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "shipping_option_id",
            "Choose how your order reaches you.",
        )
    texts = {
        "order_submitted": "This order is sent already.",
        "draft_empty": "Your order is empty. Add something to it first.",
        "shipping_address_required": "This way of shipping needs your address.",
    }
    if error.slug in texts:
        return problem(status.HTTP_409_CONFLICT, error.slug, texts[error.slug])
    return problem(
        status.HTTP_409_CONFLICT, "draft_changed", "Your order has changed. Load it again."
    )


async def submit_draft(
    state: AppState, visitor_id: str, key: str, body: SubmitBody
) -> SubmittedOrder:
    """The visitor's cart ``body.draft_id`` submitted (`Lifecycle.submit_order`). A retry
    of a committed submit (the same key) answers its reply and does nothing else.
    Refusals: 404 ``draft_not_found``, 409 ``order_submitted``, 409 ``draft_empty``, 409
    ``shipping_address_required``, 422 ``shipping_option_id``, 422 ``email_invalid``,
    503 ``submit_unavailable`` (no KEK to seal the link with)."""
    orders: OrdersComponent = state.components.get(ORDERS)
    asked = {"visitor": token_sha256(visitor_id), **body.model_dump(mode="json")}

    def lookup() -> Replay | None:
        with state.projection.pool.connection() as conn:
            return stored(conn, "storefront", key, SUBMIT_ROUTE, asked)

    reply = await asyncio.to_thread(lookup)
    if reply is not None:
        return SubmittedOrder.model_validate(reply.body)
    if ITEM_ID.fullmatch(body.draft_id) is None:
        raise draft_not_found()
    entry = SubmitInput(**body.model_dump(exclude={"draft_id"}), account_id=None)

    def write() -> Replay:
        visitor = VisitorStore(state.projection.pool).find(visitor_id)
        if visitor is None:
            raise draft_not_found()
        by = visitor_actor(visitor)
        changes: list[Order] = []

        def submit(conn: Any) -> Replay:
            order, _token = orders.lifecycle.submit_order(
                body.draft_id, entry, by, cart_of=visitor.id, conn=conn
            )
            changes.append(order)
            assert order.number is not None
            answer = SubmittedOrder(id=order.id, number=order.number, email=entry.email.strip())
            return Replay(200, answer.model_dump(mode="json"))

        with state.projection.pool.connection() as conn:
            done = idempotent_write(
                conn, "storefront", key, SUBMIT_ROUTE, asked, lambda: submit(conn)
            )
        for order in changes:
            orders.lifecycle.told(order)
        return done

    try:
        reply = await asyncio.to_thread(write)
    except LifecycleError as error:
        raise _refusal(error) from None
    except EmailInvalidError:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "email_invalid",
            "Give one email address, such as ada@example.com.",
        ) from None
    except KekMissingError:
        raise problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "submit_unavailable",
            "The shop cannot take orders now. Try again later.",
        ) from None
    return SubmittedOrder.model_validate(reply.body)
