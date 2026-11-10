"""The customer's order page (#5283, spec 2026-11-02 §5.2, §5.6, §6, §7, plan Task 5).

A customer reads their order and answers it at ``/o#<token>``: the page sends the order
link's token in each request's body, never in a path, and the edge forwards it unread.
`resolve` is the one way to an order: a `Reader` (today a link's token, `ByLink`) gives an
`Access` (the order and the customer's session), or None. Any token that reads nothing
(unknown, revoked, or not a token at all) costs the same one indexed lookup
(`links.link_by_token`) and gets the same empty 404 from the route. A token is never
logged and never stored: the ledger keeps its sha256 inside the request's digest only.

`view` builds the `CustomerOrderView`: the status in the customer's words (`ORDER_WORDS`,
`ITEM_WORDS`: a mapping, not new states), each item's sent revisions with their pictures
as presigned GETs, its price, the payment requests (an open one with its pay link; "paid"
only once the provider's payment settled it, spec §7) and the shipments. Every field is in
`storefront/wire.py` `ORDER_VIEW_FIELDS`: no cost, margin, internal note, shop event or
staff name. The first view stamps the link's ``opened_at``.

The customer's writes (`answer`, `accept`, `cancel`) run as `customer_actor`, with the
session ``link:<link id>``, through the lifecycle under the order's lock, in one
transaction with their ``Idempotency-Key``'s ledger row (`InTransaction`): a retry answers
once. Each answers the view as it is after the write.

A revision's picture is kept as a packed blob (``attachment-<sha256>``, which no browser
can open), so the first view copies its bytes to `customer_file_key` on the S3 content
store, held with the attachment (`hold_with_attachment`), and presigns that. On any store
but S3 the pictures are left out (`listings.public_content` logs why); the page still
works.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from fastapi import status
from pydantic import BaseModel, ValidationError

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent_write
from scadbuddy.orders.attachments import blob_path
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.lifecycle import ENDED, SETTLED, InTransaction, LifecycleError
from scadbuddy.orders.models import (
    SHOWN_IMAGES,
    Actor,
    Attachment,
    ItemStatus,
    Order,
    OrderItem,
    OrderStatus,
    Quote,
    Revision,
    Shipment,
)
from scadbuddy.orders.revisions import RevisionAnswerInput
from scadbuddy.orders.store import Conn
from scadbuddy.orders.storefront.customers import customer_actor
from scadbuddy.orders.storefront.links import link_by_token, mark_opened, token_sha256
from scadbuddy.orders.storefront.listings import PUBLIC_MEDIA_TTL, problem, public_content
from scadbuddy.orders.storefront.wire import (
    CancelBody,
    CustomerItem,
    CustomerOrderView,
    CustomerPayment,
    CustomerPicture,
    CustomerQuote,
    CustomerRevision,
    CustomerShipment,
    RevisionAnswerBody,
)
from scadbuddy.payments import store as payments
from scadbuddy.payments.models import PaymentRequest, PaymentRequestView
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.s3 import S3ContentBackend
from scadbuddy.storefront.wire import QUOTE_ID, REVISION_ID

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

logger = logging.getLogger(__name__)

#: The ledger's names of the routes (`idempotent_write`).
ANSWER_ROUTE: Final = "POST /storefront/order/revisions/{revision_id}/answer"
ACCEPT_ROUTE: Final = "POST /storefront/order/quotes/{quote_id}/accept"
CANCEL_ROUTE: Final = "POST /storefront/order/cancel"
#: The longest token read; any longer body reads as no token (an order link's is 43).
MAX_TOKEN_CHARS: Final = 100

#: The order's state in the customer's words. A submitted order stays `draft` (§5.5).
ORDER_WORDS: Final[dict[OrderStatus, str]] = {
    "draft": "We are reviewing your order",
    "quoted": "Your price is ready",
    "accepted": "Accepted: we will start making it soon",
    "in_production": "We are making it",
    "ready": "Ready",
    "shipped": "On its way",
    "closed": "Complete",
    "cancelled": "Cancelled",
}
#: An item's state in the customer's words. A failed print is the shop's to try again.
ITEM_WORDS: Final[dict[ItemStatus, str]] = {
    "draft": "We are working on the design",
    "quoted": "Price ready",
    "approved": "Approved",
    "queued": "Waiting to print",
    "printing": "Printing",
    "printed": "Printed",
    "fulfilled": "Done",
    "failed": "We are making it",
    "cancelled": "Cancelled",
}
#: The video types a picture may be; images are `SHOWN_IMAGES`.
_VIDEOS: Final = frozenset({"video/mp4", "video/webm", "video/quicktime"})


def customer_file_key(sha256: str) -> str:
    """The raw copy of a revision's picture, which the customer's presigned GET reads."""
    return f"customer-file-{sha256}"


@dataclass(frozen=True)
class ByLink:
    """A reader who holds the order link's token (``/o#<token>``)."""

    token: str


#: Who reads an order. The account path (``/o?order=<id>`` with an account session, which
#: owns the order) adds its own reader here.
Reader = ByLink


@dataclass(frozen=True)
class Access:
    """The order a reader may read and answer, and the customer actor's session."""

    order_id: str
    session: str
    link_id: str


def resolve(conn: Conn, reader: Reader) -> Access | None:
    """The order ``reader`` opens, or None for a reader that reads nothing."""
    link = link_by_token(conn, reader.token)
    if link is None:
        return None
    return Access(order_id=link.order_id, session=f"link:{link.id}", link_id=link.id)


def read_body(raw: bytes) -> tuple[ByLink, dict[str, Any]]:
    """The body's reader and the body. A body that is not a JSON object, or a token that
    is not a string of at most `MAX_TOKEN_CHARS`, reads as the empty token, which reads
    nothing: the route answers it as any other unknown token."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return ByLink(""), {}
    token = body.get("token")
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
        token = ""
    return ByLink(token), body


def _orders(state: AppState) -> OrdersComponent:
    orders: OrdersComponent = state.components.get(ORDERS)
    return orders


# -- the view --------------------------------------------------------------------------


@dataclass
class _Rows:
    """What one view reads, in one transaction."""

    order: Order
    items: list[OrderItem]
    revisions: dict[str, list[Revision]]
    quotes: dict[str, list[Quote]]
    pictures: dict[str, list[tuple[Attachment, str]]]
    requests: list[PaymentRequestView]
    open_requests: list[PaymentRequest]
    shipments: list[Shipment]
    approved: bool
    copies: dict[str, bool] = field(default_factory=dict)


def _picture(attachment: Attachment) -> bool:
    return (attachment.kind == "image" and attachment.media_type in SHOWN_IMAGES) or (
        attachment.kind == "video" and attachment.media_type in _VIDEOS
    )


def _read(conn: Conn, orders: OrdersComponent, order_id: str) -> _Rows:
    store = orders.store
    order = store.order(order_id, conn=conn)
    assert order is not None  # A link's foreign key keeps its order.
    items = store.items(order_id, conn=conn)
    revisions = {item.id: store.revisions(item.id, conn=conn) for item in items}
    sent = {r.id for rows in revisions.values() for r in rows if r.sent_at is not None}
    pictures: dict[str, list[tuple[Attachment, str]]] = {}
    for attachment in store.attachments(order_id, conn=conn):
        if attachment.revision_id in sent and _picture(attachment):
            found = store.attachment(attachment.id, conn=conn)
            if found is not None:
                pictures.setdefault(attachment.revision_id, []).append(found)
    return _Rows(
        order=order,
        items=items,
        revisions=revisions,
        quotes={item.id: store.quotes(item.id, conn=conn) for item in items},
        pictures=pictures,
        requests=payments.requests_for_order(conn, order_id),
        open_requests=payments.open_requests_for_order(conn, order_id),
        shipments=store.shipments(order_id, conn=conn),
        approved=orders.lifecycle.approved_anything(conn, order),
    )


async def _picture_url(
    state: AppState, content: ContentStore, attachment: Attachment, blob_key: str
) -> str | None:
    """A presigned GET of the picture's raw copy, made from its packed blob on the first
    view; None (logged, never with a URL) when the store has lost it."""
    key = customer_file_key(attachment.sha256)
    stat = await asyncio.to_thread(content.index.get, key)
    if stat is None or stat.ref.backend != content.name:
        path = await blob_path(state.store.blobs, blob_key)
        if path is None:
            logger.warning(
                "attachment %s has no stored file; its picture is left out",
                attachment.id,
                extra={"attachment_id": attachment.id},
            )
            return None
        with path.open("rb") as source:
            await content.put("media", source, name=attachment.filename, scope=BlobScope(), key=key)
        orders = _orders(state)

        def hold() -> None:
            with orders.store.transaction() as conn:
                orders.store.hold_with_attachment(conn, key, attachment.id)

        await asyncio.to_thread(hold)
        stat = await asyncio.to_thread(content.index.get, key)
        if stat is None:
            return None
    backend = content.backend
    assert isinstance(backend, S3ContentBackend)
    return await backend.presign_get(
        stat.ref.backend_id, PUBLIC_MEDIA_TTL, media_type=attachment.media_type
    )


def _quote(item: OrderItem, rows: _Rows, revisions: list[Revision]) -> CustomerQuote | None:
    """The item's price: its accepted quote, else its newest, shown once the design it
    names was sent (an item with no design shows its price at once)."""
    quotes = rows.quotes[item.id]
    if not quotes:
        return None
    quote = next((q for q in quotes if q.accepted), quotes[-1])
    by_id = {r.id: r for r in revisions}
    named = by_id.get(quote.revision_id) if quote.revision_id else None
    if revisions and (named is None or named.sent_at is None):
        return None
    open_order = rows.order.status in ("draft", "quoted") and not rows.order.on_hold
    can_accept = (
        not quote.accepted
        and open_order
        and item.status in ("draft", "quoted")
        and quote is quotes[-1]
        and (
            not revisions
            or (
                item.approval == "design_first" and named is not None and named.answer == "accepted"
            )
        )
    )
    return CustomerQuote(
        id=quote.id,
        unit_price_minor=quote.unit_price_minor,
        quantity=quote.quantity,
        total_minor=quote.total_minor,
        accepted=quote.accepted,
        can_accept=can_accept,
    )


async def _item(
    state: AppState, content: ContentStore | None, item: OrderItem, rows: _Rows
) -> CustomerItem:
    revisions = rows.revisions[item.id]
    newest = revisions[-1] if revisions else None
    open_item = rows.order.status not in ("shipped", *ENDED) and item.status not in SETTLED
    shown: list[CustomerRevision] = []
    for revision in revisions:
        if revision.sent_at is None:
            continue
        pictures: list[CustomerPicture] = []
        for attachment, blob_key in rows.pictures.get(revision.id, []) if content else []:
            assert content is not None
            url = await _picture_url(state, content, attachment, blob_key)
            if url is not None:
                pictures.append(
                    CustomerPicture(
                        url=url,
                        kind="video" if attachment.kind == "video" else "image",
                        filename=attachment.filename,
                    )
                )
        shown.append(
            CustomerRevision(
                id=revision.id,
                number=revision.number,
                note=revision.note,
                sent_at=revision.sent_at,
                answer=revision.answer,
                answer_note=revision.answer_note,
                can_answer=revision is newest and revision.answer is None and open_item,
                pictures=pictures,
            )
        )
    return CustomerItem(
        id=item.id,
        title=item.title,
        quantity=item.quantity,
        status=ITEM_WORDS[item.status],
        revisions=shown,
        quote=_quote(item, rows, revisions),
    )


def _payments(rows: _Rows) -> list[CustomerPayment]:
    """Each request the customer can act on or has paid: an open one with its pay link,
    a pending one (the provider is settling it) and a paid one."""
    links = {r.id: r.hosted_url for r in rows.open_requests}
    shown: list[CustomerPayment] = []
    for request in rows.requests:
        if request.status not in ("open", "pending", "paid"):
            continue
        shown.append(
            CustomerPayment(
                id=request.id,
                kind=request.kind,
                amount_minor=request.amount_minor,
                currency=request.currency,
                status=request.status,
                pay_url=links.get(request.id) if request.status == "open" else None,
            )
        )
    return shown


async def _build(state: AppState, rows: _Rows) -> CustomerOrderView:
    content: ContentStore | None = None
    if rows.pictures:
        try:
            content = public_content(state)
        except ApiError:
            content = None
    order = rows.order
    assert order.number is not None  # A link is made only for a submitted order.
    return CustomerOrderView(
        number=order.number,
        status=ORDER_WORDS[order.status],
        currency=order.currency,
        minor_exponent=order.minor_exponent,
        can_cancel=not rows.approved and order.status not in ("shipped", *ENDED),
        items=[
            await _item(state, content, item, rows)
            for item in rows.items
            if item.status != "cancelled" or order.status == "cancelled"
        ],
        payments=_payments(rows),
        shipments=[
            CustomerShipment(
                carrier=s.carrier, service=s.service, tracking=s.tracking, shipped_at=s.shipped_at
            )
            for s in rows.shipments
        ],
    )


async def view(state: AppState, reader: Reader) -> CustomerOrderView | None:
    """The order ``reader`` opens, as its customer reads it; None when it opens none. The
    first view stamps the link's ``opened_at``."""
    orders = _orders(state)

    def read() -> _Rows | None:
        with orders.store.transaction() as conn:
            access = resolve(conn, reader)
            if access is None:
                return None
            mark_opened(conn, access.link_id)
            return _read(conn, orders, access.order_id)

    rows = await asyncio.to_thread(read)
    return None if rows is None else await _build(state, rows)


# -- the customer's writes ---------------------------------------------------------------

#: What the customer reads for a refusal of the lifecycle's, by its slug (409 unless the
#: slug is in `_NOT_FOUND`).
_REFUSALS: Final[dict[str, str]] = {
    "revision_answered": "This design was answered already. Load the page again.",
    "revision_not_newest": "There is a newer design. Load the page again.",
    "revision_not_sent": "This design was not sent to you yet.",
    "revision_not_quoted": "This design has no price yet. The shop will send one.",
    "revision_not_accepted": "Accept the design first.",
    "quote_stale": "This price is for an older design. Load the page again.",
    "item_not_open": "This item can no longer change.",
    "order_approved": "Part of your order is approved already. Ask the shop to cancel it.",
    "order_not_cancellable": "This order can no longer be cancelled.",
    "order_on_hold": "The shop has paused this order. Ask the shop about it.",
    "payment_in_flight": "A payment is being confirmed. Try again once it is.",
}
_NOT_FOUND: Final = frozenset({"revision_not_found", "quote_not_found", "item_not_found"})


def _refusal(error: LifecycleError) -> ApiError:
    """What the customer reads for the lifecycle's refusal: never its own text, which
    names the shop's ids."""
    if error.slug in _NOT_FOUND or (error.not_found and error.slug != "order_not_found"):
        return problem(status.HTTP_404_NOT_FOUND, error.slug, "There is no such item.")
    if error.slug == "note":
        return problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "note", "Say what you would like changed."
        )
    if error.unprocessable:
        return problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT, error.slug, "This answer could not be sent."
        )
    if error.slug in _REFUSALS:
        return problem(status.HTTP_409_CONFLICT, error.slug, _REFUSALS[error.slug])
    return problem(
        status.HTTP_409_CONFLICT, "order_changed", "Your order has changed. Load the page again."
    )


def _body[Body: BaseModel](model: type[Body], body: dict[str, Any], slug: str) -> Body:
    """``model`` from the body past its token; 422 ``slug`` naming nothing it was sent."""
    try:
        return model.model_validate(body)
    except ValidationError:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT, slug, "This request is not one the page sends."
        ) from None


class _UnreadError(Exception):
    """The reader's order is gone from under the write (its link revoked meanwhile)."""


_Act = Callable[[Conn, Access, Order, InTransaction], None]


async def _access(state: AppState, reader: Reader) -> Access | None:
    """`resolve` in a transaction of its own: what a write checks before it reads the
    rest of its body, so a token that reads nothing is refused alike whatever came with
    it."""
    orders = _orders(state)

    def first() -> Access | None:
        with orders.store.transaction() as conn:
            return resolve(conn, reader)

    return await asyncio.to_thread(first)


async def _write(
    state: AppState,
    reader: Reader,
    access: Access,
    key: str,
    route: str,
    asked: dict[str, Any],
    act: _Act,
) -> CustomerOrderView | None:
    """``act`` once per ``key`` as the reader's customer, in one transaction with the
    key's ledger row; the view after it. A retry of a committed write (the same key)
    answers the view and does nothing else. None when the reader's link was revoked
    meanwhile."""
    orders = _orders(state)
    asked = {"link": token_sha256(reader.token), **asked}

    def run() -> None:
        with state.projection.pool.connection() as conn:
            within = InTransaction(conn)

            def write() -> Replay:
                if resolve(conn, reader) != access:
                    raise _UnreadError
                order = orders.store.order(access.order_id, conn=conn)
                assert order is not None
                act(conn, access, order, within)
                return Replay(200, {"order_id": access.order_id})

            idempotent_write(conn, "storefront", key, route, asked, write)
        orders.lifecycle.tell(within)

    try:
        await asyncio.to_thread(run)
    except _UnreadError:
        return None
    except LifecycleError as error:
        raise _refusal(error) from None
    return await view(state, reader)


def _actor(access: Access, order: Order) -> Actor:
    if order.customer_id is None:
        # A back-office order whose link went to an address with no customer behind it.
        raise problem(
            status.HTTP_409_CONFLICT,
            "order_changed",
            "The shop must finish setting up this order. Ask the shop about it.",
        )
    return customer_actor(order.customer_id, access.session)


def _revision_of(conn: Conn, orders: OrdersComponent, order: Order, revision_id: str) -> Revision:
    """The revision when it is one of ``order``'s items'; else 404 ``revision_not_found``."""
    found = (
        orders.store.revision(revision_id, conn=conn)
        if REVISION_ID.fullmatch(revision_id)
        else None
    )
    item = orders.store.item(found.item_id, conn=conn) if found is not None else None
    if found is None or item is None or item.order_id != order.id:
        raise problem(status.HTTP_404_NOT_FOUND, "revision_not_found", "There is no such design.")
    return found


def _quote_of(conn: Conn, orders: OrdersComponent, order: Order, quote_id: str) -> Quote:
    """The quote when it is one of ``order``'s items'; else 404 ``quote_not_found``."""
    found = orders.store.quote(quote_id, conn=conn) if QUOTE_ID.fullmatch(quote_id) else None
    item = orders.store.item(found.order_item_id, conn=conn) if found is not None else None
    if found is None or item is None or item.order_id != order.id:
        raise problem(status.HTTP_404_NOT_FOUND, "quote_not_found", "There is no such price.")
    return found


async def answer(
    state: AppState, reader: Reader, key: str, revision_id: str, body: dict[str, Any]
) -> CustomerOrderView | None:
    """The customer's answer to the newest sent revision (`Lifecycle.answer_revision`):
    ``accepted``, or ``changes_requested`` with a note (422 ``note`` without). Refusals:
    404 ``revision_not_found``, 409 ``revision_answered`` (another tab answered first),
    409 ``revision_not_newest``, 422 ``answer_body_invalid``."""
    access = await _access(state, reader)
    if access is None:
        return None
    entry = _body(RevisionAnswerBody, body, "answer_body_invalid")
    orders = _orders(state)

    def act(conn: Conn, access: Access, order: Order, within: InTransaction) -> None:
        revision = _revision_of(conn, orders, order, revision_id)
        orders.lifecycle.answer_revision(
            order.id,
            revision.item_id,
            revision.id,
            RevisionAnswerInput(answer=entry.answer, note=entry.note),
            _actor(access, order),
            within=within,
        )

    asked = {"revision_id": revision_id, **entry.model_dump()}
    return await _write(state, reader, access, key, ANSWER_ROUTE, asked, act)


async def accept(
    state: AppState, reader: Reader, key: str, quote_id: str
) -> CustomerOrderView | None:
    """The customer accepts a price (`Lifecycle.accept_quote`). Refusals: 404
    ``quote_not_found``, 409 ``revision_not_accepted`` (a ``design_first`` item's design
    first), 409 ``quote_stale``."""
    access = await _access(state, reader)
    if access is None:
        return None
    orders = _orders(state)

    def act(conn: Conn, access: Access, order: Order, within: InTransaction) -> None:
        quote = _quote_of(conn, orders, order, quote_id)
        orders.lifecycle.accept_quote(
            order.id, quote.order_item_id, quote.id, _actor(access, order), within=within
        )

    return await _write(state, reader, access, key, ACCEPT_ROUTE, {"quote_id": quote_id}, act)


async def cancel(
    state: AppState, reader: Reader, key: str, body: dict[str, Any]
) -> CustomerOrderView | None:
    """The customer cancels the order while nothing is approved (`Lifecycle.cancel_order`).
    Refusals: 409 ``order_approved``, 409 ``order_not_cancellable``, 409
    ``payment_in_flight``, 422 ``cancel_body_invalid``."""
    access = await _access(state, reader)
    if access is None:
        return None
    entry = _body(CancelBody, body, "cancel_body_invalid")
    orders = _orders(state)
    reason = entry.reason.strip() or None

    def act(conn: Conn, access: Access, order: Order, within: InTransaction) -> None:
        orders.lifecycle.cancel_order(order.id, _actor(access, order), reason, within=within)

    return await _write(state, reader, access, key, CANCEL_ROUTE, {"reason": reason}, act)
