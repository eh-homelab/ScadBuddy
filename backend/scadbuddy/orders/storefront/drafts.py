"""A visitor's draft order (#5282, spec 2026-11-02 §5.4, plan Task 8).

The draft is the visitor's cart: an order with ``source = 'storefront'``, no number and
no customer until it is submitted (`Lifecycle.ensure_draft`). `add_listing_item` checks
the visitor's values against the listing, never trusting the edge
(`renders.py` `checked_choice`), and adds a model item with revision 1 from the listing's
snapshot, titled as the listing. The add runs in one transaction with its
``Idempotency-Key``'s ledger row (`idempotent_write`), so a request the edge sends again
after a lost answer adds one item. `remove_item` does the same for a delete.

Every write names the visitor's row (`Lifecycle` ``cart_of``), which the lifecycle checks
under the order's lock: another visitor's draft or item is 404 ``item_not_found``, never
403. A read (`get_draft`) makes no visitor row: a visitor that never wrote has no draft.
The visitor reads its items' files as presigned GETs (`PUBLIC_MEDIA_TTL`), never a path.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent_write
from scadbuddy.orders.attachments import blob_key
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.lifecycle import LifecycleError, NewModelItem
from scadbuddy.orders.models import Attachment, Order, OrderItem
from scadbuddy.orders.store import item_subject
from scadbuddy.orders.storefront.listings import PUBLIC_MEDIA_TTL, problem, public_content
from scadbuddy.orders.storefront.renders import checked_choice
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from scadbuddy.orders.storefront.wire import (
    AddListingItem,
    DraftAttachment,
    DraftItem,
    DraftView,
)
from scadbuddy.store.content import ContentStore
from scadbuddy.store.s3 import S3ContentBackend
from scadbuddy.storefront.wire import ITEM_ID

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

#: The ledger's names of the routes (`idempotent_write`).
ADD_ROUTE: Final = "POST /storefront/draft/items"
REMOVE_ROUTE: Final = "DELETE /storefront/draft/items/{item_id}"


def _orders(state: AppState) -> OrdersComponent:
    orders: OrdersComponent = state.components.get(ORDERS)
    return orders


def item_not_found() -> ApiError:
    """404 ``item_not_found``: an unknown item and another visitor's item alike."""
    return problem(status.HTTP_404_NOT_FOUND, "item_not_found", "There is no such item.")


def _draft_changed() -> ApiError:
    """409 ``draft_changed``: the draft moved under the write (submitted, or swept)."""
    return problem(
        status.HTTP_409_CONFLICT, "draft_changed", "Your order has changed. Load it again."
    )


async def _url(content: ContentStore, attachment: Attachment) -> str | None:
    """A presigned GET of the attachment's blob, or None when the index lost it."""
    stat = await asyncio.to_thread(content.index.get, blob_key(attachment.sha256))
    if stat is None or stat.ref.backend != content.name:
        return None
    backend = content.backend
    assert isinstance(backend, S3ContentBackend)
    return await backend.presign_get(stat.ref.backend_id, PUBLIC_MEDIA_TTL)


def _inputs(item: OrderItem) -> dict[str, Any] | None:
    """A model item's parameter values; None on any other kind."""
    if item.kind != "model" or item.reference is None:
        return None
    params = item.reference.inputs.get("params")
    return dict(params) if isinstance(params, dict) else {}


async def _view(state: AppState, order: Order) -> DraftView:
    """The visitor's view of ``order``."""
    orders = _orders(state)

    def read() -> list[tuple[OrderItem, list[Attachment]]]:
        return [
            (item, orders.store.subject_attachments(item_subject(item.id)))
            for item in orders.store.items(order.id)
        ]

    rows = await asyncio.to_thread(read)
    content = public_content(state) if any(files for _, files in rows) else None
    items: list[DraftItem] = []
    for item, files in rows:
        attachments: list[DraftAttachment] = []
        for attachment in files:
            assert content is not None
            url = await _url(content, attachment)
            if url is not None:
                attachments.append(
                    DraftAttachment(filename=attachment.filename, kind=attachment.kind, url=url)
                )
        items.append(
            DraftItem(
                id=item.id,
                kind=item.kind,
                title=item.title,
                quantity=item.quantity,
                inputs=_inputs(item),
                notes=item.notes if item.kind == "request" else None,
                attachments=attachments,
            )
        )
    return DraftView(
        id=order.id,
        currency=order.currency,
        minor_exponent=order.minor_exponent,
        items=items,
    )


async def get_draft(state: AppState, visitor_id: str) -> DraftView | None:
    """The visitor's draft, or None when it has none. A visitor that never wrote gets
    None, and no row is made for it."""
    orders = _orders(state)

    def find() -> Order | None:
        visitor = VisitorStore(state.projection.pool).find(visitor_id)
        return None if visitor is None else orders.store.draft_of(visitor.id)

    order = await asyncio.to_thread(find)
    return None if order is None else await _view(state, order)


async def add_listing_item(
    state: AppState, visitor_id: str, key: str, body: AddListingItem
) -> DraftView:
    """The listing at ``body``'s values added to the visitor's draft, made on its first
    add: a model item with revision 1, titled as the listing. Refusals: 404
    ``listing_not_found``, 409 ``listing_changed``, 422 ``input_outside_listing`` (with
    ``name`` and ``reason``), 409 ``draft_changed``."""
    orders = _orders(state)
    listing, resolved = await checked_choice(orders, body)
    visitor = await asyncio.to_thread(VisitorStore(state.projection.pool).ensure, visitor_id)
    by = visitor_actor(visitor)
    currency = await asyncio.to_thread(lambda: orders.price_book.policy()[0])
    asked = {"visitor": visitor.id, **body.model_dump(mode="json")}
    entry = NewModelItem(resolved=resolved, quantity=body.quantity, title=listing.title)
    added: list[OrderItem] = []

    def write() -> None:
        # Outside the ledger's transaction: two first adds at once make one draft.
        draft = orders.lifecycle.ensure_draft(visitor.id, by, currency=currency)

        def add(conn: Any) -> Replay:
            added.extend(
                orders.lifecycle.add_items(draft.id, by, [entry], cart_of=visitor.id, conn=conn)
            )
            return Replay(200, {"order_id": draft.id, "item_id": added[0].id})

        with state.projection.pool.connection() as conn:
            idempotent_write(conn, "storefront", key, ADD_ROUTE, asked, lambda: add(conn))
        if added:
            orders.lifecycle.told(draft.id, added)

    try:
        await asyncio.to_thread(write)
    except LifecycleError:
        raise _draft_changed() from None
    order = await asyncio.to_thread(orders.store.draft_of, visitor.id)
    if order is None:
        raise _draft_changed()
    return await _view(state, order)


async def remove_item(state: AppState, visitor_id: str, key: str, item_id: str) -> DraftView:
    """The item taken off the visitor's draft. 404 ``item_not_found`` unless the item is
    on the draft that visitor owns, checked under the order's lock; 409 ``draft_changed``
    for an item the lifecycle keeps."""
    if ITEM_ID.fullmatch(item_id) is None:
        raise item_not_found()
    orders = _orders(state)

    def write() -> Order | None:
        visitor = VisitorStore(state.projection.pool).find(visitor_id)
        draft = None if visitor is None else orders.store.draft_of(visitor.id)
        if visitor is None or draft is None:
            raise item_not_found()
        by = visitor_actor(visitor)
        asked = {"visitor": visitor.id, "item_id": item_id}
        removed = False

        def remove(conn: Any) -> Replay:
            nonlocal removed
            orders.lifecycle.delete_item(draft.id, item_id, by, cart_of=visitor.id, conn=conn)
            removed = True
            return Replay(200, {"order_id": draft.id, "item_id": item_id})

        with state.projection.pool.connection() as conn:
            idempotent_write(conn, "storefront", key, REMOVE_ROUTE, asked, lambda: remove(conn))
        if removed:
            orders.lifecycle.told(draft.id)
        return orders.store.draft_of(visitor.id)

    try:
        order = await asyncio.to_thread(write)
    except LifecycleError as error:
        if error.not_found:
            raise item_not_found() from None
        raise _draft_changed() from None
    if order is None:
        raise _draft_changed()
    return await _view(state, order)
