"""The customer's order page (#5283, spec 2026-11-02 §5.2, §5.6, §6, §7, plan Task 5).

A customer reads their order and answers it at ``/o#<token>``: the page sends the order
link's token in each request's body, never in a path, and the edge forwards it unread.
`resolve` is the one way to an order: a `Reader` gives an `Access` (the order and the
customer's session), or None. A `ByLink` reader holds the link's token; a `ByAccount`
reader (``/o?order=<id>``) is the signed-in account of the request's store session, which
must own the order (`accounts.account_owns_order`), and every failure there (no session,
another account's order, an unknown id) is the same empty 404. A link opened while signed
in attaches the order to the account (`accounts.attach_order`), in the view's
transaction. Any token that reads nothing (unknown, revoked, or not a token at all) costs
the same one indexed lookup (`links.link_by_token`) and gets the same empty 404 from the
route. A token is never
logged and never stored: the ledger keeps its sha256 inside the request's digest only.

`view` builds the `CustomerOrderView`: the status in the customer's words (`ORDER_WORDS`,
`ITEM_WORDS`: a mapping, not new states), each item's sent revisions with their pictures
as presigned GETs, its price, the payment requests (an open one with its pay link; "paid"
only once the provider's payment settled it, spec §7) and the shipments. Every field is in
`storefront/wire.py` `ORDER_VIEW_FIELDS`: no cost, margin, internal note, shop event or
staff name. The first view stamps the link's ``opened_at``.

The customer's writes (`answer`, `accept`, `cancel`) run as `customer_actor`, with the
session ``link:<link id>`` (a link's reader) or ``account:<account id>`` (a signed-in
reader), through the lifecycle under the order's lock, in one
transaction with their ``Idempotency-Key``'s ledger row (`InTransaction`): a retry answers
once. Each answers the view as it is after the write.

A revision's picture (only the design's own files and the customer's, `_SHOWN_ROLES`) is
kept as a packed blob (``attachment-<sha256>``, which no browser can open), so the first
view copies its bytes to `customer_file_key` on the S3 content store and presigns that.
Every attachment that shows the copy holds it (`hold_with_attachment`). A picture that
cannot be shown now (the copy failed, the file is past `MAX_PICTURE_COPY_BYTES`, or the
store is not S3, which `listings.public_content` logs) is listed with no URL: the view
never fails for one. A render's 3D preview (`PREVIEW_3D`) is one too, of kind ``model``,
presigned the same way for the storefront's viewer. Its caption is the shop's caption or
"Design N, picture K", never the file's own name. Each revision also lists its colours by
slot, with the price book's colour names: they are part of the design the customer
accepts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from fastapi import status
from pydantic import BaseModel, ValidationError

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent_write
from scadbuddy.orders.attachments import blob_path
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.lifecycle import ENDED, SETTLED, InTransaction, LifecycleError
from scadbuddy.orders.models import (
    REVISION_ROLES,
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
from scadbuddy.orders.storefront.accounts import account_owns_order, attach_order, submitted
from scadbuddy.orders.storefront.customers import customer_actor
from scadbuddy.orders.storefront.links import link_by_token, mark_opened, token_sha256
from scadbuddy.orders.storefront.listings import PUBLIC_MEDIA_TTL, problem, public_content
from scadbuddy.orders.storefront.wire import (
    CancelBody,
    CustomerColour,
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
from scadbuddy.payments.component import PAYMENTS
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
#: The largest picture the first view copies for the customer: a design's render or a
#: test print's photo or short video is well below it. A larger file is shown without a
#: link rather than held up in the view's request.
MAX_PICTURE_COPY_BYTES: Final = 25 * 1024 * 1024
#: The roles of a revision's files the customer sees: the design's own (`REVISION_ROLES`)
#: and the files the customer sent with a request for changes. Never a file the shop
#: added for itself.
_SHOWN_ROLES: Final = REVISION_ROLES | {"reference"}
#: The video types a picture may be; images are `SHOWN_IMAGES`.
_VIDEOS: Final = frozenset({"video/mp4", "video/webm", "video/quicktime"})
#: A render's 3D preview, which the storefront's viewer (`components/Preview.tsx`) shows.
PREVIEW_3D: Final = "model/gltf-binary"


def customer_file_key(sha256: str) -> str:
    """The raw copy of a revision's picture, which the customer's presigned GET reads."""
    return f"customer-file-{sha256}"


@dataclass(frozen=True)
class ByLink:
    """A reader who holds the order link's token (``/o#<token>``). ``account_id`` is the
    signed-in account of the request's store session, if any: a view attaches the order
    to it (spec §5.2), and nothing else does."""

    token: str
    account_id: str | None = None

    def ledger(self) -> dict[str, str]:
        """What names this reader in a write's ledger digest: the token's sha256 only."""
        return {"link": token_sha256(self.token)}


@dataclass(frozen=True)
class ByAccount:
    """A signed-in reader (``/o?order=<id>``): the account of the request's store session
    (None with no live session) and the order it asks for. It reads only an order the
    account owns (`accounts.account_owns_order`)."""

    account_id: str | None
    order_id: str

    def ledger(self) -> dict[str, str]:
        return {"account": self.account_id or "", "order": self.order_id}


#: Who reads an order: a link's holder, or a signed-in account that owns it.
Reader = ByLink | ByAccount


@dataclass(frozen=True)
class Access:
    """The order a reader may read and answer, the customer actor's session
    (``link:<id>`` or ``account:<id>``), and the link that opened it, if one did."""

    order_id: str
    session: str
    link_id: str | None


def resolve(conn: Conn, reader: Reader) -> Access | None:
    """The order ``reader`` opens, or None for a reader that reads nothing: an unknown,
    revoked or malformed token, no live session, or an order the account does not own.
    Every one of them is the same empty 404."""
    if isinstance(reader, ByAccount):
        if reader.account_id is None or not reader.order_id:
            return None
        if not account_owns_order(conn, reader.account_id, reader.order_id):
            return None
        if not submitted(conn, reader.order_id):
            return None  # A cart is not an order page.
        return Access(
            order_id=reader.order_id, session=f"account:{reader.account_id}", link_id=None
        )
    link = link_by_token(conn, reader.token)
    if link is None:
        return None
    return Access(order_id=link.order_id, session=f"link:{link.id}", link_id=link.id)


def read_body(raw: bytes, account_id: str | None = None) -> tuple[Reader, dict[str, Any]]:
    """The body's reader and the body: ``token`` (a link), else ``order_id`` (the signed-in
    ``account_id``'s). A body that is not a JSON object, or a token or an order id that is
    not a string of at most `MAX_TOKEN_CHARS`, reads as the empty token, which reads
    nothing: the route answers it as any other unknown token."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return ByLink("", account_id), {}

    def text(name: str) -> str | None:
        value = body.get(name)
        return value if isinstance(value, str) and len(value) <= MAX_TOKEN_CHARS else None

    token = text("token")
    if token is None and "token" not in body:
        order_id = text("order_id")
        if order_id is not None:
            return ByAccount(account_id, order_id), body
    return ByLink(token or "", account_id), body


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
    paid: bool
    #: The price book's colour names, by filament id.
    colour_names: dict[str, str | None]


def _picture(attachment: Attachment) -> bool:
    if attachment.role not in _SHOWN_ROLES:
        return False
    return (
        (attachment.kind == "image" and attachment.media_type in SHOWN_IMAGES)
        or (attachment.kind == "video" and attachment.media_type in _VIDEOS)
        or attachment.media_type == PREVIEW_3D
    )


def _picture_kind(attachment: Attachment) -> Literal["image", "video", "model"]:
    if attachment.media_type == PREVIEW_3D:
        return "model"
    return "video" if attachment.kind == "video" else "image"


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
        paid=payments.has_payment(conn, order_id),
        colour_names={
            f.id: f.colour_name for f in store.filaments(include_archived=True, conn=conn)
        },
    )


async def _picture_url(
    state: AppState, content: ContentStore, attachment: Attachment, blob_key: str
) -> str | None:
    """A presigned GET of the picture's raw copy, made from its packed blob on the first
    view and held by every attachment that shows it. None, logged by the attachment's
    id only (never a URL or a token), when it cannot be shown now: the blob is lost, the
    file is past `MAX_PICTURE_COPY_BYTES` with no copy yet, or any step failed. The view
    then lists the picture with no URL; it never fails for one."""
    key = customer_file_key(attachment.sha256)
    try:
        stat = await asyncio.to_thread(content.index.get, key)
        if stat is None or stat.ref.backend != content.name:
            if attachment.size_bytes > MAX_PICTURE_COPY_BYTES:
                logger.warning(
                    "attachment %s is too large to copy for the customer; its picture is"
                    " shown without a link",
                    attachment.id,
                    extra={"attachment_id": attachment.id, "size_bytes": attachment.size_bytes},
                )
                return None
            path = await blob_path(state.store.blobs, blob_key)
            if path is None:
                logger.warning(
                    "attachment %s has no stored file; its picture is shown without a link",
                    attachment.id,
                    extra={"attachment_id": attachment.id},
                )
                return None
            with path.open("rb") as source:
                await content.put(
                    "media", source, name=f"{attachment.id}", scope=BlobScope(), key=key
                )
            stat = await asyncio.to_thread(content.index.get, key)
            if stat is None:
                return None
        orders = _orders(state)

        def hold() -> None:
            # Every attachment that shows the copy holds it, so deleting one never
            # sweeps a copy another still shows. Idempotent.
            with orders.store.transaction() as conn:
                orders.store.hold_with_attachment(conn, key, attachment.id)

        await asyncio.to_thread(hold)
        backend = content.backend
        assert isinstance(backend, S3ContentBackend)
        return await backend.presign_get(
            stat.ref.backend_id, PUBLIC_MEDIA_TTL, media_type=attachment.media_type
        )
    except Exception:
        logger.warning(
            "attachment %s could not be copied or signed for the customer; its picture is"
            " shown without a link",
            attachment.id,
            exc_info=True,
            extra={"attachment_id": attachment.id},
        )
        return None


def _quote(item: OrderItem, rows: _Rows, revisions: list[Revision]) -> CustomerQuote | None:
    """The item's price: its accepted quote, else its newest, shown once the design it
    names was sent (an item with no design shows its price at once)."""
    quotes = rows.quotes[item.id]
    if not quotes:
        return None
    quote = next((q for q in quotes if q.accepted), quotes[-1])
    by_id = {r.id: r for r in revisions}
    # A quote that names no design (a manual price, #4250) prices the item as the
    # customer last saw it: the newest design sent to them.
    sent = [r for r in revisions if r.sent_at is not None]
    named = by_id.get(quote.revision_id) if quote.revision_id else (sent[-1] if sent else None)
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
        held = rows.pictures.get(revision.id, [])
        # The first view copies each picture into the store; the copies run side by side.
        urls: list[str | None] = (
            list(await asyncio.gather(*(_picture_url(state, content, a, k) for a, k in held)))
            if content is not None
            else [None] * len(held)
        )
        pictures = [
            CustomerPicture(
                url=url,
                kind=_picture_kind(attachment),
                caption=attachment.caption.strip() or f"Design {revision.number}, picture {number}",
            )
            for number, ((attachment, _key), url) in enumerate(zip(held, urls, strict=True), 1)
        ]
        shown.append(
            CustomerRevision(
                id=revision.id,
                number=revision.number,
                note=revision.note,
                sent_at=revision.sent_at,
                answer=revision.answer,
                answer_note=revision.answer_note,
                can_answer=revision is newest
                and revision.answer is None
                and open_item
                and not rows.order.on_hold,
                pictures=pictures,
                colours=[
                    CustomerColour(
                        slot=slot.slot,
                        colour=slot.colour,
                        name=rows.colour_names.get(slot.filament_id) if slot.filament_id else None,
                    )
                    for slot in revision.colours
                ],
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
        can_cancel=not rows.approved and not rows.paid and order.status not in ("shipped", *ENDED),
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


async def view(state: AppState, reader: Reader, *, attach: bool = True) -> CustomerOrderView | None:
    """The order ``reader`` opens, as its customer reads it; None when it opens none. The
    first view stamps the link's ``opened_at``. A link opened while signed in attaches the
    order to the account, bound to that link (`accounts.attach_order`), only when
    ``attach``: the view that answers a write passes False, so only an explicit view
    attaches."""
    orders = _orders(state)

    def read() -> _Rows | None:
        with orders.store.transaction() as conn:
            access = resolve(conn, reader)
            if access is None:
                return None
            if access.link_id is not None:
                mark_opened(conn, access.link_id)
                if attach and isinstance(reader, ByLink) and reader.account_id is not None:
                    # A link opened while signed in joins that account (spec §5.2).
                    attach_order(conn, reader.account_id, access.order_id, access.link_id)
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
    "order_paid": "You have paid part of this order. Ask the shop to cancel it.",
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
#: What runs once a customer's write accepted the order (the API's `ensure_order_projects`).
AfterAccept = Callable[[Order], Awaitable[None]]


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
    accepted: AfterAccept | None = None,
) -> CustomerOrderView | None:
    """``act`` once per ``key`` as the reader's customer, in one transaction with the
    key's ledger row; the view after it. A retry of a committed write (the same key)
    answers the view and does nothing else. None when the reader's link was revoked
    meanwhile.

    The write runs as `POST /orders/{id}/accept` and `/cancel` run theirs
    (`Payments.run_closing_linked`): an answer or an accept that accepts the order, or a
    cancel, cancels the order's open requests, and their provider pages close right after
    the commit, so the customer cannot pay a cancelled request; a balance request the
    acceptance opens is linked. When the order became accepted, ``accepted`` runs with
    it (the API's `ensure_order_projects`, best effort)."""
    orders = _orders(state)
    asked = {**reader.ledger(), **asked}

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

    before = await asyncio.to_thread(orders.store.order, access.order_id)
    try:
        await state.components.get(PAYMENTS).service.run_closing_linked(access.order_id, run)
    except _UnreadError:
        return None
    except LifecycleError as error:
        raise _refusal(error) from None
    if accepted is not None and before is not None and before.status != "accepted":
        after = await asyncio.to_thread(orders.store.order, access.order_id)
        if after is not None and after.status == "accepted":
            await accepted(after)
    return await view(state, reader, attach=False)


def _actor(access: Access, order: Order) -> Actor:
    if order.customer_id is None:
        # A guard only: `Lifecycle.send_customer_link` refuses an order with no customer
        # (`no_customer`), and a submit always sets one.
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
    state: AppState,
    reader: Reader,
    key: str,
    revision_id: str,
    body: dict[str, Any],
    accepted: AfterAccept | None = None,
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
    return await _write(state, reader, access, key, ANSWER_ROUTE, asked, act, accepted)


async def accept(
    state: AppState, reader: Reader, key: str, quote_id: str, accepted: AfterAccept | None = None
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

    asked = {"quote_id": quote_id}
    return await _write(state, reader, access, key, ACCEPT_ROUTE, asked, act, accepted)


async def cancel(
    state: AppState, reader: Reader, key: str, body: dict[str, Any]
) -> CustomerOrderView | None:
    """The customer cancels the order while nothing is approved and nothing is paid
    (`Lifecycle.cancel_order`). Refusals: 409 ``order_approved``, 409 ``order_paid`` (a
    payment is recorded, a deposit included: the shop refunds, so the customer asks the
    shop), 409 ``order_not_cancellable``, 409 ``payment_in_flight``, 422
    ``cancel_body_invalid``."""
    access = await _access(state, reader)
    if access is None:
        return None
    entry = _body(CancelBody, body, "cancel_body_invalid")
    orders = _orders(state)
    reason = entry.reason.strip() or None

    def act(conn: Conn, access: Access, order: Order, within: InTransaction) -> None:
        orders.lifecycle.cancel_order(order.id, _actor(access, order), reason, within=within)

    return await _write(state, reader, access, key, CANCEL_ROUTE, {"reason": reason}, act)
