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

The account comes only from the request's session (`api/crud_storefront.py`
`store_account`); a body never names it. Signed in, the order takes the account's
customer and is attached to the account; a cart whose visitor joined another account is
404 ``draft_not_found`` too. A session that names no live account (expired, ended or
unknown) reads as none, so that submit is anonymous: a new customer and no attach, the
safe side, and the page is told nothing more.

A key is bound to who sent it: the ledger's request names the account, so a key sent
again under another session, or with none, is 422 ``idempotency-key-reused``, never the
stored reply. A lost-reply submit retried after the visitor signed in or out (or after
its session expired) is refused that way, though its order exists; the page loads the
cart again and finds it submitted.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent_write, stored
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.lifecycle import LifecycleError, SubmitInput
from scadbuddy.orders.models import Order, OrderItem
from scadbuddy.orders.storefront.accounts import Account
from scadbuddy.orders.storefront.addresses import EmailInvalidError, check_address
from scadbuddy.orders.storefront.line_prices import reprice
from scadbuddy.orders.storefront.listings import problem
from scadbuddy.orders.storefront.staged_files import poke, uploads_of
from scadbuddy.orders.storefront.visitors import VisitorStore, token_sha256, visitor_actor
from scadbuddy.orders.storefront.wire import SubmitBody, SubmittedOrder
from scadbuddy.payments.component import PAYMENTS
from scadbuddy.payments.credentials import KekMissingError
from scadbuddy.storefront.wire import ITEM_ID

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

#: The ledger's name of the route (`idempotent_write`).
SUBMIT_ROUTE: Final = "POST /storefront/draft/submit"
#: A cart that holds a refused file (#6133).
MODEL_FILE_REFUSED: Final = "model_file_refused"
#: A cart whose firm prices changed since the page read it (#6153).
PRICES_CHANGED: Final = "prices_changed"


def _cart_items(state: AppState, visitor_id: str, body: SubmitBody) -> list[OrderItem]:
    """The items of the visitor's cart ``body.draft_id``, or none when it is not the
    visitor's unsubmitted cart (`Lifecycle.submit_order` refuses it then). Blocking."""
    orders: OrdersComponent = state.components.get(ORDERS)
    visitor = VisitorStore(state.projection.pool).find(visitor_id)
    order = orders.store.order(body.draft_id)
    if (
        visitor is None
        or order is None
        or order.storefront_visitor_id != visitor.id
        or not order.unsubmitted
    ):
        return []
    return orders.store.items(order.id)


def draft_not_found() -> ApiError:
    """404 ``draft_not_found``: no cart, and another visitor's cart alike."""
    return problem(status.HTTP_404_NOT_FOUND, "draft_not_found", "There is no such order.")


def _refusal(error: LifecycleError) -> ApiError:
    """What a visitor reads for the lifecycle's refusal of its submit."""
    if error.not_found:
        return draft_not_found()
    if error.unprocessable:
        detail = (
            "Choose how your order reaches you."
            if error.slug == "shipping_option_id"
            else "Your order could not be sent as it is. Load it again."
        )
        return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, error.slug, detail)
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


def _no_refused_file(conn: Any, draft_id: str, visitor_id: str) -> None:
    """409 ``model_file_refused`` while the visitor's cart holds a file its check refused,
    or one whose staged copy expired (#6133, spec 2026-11-13 §4.3), naming those files'
    sources. The customer removes it first. A file still being checked does not block.
    The order's lock is taken first, as the check's verdict takes it, so a verdict that
    lands meanwhile is seen. Another visitor's cart, or no cart, is left to
    `Lifecycle.submit_order`, which refuses it."""
    order = conn.execute(
        "SELECT id FROM orders WHERE id = %s AND storefront_visitor_id = %s"
        " AND submitted_at IS NULL FOR UPDATE",
        (draft_id, visitor_id),
    ).fetchone()
    if order is None:
        return
    refused = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM order_item_sources WHERE order_id = %s AND kind = 'file'"
            " AND check_status = 'refused' ORDER BY item_id, position",
            (draft_id,),
        ).fetchall()
    ]
    if refused:
        raise problem(
            status.HTTP_409_CONFLICT,
            MODEL_FILE_REFUSED,
            "A model file cannot be printed. Remove it, then send your order.",
            sources=refused,
        )


async def submit_draft(
    state: AppState, visitor_id: str, key: str, body: SubmitBody, account: Account | None
) -> SubmittedOrder:
    """The visitor's cart ``body.draft_id`` submitted (`Lifecycle.submit_order`). A retry
    of a committed submit (the same key) answers its reply and does nothing else.
    Each listing line is priced again first (`line_prices.reprice`, #6153): a line whose
    firm price changed, came or went is 409 ``prices_changed`` (``items``: each line's
    ``id`` and its ``price`` now, ``firm`` or ``by_hand``), and the page shows the cart
    again before the customer sends it; a line with no price then goes to the shop's
    check. When every line is firm, the
    order is accepted and its balance request opens (``pay_now``).
    Refusals: 404 ``draft_not_found``, 409 ``order_submitted``, 409 ``draft_empty``, 409
    ``shipping_address_required``, 409 ``model_file_refused`` (#6133), 409
    ``prices_changed`` (#6153), 422
    ``shipping_option_id``, 422 ``email_invalid``,
    503 ``submit_unavailable`` (no KEK to seal the link with)."""
    orders: OrdersComponent = state.components.get(ORDERS)
    asked = {
        "visitor": token_sha256(visitor_id),
        "account": None if account is None else account.id,
        **body.model_dump(mode="json"),
    }

    def lookup() -> Replay | None:
        with state.projection.pool.connection() as conn:
            return stored(conn, "storefront", key, SUBMIT_ROUTE, asked)

    reply = await asyncio.to_thread(lookup)
    if reply is not None:
        return SubmittedOrder.model_validate(reply.body)
    if ITEM_ID.fullmatch(body.draft_id) is None:
        raise draft_not_found()
    # Each line priced again by its variant's key (#6153), never by the browser.
    repriced = await reprice(state, await asyncio.to_thread(_cart_items, state, visitor_id, body))
    if repriced.told:
        raise problem(
            status.HTTP_409_CONFLICT,
            PRICES_CHANGED,
            "Some prices changed. Check your order, then send it again.",
            items=[line.model_dump() for line in repriced.told],
        )
    entry = SubmitInput(
        **body.model_dump(exclude={"draft_id"}),
        account_id=None if account is None else account.id,
    )

    def write() -> Replay:
        visitor = VisitorStore(state.projection.pool).find(visitor_id)
        if visitor is None:
            raise draft_not_found()
        by = visitor_actor(visitor)
        changes: list[tuple[Order, list[OrderItem]]] = []

        def submit(conn: Any) -> Replay:
            _no_refused_file(conn, body.draft_id, visitor.id)
            order, _token = orders.lifecycle.submit_order(
                body.draft_id, entry, by, cart_of=visitor.id, conn=conn, firm=repriced.firm
            )
            changes.append((order, orders.store.items(order.id, conn=conn)))
            assert order.number is not None
            # The address the link was mailed to: `submit_order` checked it the same way.
            sent_to = check_address(entry.email)
            answer = SubmittedOrder(
                id=order.id,
                number=order.number,
                email=sent_to,
                pay_now=order.status == "accepted",
            )
            return Replay(200, answer.model_dump(mode="json"))

        with state.projection.pool.connection() as conn:
            done = idempotent_write(
                conn, "storefront", key, SUBMIT_ROUTE, asked, lambda: submit(conn)
            )
        for order, items in changes:
            orders.lifecycle.told(order, items)
        return done

    try:
        # A checkout of firm lines accepts the order, which opens its balance request
        # and cancels the deposit's: their pages are linked and closed after the commit.
        reply = await state.components.get(PAYMENTS).service.run_closing_linked(
            body.draft_id, write
        )
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
    # Each staged file of the order now waits for its import, not a deadline (#6133).
    await poke(
        state, await asyncio.to_thread(uploads_of, state, order_id=body.draft_id), "submitted"
    )
    return SubmittedOrder.model_validate(reply.body)
