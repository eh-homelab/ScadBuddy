"""A visitor's draft order (#5282, spec 2026-11-02 §5.4, plan Task 8).

The draft is the visitor's cart: an order with ``source = 'storefront'``, no number and
no customer until it is submitted (`Lifecycle.ensure_draft`). `add_listing_item` checks
the visitor's values against the listing, never trusting the edge
(`renders.py` `checked_choice`), and adds a model item with revision 1 from the listing's
snapshot, titled as the listing, and records the names of the parameters the listing
offered: the draft shows only those values, never the listing's locks or the template's
other defaults. A draft holds at most `MAX_DRAFT_ITEMS` items. The add runs in one
transaction with its ``Idempotency-Key``'s ledger row (`idempotent_write`), so a request
the edge sends again after a lost answer adds one item. Each write looks up its key before
any other work (`stored`): a retry of a write that committed answers with the order that
write named, whatever changed since, and checks or makes nothing. `remove_item` does the
same for a delete. The bus is told after the commit, of the order as the transaction left
it.

`add_request` adds a design request: a ``request`` item whose notes are the visitor's text,
with each file as a ``reference`` attachment. What a file is comes from its bytes
(`sniff`), never its name or sent type, and only `REQUEST_KINDS` are taken. Each file is
kept twice: as ``attachment-<sha256>``, the packed blob the back office reads, and raw as
`request_file_key`, which the visitor reads as a presigned download of its sniffed media
type. The attachment row holds both, so removing the item (or the draft sweep's
`delete_attachment`) drops both holds.

Every write names the visitor's row (`Lifecycle` ``cart_of``), which the lifecycle checks
under the order's lock: another visitor's draft or item is 404 ``item_not_found``, never
403. A read (`get_draft`) makes no visitor row: a visitor that never wrote has no draft.
The visitor reads its items' files as presigned GETs (`PUBLIC_MEDIA_TTL`), never a path.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, Final
from urllib.parse import quote

from fastapi import status
from starlette.datastructures import UploadFile

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, has_reply, idempotent_write, stored
from scadbuddy.orders.attachments import SNIFF_BYTES, clean_filename, keep_file, sniff
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.lifecycle import LifecycleError, NewModelItem, NewRequestItem
from scadbuddy.orders.models import Attachment, AttachmentKind, Order, OrderItem
from scadbuddy.orders.store import item_subject
from scadbuddy.orders.storefront.listings import PUBLIC_MEDIA_TTL, problem, public_content
from scadbuddy.orders.storefront.model_requests import draft_sources
from scadbuddy.orders.storefront.renders import checked_choice
from scadbuddy.orders.storefront.staged_files import poke, uploads_of
from scadbuddy.orders.storefront.visitors import VisitorStore, token_sha256, visitor_actor
from scadbuddy.orders.storefront.wire import (
    MAX_REQUEST_FILE_BYTES,
    MAX_REQUEST_FILES,
    REQUEST_KINDS,
    AddListingItem,
    DraftAttachment,
    DraftFileKind,
    DraftItem,
    DraftView,
    Listing,
    PublicShippingOption,
    shown_label,
)
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.s3 import S3ContentBackend
from scadbuddy.storefront.uploads import checked_text, file_too_large, too_many_files
from scadbuddy.storefront.wire import ITEM_ID, unescape_filename

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

#: The ledger's names of the routes (`idempotent_write`).
ADD_ROUTE: Final = "POST /storefront/draft/items"
REMOVE_ROUTE: Final = "DELETE /storefront/draft/items/{item_id}"
REQUEST_ROUTE: Final = "POST /storefront/draft/requests"
#: The most items one draft holds.
MAX_DRAFT_ITEMS: Final = 50
#: A design request item's title: its text is its notes.
REQUEST_TITLE: Final = "Design request"


def _orders(state: AppState) -> OrdersComponent:
    orders: OrdersComponent = state.components.get(ORDERS)
    return orders


def item_not_found() -> ApiError:
    """404 ``item_not_found``: an unknown item and another visitor's item alike."""
    return problem(status.HTTP_404_NOT_FOUND, "item_not_found", "There is no such item.")


def draft_changed() -> ApiError:
    """409 ``draft_changed``: the draft moved under the write (submitted, or swept)."""
    return problem(
        status.HTTP_409_CONFLICT, "draft_changed", "Your order has changed. Load it again."
    )


def requests_disabled() -> ApiError:
    """404 ``requests_disabled``: "Ask for a design" is off."""
    return problem(
        status.HTTP_404_NOT_FOUND, "requests_disabled", "This shop does not take design requests."
    )


def draft_full() -> ApiError:
    """422 ``draft_full``: the draft holds `MAX_DRAFT_ITEMS` items already."""
    return problem(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "draft_full",
        f"Your order holds {MAX_DRAFT_ITEMS} items, the most it can. Remove one first.",
        limit=MAX_DRAFT_ITEMS,
    )


def request_file_key(sha256: str) -> str:
    """The raw copy of a request's file, which the visitor's presigned GET reads. The
    back office reads the same bytes as ``attachment-<sha256>`` (`keep_file`), a packed
    blob directory no browser can open."""
    return f"request-file-{sha256}"


def _label(kind: AttachmentKind, media_type: str) -> DraftFileKind:
    """What the visitor reads a file as: a photo, a video, a PDF, or ``other``."""
    if kind in ("image", "video"):
        return kind
    if kind == "document" and media_type == "application/pdf":
        return "pdf"
    return "other"


def _disposition(filename: str) -> str:
    """``Content-Disposition`` of a download named ``filename`` (RFC 6266): an ASCII
    name, and the name itself as RFC 8187 UTF-8."""
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


async def _url(content: ContentStore, attachment: Attachment) -> str | None:
    """A presigned GET of the file's raw copy, as a download of its sniffed media type;
    None when the index has none (a file not sent from the storefront, or lost)."""
    stat = await asyncio.to_thread(content.index.get, request_file_key(attachment.sha256))
    if stat is None or stat.ref.backend != content.name:
        return None
    backend = content.backend
    assert isinstance(backend, S3ContentBackend)
    return await backend.presign_get(
        stat.ref.backend_id,
        PUBLIC_MEDIA_TTL,
        media_type=attachment.media_type,
        disposition=_disposition(attachment.filename),
    )


def _inputs(item: OrderItem, offered: list[str] | None) -> dict[str, Any] | None:
    """The values of the parameters the listing offered when the item was added; None
    for an item not added from a listing, or not a model item."""
    if item.kind != "model" or item.reference is None or offered is None:
        return None
    params = item.reference.inputs.get("params")
    if not isinstance(params, dict):
        return {}
    return {name: params[name] for name in offered if name in params}


def _captions(listings: Sequence[Listing]) -> dict[tuple[str, str], dict[str, str]]:
    """Each published template commit's parameter labels, by (slug, commit): what a
    draft item's values are called on the page (`shown_label`).

    An item does not record which listing it came from, and several listings may share a
    slug and commit. So a listing's own label is used only when every listing at that
    pair that offers the parameter shows the same one; otherwise the item reads the
    template's caption without its extruder note, which never names another listing's
    choice. Like the listing page, this reads each row's live ``params``, so an edited
    label shows at once."""
    seen: dict[tuple[str, str], dict[str, set[str]]] = {}
    captions: dict[tuple[str, str], dict[str, str | None]] = {}
    for listing in listings:
        if listing.snapshot is None:
            continue
        pair = (listing.slug, listing.commit)
        bounds = listing.params
        names = seen.setdefault(pair, {})
        for p in listing.snapshot.schema_.parameters:
            captions.setdefault(pair, {})[p.name] = p.caption
            votes = names.setdefault(p.name, set())
            # Only a listing that offers the parameter has a say in its label.
            if p.name in bounds:
                votes.add(shown_label(p.name, bounds[p.name].label, p.caption))
    found: dict[tuple[str, str], dict[str, str]] = {}
    for pair, names in seen.items():
        labels = found.setdefault(pair, {})
        for name, shown in names.items():
            caption = captions[pair][name]
            label = next(iter(shown)) if len(shown) == 1 else shown_label(name, None, caption)
            if label != name:
                labels[name] = label
    return found


def _labels(
    item: OrderItem,
    inputs: dict[str, Any] | None,
    captions: dict[tuple[str, str], dict[str, str]],
) -> dict[str, str]:
    if not inputs or item.reference is None:
        return {}
    known = captions.get((item.reference.slug, item.reference.commit), {})
    return {name: known.get(name, name) for name in inputs}


async def _view(state: AppState, order: Order) -> DraftView:
    """The visitor's view of ``order``."""
    orders = _orders(state)

    def read() -> tuple[
        list[OrderItem],
        dict[str, list[str] | None],
        list[Attachment],
        list[Listing],
        list[PublicShippingOption],
        str,
    ]:
        offered = orders.store.storefront_offered(order.id)
        items = orders.store.items(order.id)
        rules = orders.lifecycle.rules()
        # The captions of the listing items' values: the listings at those items' slug
        # and commit only, in one query; none when the draft has no listing item.
        pairs = {
            (item.reference.slug, item.reference.commit)
            for item in items
            if offered.get(item.id) and item.reference is not None
        }
        return (
            items,
            offered,
            orders.store.attachments(order.id),
            orders.listings.at_commits(pairs) if pairs else [],
            [PublicShippingOption(**option.model_dump()) for option in rules.shipping_options],
            # A cart holds no amount, and the submit takes the shop's currency
            # (`Lifecycle.submit_order`), so the view shows that one.
            rules.shop_currency,
        )

    rows, offered, files, listings, shipping, currency = await asyncio.to_thread(read)
    sources = (
        await asyncio.to_thread(draft_sources, state, order.id)
        if any(item.kind == "request" for item in rows)
        else {}
    )
    captions = _captions(listings)
    by_subject: dict[str, list[Attachment]] = {}
    for attachment in files:
        by_subject.setdefault(attachment.subject, []).append(attachment)
    content = (
        public_content(state) if any(item_subject(item.id) in by_subject for item in rows) else None
    )
    items: list[DraftItem] = []
    for item in rows:
        attachments: list[DraftAttachment] = []
        for attachment in by_subject.get(item_subject(item.id), []):
            assert content is not None
            url = await _url(content, attachment)
            if url is not None:
                attachments.append(
                    DraftAttachment(
                        filename=attachment.filename,
                        kind=_label(attachment.kind, attachment.media_type),
                        url=url,
                    )
                )
        items.append(
            DraftItem(
                id=item.id,
                kind=item.kind,
                title=item.title,
                quantity=item.quantity,
                inputs=(inputs := _inputs(item, offered.get(item.id))),
                labels=_labels(item, inputs, captions),
                notes=item.notes if item.kind == "request" else None,
                attachments=attachments,
                own_model=sources.get(item.id, (False, []))[0],
                sources=sources.get(item.id, (False, []))[1],
            )
        )
    return DraftView(
        id=order.id,
        currency=currency,
        minor_exponent=minor_exponent(currency),
        items=items,
        shipping_options=shipping,
    )


def _stored(state: AppState, key: str, route: str, asked: Any) -> Replay | None:
    with state.projection.pool.connection() as conn:
        return stored(conn, "storefront", key, route, asked)


def request_sent(state: AppState, key: str, route: str | None = None) -> bool:
    """Whether ``key`` holds a committed write of ``route`` (a design request's by
    default): its retry is answered even once "Ask for a design" is off
    (`add_request`)."""
    with state.projection.pool.connection() as conn:
        return has_reply(conn, "storefront", key, route or REQUEST_ROUTE)


async def _answer(state: AppState, visitor_id: str, reply: Replay) -> DraftView:
    """The view of the order a write's reply names, while it is still the visitor's;
    409 ``draft_changed`` once it is not (swept)."""
    orders = _orders(state)
    order_id: str = reply.body["order_id"]

    def find() -> Order | None:
        visitor = VisitorStore(state.projection.pool).find(visitor_id)
        order = orders.store.order(order_id)
        if visitor is None or order is None or order.storefront_visitor_id != visitor.id:
            return None
        return order

    order = await asyncio.to_thread(find)
    if order is None:
        raise draft_changed()
    return await _view(state, order)


async def answer(state: AppState, visitor_id: str, reply: Replay) -> DraftView:
    """`_answer`, for the other draft writes (`model_requests.add_model_request`)."""
    return await _answer(state, visitor_id, reply)


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
    add: a model item with revision 1, titled as the listing. A retry of a committed add
    (the same key) answers its order as it is now and does nothing else. Refusals: 404
    ``listing_not_found``, 409 ``listing_changed``, 422 ``input_outside_listing`` (with
    ``name`` and ``reason``), 422 ``draft_full``, 409 ``draft_changed``."""
    orders = _orders(state)
    asked = {"visitor": token_sha256(visitor_id), **body.model_dump(mode="json")}
    reply = await asyncio.to_thread(_stored, state, key, ADD_ROUTE, asked)
    if reply is not None:
        return await _answer(state, visitor_id, reply)
    listing, resolved = await checked_choice(orders, body)
    visitor = await asyncio.to_thread(VisitorStore(state.projection.pool).ensure, visitor_id)
    by = visitor_actor(visitor)
    currency = await asyncio.to_thread(lambda: orders.price_book.policy()[0])
    entry = NewModelItem(
        resolved=resolved,
        quantity=body.quantity,
        title=listing.title,
        storefront_offered=tuple(listing.params),
    )

    def write() -> Replay:
        # Outside the ledger's transaction: two first adds at once make one draft.
        draft = orders.lifecycle.ensure_draft(visitor.id, by, currency=currency)
        changes: list[tuple[Order, list[OrderItem]]] = []

        def add(conn: Any) -> Replay:
            items = orders.lifecycle.add_items(draft.id, by, [entry], cart_of=visitor.id, conn=conn)
            # Under the order's lock, which the add took: a full draft takes nothing.
            if len(orders.store.items(draft.id, conn=conn)) > MAX_DRAFT_ITEMS:
                raise draft_full()
            order = orders.store.order(draft.id, conn=conn)
            assert order is not None
            changes.append((order, items))
            return Replay(200, {"order_id": draft.id, "item_id": items[0].id})

        with state.projection.pool.connection() as conn:
            done = idempotent_write(conn, "storefront", key, ADD_ROUTE, asked, lambda: add(conn))
        for order, items in changes:
            orders.lifecycle.told(order, items)
        return done

    try:
        reply = await asyncio.to_thread(write)
    except LifecycleError:
        raise draft_changed() from None
    return await _answer(state, visitor_id, reply)


async def remove_item(state: AppState, visitor_id: str, key: str, item_id: str) -> DraftView:
    """The item taken off the visitor's draft. 404 ``item_not_found`` unless the item is
    on the draft that visitor owns, checked under the order's lock; 409 ``draft_changed``
    for an item the lifecycle keeps. A retry of a committed delete (the same key) answers
    its order as it is now and does nothing else."""
    if ITEM_ID.fullmatch(item_id) is None:
        raise item_not_found()
    orders = _orders(state)
    asked = {"visitor": token_sha256(visitor_id), "item_id": item_id}
    reply = await asyncio.to_thread(_stored, state, key, REMOVE_ROUTE, asked)
    if reply is not None:
        return await _answer(state, visitor_id, reply)

    def write() -> Replay:
        visitor = VisitorStore(state.projection.pool).find(visitor_id)
        draft = None if visitor is None else orders.store.draft_of(visitor.id)
        if visitor is None or draft is None:
            raise item_not_found()
        by = visitor_actor(visitor)
        changes: list[Order] = []

        def remove(conn: Any) -> Replay:
            # The staged files the line held, for their watchers (#6133).
            freed.extend(uploads_of(state, item_id=item_id))
            orders.lifecycle.delete_item(draft.id, item_id, by, cart_of=visitor.id, conn=conn)
            order = orders.store.order(draft.id, conn=conn)
            assert order is not None
            changes.append(order)
            return Replay(200, {"order_id": draft.id, "item_id": item_id})

        with state.projection.pool.connection() as conn:
            done = idempotent_write(
                conn, "storefront", key, REMOVE_ROUTE, asked, lambda: remove(conn)
            )
        for order in changes:
            orders.lifecycle.told(order)
        return done

    freed: list[str] = []
    try:
        reply = await asyncio.to_thread(write)
    except LifecycleError as error:
        if error.not_found:
            raise item_not_found() from None
        raise draft_changed() from None
    await poke(state, freed, "unclaimed")
    return await _answer(state, visitor_id, reply)


@dataclass(frozen=True)
class _File:
    """A request's file as its bytes say: counted, hashed and sniffed."""

    source: BinaryIO
    filename: str
    kind: AttachmentKind
    media_type: str
    size: int
    sha256: str


def _checked_file(upload: UploadFile) -> _File:
    """``upload`` read once from its start, its bytes counted as they are read: 413
    ``file_too_large`` past `MAX_REQUEST_FILE_BYTES`, 415 ``file_type`` for a kind its
    bytes say is not one of `REQUEST_KINDS` (its name and its sent type count for
    nothing). Blocking: call it in a thread."""
    source = upload.file
    source.seek(0)
    digest = hashlib.sha256()
    head = b""
    size = 0
    while chunk := source.read(1 << 20):
        size += len(chunk)
        if size > MAX_REQUEST_FILE_BYTES:
            raise file_too_large()
        digest.update(chunk)
        if len(head) < SNIFF_BYTES:
            head += chunk[: SNIFF_BYTES - len(head)]
    # The edge escapes the name it forwards, as a browser does (`escape_filename`).
    filename = clean_filename(unescape_filename(upload.filename or "file"))
    kind, media_type = sniff(head, filename)
    if _label(kind, media_type) not in REQUEST_KINDS:
        raise problem(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "file_type",
            "Send photos, videos or PDFs.",
            filename=filename,
        )
    source.seek(0)
    return _File(source, filename, kind, media_type, size, digest.hexdigest())


async def add_request(
    state: AppState,
    visitor_id: str,
    key: str,
    *,
    text: str,
    files: Sequence[UploadFile],
) -> DraftView:
    """A design request added to the visitor's draft: a ``request`` item whose notes are
    ``text``, with each file on it as a ``reference`` attachment. Each file is kept as
    ``attachment-<sha256>`` (what the back office reads) and as `request_file_key` (what
    the visitor's presigned GET reads) before the rows; a refused row leaves unheld
    objects for the store's sweeps. A retry of a committed request (the same key)
    answers its order as it is now and does nothing else, even once requests are off.
    Refusals: 404 ``requests_disabled`` unless ``storefront_requests_enabled``, after
    the retry's lookup; 422 ``request_text``
    (`checked_text`: not 1 to `MAX_REQUEST_TEXT` characters of plain text); 422
    ``too_many_files`` past `MAX_REQUEST_FILES`; 413 ``file_too_large``; 415
    ``file_type``; 422 ``draft_full``; 409 ``draft_changed``; 503
    ``storefront_needs_s3``."""
    # Before any file is read or stored.
    text = checked_text(text)
    if len(files) > MAX_REQUEST_FILES:
        raise too_many_files()
    checked = [await asyncio.to_thread(_checked_file, upload) for upload in files]
    asked = {
        "visitor": token_sha256(visitor_id),
        "text": text,
        "files": [{"filename": f.filename, "sha256": f.sha256} for f in checked],
    }
    reply = await asyncio.to_thread(_stored, state, key, REQUEST_ROUTE, asked)
    if reply is not None:
        return await _answer(state, visitor_id, reply)
    snapshot = await asyncio.to_thread(state.settings_store.snapshot)
    if not snapshot.runtime.storefront_requests_enabled:
        raise requests_disabled()
    content = public_content(state)
    orders = _orders(state)
    kept: list[str] = []
    for file in checked:
        kept.append(await keep_file(state.store.blobs, file.source, file.sha256))
        await content.put(
            "media",
            file.source,
            name=file.filename,
            scope=BlobScope(),
            key=request_file_key(file.sha256),
        )
    visitor = await asyncio.to_thread(VisitorStore(state.projection.pool).ensure, visitor_id)
    by = visitor_actor(visitor)
    currency = await asyncio.to_thread(lambda: orders.price_book.policy()[0])
    entry = NewRequestItem(title=REQUEST_TITLE, quantity=1, notes=text)

    def write() -> Replay:
        # Outside the ledger's transaction: two first adds at once make one draft.
        draft = orders.lifecycle.ensure_draft(visitor.id, by, currency=currency)
        changes: list[tuple[Order, list[OrderItem]]] = []

        def add(conn: Any) -> Replay:
            [item] = orders.lifecycle.add_items(
                draft.id, by, [entry], cart_of=visitor.id, conn=conn
            )
            if len(orders.store.items(draft.id, conn=conn)) > MAX_DRAFT_ITEMS:
                raise draft_full()
            for file, blob in zip(checked, kept, strict=True):
                attachment = orders.lifecycle.add_attachment(
                    draft.id,
                    item.id,
                    by,
                    attachment_id=str(uuid.uuid4()),
                    kind=file.kind,
                    role="reference",
                    filename=file.filename,
                    media_type=file.media_type,
                    size_bytes=file.size,
                    sha256=file.sha256,
                    blob_key=blob,
                    caption="",
                    cart_of=visitor.id,
                    conn=conn,
                )
                orders.store.hold_with_attachment(
                    conn, request_file_key(file.sha256), attachment.id
                )
            order = orders.store.order(draft.id, conn=conn)
            assert order is not None
            changes.append((order, [item]))
            return Replay(200, {"order_id": draft.id, "item_id": item.id})

        with state.projection.pool.connection() as conn:
            done = idempotent_write(
                conn, "storefront", key, REQUEST_ROUTE, asked, lambda: add(conn)
            )
        for order, items in changes:
            orders.lifecycle.told(order, items)
        return done

    try:
        reply = await asyncio.to_thread(write)
    except LifecycleError:
        raise draft_changed() from None
    return await _answer(state, visitor_id, reply)
