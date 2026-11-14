"""A storefront customer's own models (#6133, spec 2026-11-13 §5, §6).

The files never pass through ScadBuddy: `presign_uploads` writes one `storefront_uploads`
row per file and answers a presigned PUT for its staging key (`store/s3.py`
`presign_put`: the exact size and the type are signed in, `UPLOAD_URL_TTL`). The browser
PUTs each file straight to the bucket. `add_model_request` then claims the uploads the
body names: each must be the visitor's and unclaimed, and is frozen (`_claimable`: a
server-side copy to a key no presigned URL names, whose ``HEAD`` must answer exactly its
declared size; the check and the import read only the copy). No byte of a file is read
here (#5295: the intake worker checks it, `order_item_sources.check_status`). The same
transaction adds a ``request`` item to the visitor's draft, its `order_item_model_requests`
row (the colours asked for) and one `order_item_sources` row per file and link, and claims
the uploads.

A link is stored as text only (`orders/customer_links.py`): this side never fetches it.

Each upload has a watcher, a ``StagedFile`` workflow (`staged_files.py`), started before
the presign answers any URL; it owns the file's life and its clean-up (`UNCLAIMED_GRACE`,
`STAGING_TTL`). `staged_files.sweep_staged` is only the backstop (`ORPHAN_AGE`).

Everything needs the S3 store (`listings.py` `public_content`): any other is 503
``storefront_needs_s3``, as the public site's media is.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from fastapi import status
from psycopg.types.json import Jsonb

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, forget, idempotent_write, stored
from scadbuddy.orders.attachments import clean_filename
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.customer_links import LinkInvalidError, checked_link, link_type
from scadbuddy.orders.lifecycle import LifecycleError, NewRequestItem
from scadbuddy.orders.models import Order, OrderItem
from scadbuddy.orders.storefront.listings import problem, public_content
from scadbuddy.orders.storefront.visitors import VisitorStore, token_sha256, visitor_actor
from scadbuddy.orders.storefront.wire import (
    DraftSource,
    ModelRequestBody,
    UploadSlot,
    UploadSlots,
    UploadSlotsBody,
)
from scadbuddy.store.s3 import STAGED_PUT_HEADERS, S3ContentBackend, staging_key
from scadbuddy.storefront.uploads import checked_text
from scadbuddy.storefront.wire import (
    MAX_MODEL_REQUEST_BYTES,
    MAX_PRINT_LINKS,
    MAX_PURCHASE_LINKS,
    UPLOAD_ID,
)

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

logger = logging.getLogger(__name__)

#: The ledger's names of the routes (`idempotent_write`).
UPLOADS_ROUTE: Final = "POST /storefront/uploads"
MODELS_ROUTE: Final = "POST /storefront/draft/models"
#: How long a presigned PUT may start (S3 checks the expiry when a request starts, so a
#: slow upload that started in time completes).
UPLOAD_URL_TTL: Final = timedelta(minutes=15)
#: A slot never claimed is cleaned up this long past its URL's expiry.
UNCLAIMED_GRACE: Final = timedelta(hours=1)
#: A claimed file of a cart not submitted is cleaned up this long after its claim: an
#: abandoned cart must not keep up to `MAX_MODEL_REQUEST_BYTES` staged for the cart TTL. A
#: submitted order's file has no deadline.
STAGING_TTL: Final = timedelta(hours=72)
#: The backstop deletes a staging object no row names, and restarts the watcher of a row
#: whose watcher is not running, past this age.
ORPHAN_AGE: Final = timedelta(days=1)
#: A file source still ``checking`` this long after its claim has its check started again
#: by the backstop when the check is not running (#6133 PR 2).
CHECK_STALE: Final = timedelta(hours=1)
#: The rows one backstop query reads; a pass reads at most `SWEEP_PASS_BATCHES` of them.
SWEEP_BATCH: Final = 200
#: The batches one backstop pass walks (one Temporal ``describe`` per row), so the drafts
#: sweep stays inside its timeout; the next pass goes on from where this one stopped.
SWEEP_PASS_BATCHES: Final = 5
#: A request item's title; its note is its notes.
MODEL_REQUEST_TITLE: Final = "Your models"
#: The check reason of a staged file cleaned up from a cart not submitted.
EXPIRED: Final = "This file expired before the order was sent; upload it again."
_SUFFIXES: Final = {"model/stl": ".stl", "model/3mf": ".3mf"}


def _orders(state: AppState) -> OrdersComponent:
    orders: OrdersComponent = state.components.get(ORDERS)
    return orders


def _backend(state: AppState) -> S3ContentBackend:
    """The S3 backend (503 ``storefront_needs_s3`` on any other)."""
    backend = public_content(state).backend
    assert isinstance(backend, S3ContentBackend)
    return backend


def _unprocessable(slug: str, detail: str, **extensions: Any) -> ApiError:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, slug, detail, **extensions)


def _stored(state: AppState, key: str, route: str, asked: Any) -> Replay | None:
    with state.projection.pool.connection() as conn:
        return stored(conn, "storefront", key, route, asked)


# -- presign ----------------------------------------------------------------------------


def _checked_names(body: UploadSlotsBody) -> list[str]:
    """Each file's name to keep, ending as its type says; 422 ``upload_name`` for a name
    whose extension is not its type's, 422 ``upload_total`` past
    `MAX_MODEL_REQUEST_BYTES` in all."""
    names = []
    for index, ask in enumerate(body.files):
        name = clean_filename(ask.filename)
        if not name.lower().endswith(_SUFFIXES[ask.media_type]):
            raise _unprocessable(
                "upload_name",
                "Each file must be an .stl or a .3mf file, named as its type.",
                index=index,
            )
        names.append(name)
    if sum(ask.size for ask in body.files) > MAX_MODEL_REQUEST_BYTES:
        raise _unprocessable(
            "upload_total",
            f"Send at most {MAX_MODEL_REQUEST_BYTES // (1024 * 1024 * 1024)} GiB of files at once.",
        )
    return names


@dataclass(frozen=True)
class _Slot:
    id: str
    object_key: str
    filename: str
    size: int
    media_type: str
    #: The ETag of the claim's frozen copy (`_claimable`); empty before the claim.
    etag: str = ""


async def _signed(backend: S3ContentBackend, slots: Sequence[_Slot]) -> UploadSlots:
    expires_at = datetime.now(UTC) + UPLOAD_URL_TTL
    answered = []
    for slot in slots:
        url = await backend.presign_put(
            slot.object_key, size=slot.size, media_type=slot.media_type, expires=UPLOAD_URL_TTL
        )
        answered.append(
            UploadSlot(
                id=slot.id,
                filename=slot.filename,
                url=url,
                headers={"Content-Type": slot.media_type, **STAGED_PUT_HEADERS},
                expires_at=expires_at,
            )
        )
    return UploadSlots(uploads=answered)


def _slots(state: AppState, ids: Sequence[str], *, renew: bool) -> list[_Slot]:
    """The rows of ``ids`` still unclaimed, in order; ``renew`` moves their expiry to a new
    URL's."""
    with state.projection.pool.connection() as conn:
        if renew:
            conn.execute(
                "UPDATE storefront_uploads SET expires_at = %s"
                " WHERE id = ANY(%s) AND claimed_at IS NULL",
                (datetime.now(UTC) + UPLOAD_URL_TTL, list(ids)),
            )
        rows = conn.execute(
            "SELECT id, object_key, filename, size, media_type FROM storefront_uploads"
            " WHERE id = ANY(%s) AND claimed_at IS NULL",
            (list(ids),),
        ).fetchall()
    by_id = {row["id"]: row for row in rows}
    return [
        _Slot(r["id"], r["object_key"], r["filename"], r["size"], r["media_type"])
        for i in ids
        if (r := by_id.get(i)) is not None
    ]


async def presign_uploads(
    state: AppState, visitor_id: str, key: str, body: UploadSlotsBody
) -> UploadSlots:
    """One upload slot per file: a row and a presigned PUT of its staging key. A retry of
    the same key answers the same slots with new URLs (those still unclaimed). Each slot's
    watcher is started before any URL is answered. Refusals: 503 ``storefront_needs_s3``,
    ``uploads_unavailable`` (no watcher could be started; the new rows and the key's reply
    are dropped, so a retry starts afresh); 422
    ``upload_name``, ``upload_total``."""
    from scadbuddy.orders.storefront.staged_files import unavailable, watch

    backend = _backend(state)
    names = _checked_names(body)
    asked = {"visitor": token_sha256(visitor_id), **body.model_dump(mode="json")}
    reply = await asyncio.to_thread(_stored, state, key, UPLOADS_ROUTE, asked)
    if reply is not None:
        again = await asyncio.to_thread(_slots, state, reply.body["ids"], renew=True)
        await watch(state, [slot.id for slot in again])
        return await _signed(backend, again)
    visitor = await asyncio.to_thread(VisitorStore(state.projection.pool).ensure, visitor_id)
    prefix = backend.settings.prefix
    made = [
        _Slot(
            id=(slot_id := uuid.uuid4().hex),
            object_key=staging_key(prefix, slot_id),
            filename=name,
            size=ask.size,
            media_type=ask.media_type,
        )
        for ask, name in zip(body.files, names, strict=True)
    ]

    def write() -> list[str]:
        expires_at = datetime.now(UTC) + UPLOAD_URL_TTL

        def insert(conn: Any) -> Replay:
            for slot in made:
                conn.execute(
                    "INSERT INTO storefront_uploads"
                    " (id, visitor_id, object_key, filename, size, media_type, expires_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        slot.id,
                        visitor.id,
                        slot.object_key,
                        slot.filename,
                        slot.size,
                        slot.media_type,
                        expires_at,
                    ),
                )
            return Replay(200, {"ids": [slot.id for slot in made]})

        with state.projection.pool.connection() as conn:
            done = idempotent_write(
                conn, "storefront", key, UPLOADS_ROUTE, asked, lambda: insert(conn)
            )
        ids: list[str] = done.body["ids"]
        return ids

    ids = await asyncio.to_thread(write)
    if ids != [slot.id for slot in made]:
        # A first send of the same key committed meanwhile: answer its slots.
        again = await asyncio.to_thread(_slots, state, ids, renew=False)
        if len(again) != len(ids):
            # That send's watchers failed and its rows were dropped: no slot to answer.
            raise unavailable()
        await watch(state, [slot.id for slot in again])
        return await _signed(backend, again)
    try:
        await watch(state, ids)
    except ApiError:
        # No URL without a watcher: the rows and the key's reply go together, so a retry
        # of the key makes its slots afresh.
        await asyncio.to_thread(_drop, state, key, ids)
        raise
    return await _signed(backend, made)


def _drop(state: AppState, key: str, ids: Sequence[str]) -> None:
    with state.projection.pool.connection() as conn, conn.transaction():
        conn.execute(
            "DELETE FROM storefront_uploads WHERE id = ANY(%s) AND claimed_at IS NULL", (list(ids),)
        )
        forget(conn, "storefront", key, UPLOADS_ROUTE)


async def upload_origin(state: AppState) -> str:
    """The origin the browser PUTs to, taken from a presigned URL itself, so the edge's page
    CSP names exactly what `presign_uploads` answers (503 ``storefront_needs_s3`` on a store
    that is not S3)."""
    backend = _backend(state)
    probe = await backend.presign_put(
        staging_key(backend.settings.prefix, "0" * 32),
        size=1,
        media_type="model/stl",
        expires=timedelta(minutes=1),
    )
    parts = urlsplit(probe)
    return f"{parts.scheme}://{parts.netloc}"


# -- the request ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Link:
    url: str
    purpose: str
    link_type: str
    canonical: str | None
    note: str


def _checked_links(body: ModelRequestBody) -> list[_Link]:
    """Each link as stored, typed by its handler; 422 ``link_invalid`` (with ``index``),
    ``too_many_links``."""
    links = []
    for index, ask in enumerate(body.links):
        try:
            url = checked_link(ask.url)
        except LinkInvalidError as error:
            raise _unprocessable("link_invalid", str(error), index=index) from None
        found, canonical = link_type(url, ask.purpose)
        links.append(_Link(url, ask.purpose, found, canonical, ask.note.strip()))
    if sum(link.purpose == "print" for link in links) > MAX_PRINT_LINKS:
        raise _unprocessable("too_many_links", f"Send at most {MAX_PRINT_LINKS} model links.")
    if sum(link.purpose == "purchase" for link in links) > MAX_PURCHASE_LINKS:
        raise _unprocessable("too_many_links", f"Send at most {MAX_PURCHASE_LINKS} things to buy.")
    return links


def _checked_note(note: str) -> str:
    """The note as plain text (`checked_text`), or empty."""
    return checked_text(note) if note.strip() else ""


def _checked_colours(orders: OrdersComponent, body: ModelRequestBody) -> dict[str, Any]:
    """The colours asked for; 422 ``colour_unknown`` for a filament the price book does not
    offer, ``colour_missing`` for no choice at all."""
    if body.colours.shop_choice:
        return {"shop_choice": True}
    if not body.colours.filaments:
        raise _unprocessable("colour_missing", "Pick a colour, or let the shop choose.")
    offered = {f.id for f in orders.store.filaments()}
    for index, filament in enumerate(body.colours.filaments):
        if filament not in offered:
            raise _unprocessable(
                "colour_unknown", "The shop does not offer that colour.", index=index
            )
    return {"filaments": list(dict.fromkeys(body.colours.filaments))}


def _upload_missing(index: int) -> ApiError:
    return _unprocessable("upload_missing", "A file did not arrive. Upload it again.", index=index)


async def _claimable(
    state: AppState, backend: S3ContentBackend, visitor_id: str, ids: Sequence[str]
) -> list[_Slot]:
    """The visitor's unclaimed slots of ``ids``, each frozen: copied server side to its
    frozen key (`staging_key` ``frozen``), which no presigned URL names, so the customer's
    PUT URL (valid up to `UPLOAD_URL_TTL`) cannot change the file after the claim. The
    check and the import read only that copy. 422 ``upload_missing`` for an unknown,
    foreign, claimed or absent one, ``upload_size`` for one whose copy is not its declared
    size (the copy is deleted). No byte passes through this process."""
    for index, slot_id in enumerate(ids):
        if UPLOAD_ID.fullmatch(slot_id) is None or ids.index(slot_id) != index:
            raise _upload_missing(index)

    def read() -> list[_Slot]:
        visitor = VisitorStore(state.projection.pool).find(visitor_id)
        if visitor is None:
            return []
        with state.projection.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, object_key, filename, size, media_type FROM storefront_uploads"
                " WHERE id = ANY(%s) AND visitor_id = %s AND claimed_at IS NULL",
                (list(ids), visitor.id),
            ).fetchall()
        return [
            _Slot(r["id"], r["object_key"], r["filename"], r["size"], r["media_type"]) for r in rows
        ]

    by_id = {slot.id: slot for slot in await asyncio.to_thread(read)}
    slots = []
    for index, slot_id in enumerate(ids):
        slot = by_id.get(slot_id)
        if slot is None:
            raise _upload_missing(index)
        frozen_key = staging_key(backend.settings.prefix, slot.id, frozen=True)
        frozen = await backend.freeze_staged(slot.object_key, frozen_key)
        if frozen is None:
            raise _upload_missing(index)
        size, etag = frozen
        if size != slot.size:
            await backend.remove_staged(frozen_key)
            raise _unprocessable(
                "upload_size", "A file arrived incomplete. Upload it again.", index=index
            )
        slots.append(replace(slot, etag=etag))
    return slots


async def _drop_originals(backend: S3ContentBackend, slots: Sequence[_Slot]) -> None:
    """The claimed slots' writable originals deleted, now that their frozen copies are
    claimed. Best effort: a failure is logged, and the file's watcher deletes every staging
    key of the slot when it ends (`staged_files.step`)."""
    for slot in slots:
        try:
            await backend.remove_staged(slot.object_key)
        except Exception:
            logger.warning("could not delete the claimed original %s", slot.id, exc_info=True)


def _upload_taken() -> ApiError:
    return problem(
        status.HTTP_409_CONFLICT, "upload_taken", "A file was already sent. Load your order again."
    )


async def add_model_request(
    state: AppState, visitor_id: str, key: str, body: ModelRequestBody
) -> Replay:
    """A "print my own model" request added to the visitor's draft: a ``request`` item
    titled `MODEL_REQUEST_TITLE`, its colours, and one source per upload and link. A retry
    of a committed request (the same key) answers its reply and does nothing else. The
    caller answers the draft view. Refusals: 422 ``model_request_empty`` (no file and no
    model link), ``link_invalid``, ``too_many_links``, ``request_text``,
    ``colour_missing``, ``colour_unknown``, ``upload_missing``, ``upload_size``,
    ``draft_full``; 409 ``upload_taken``, ``draft_changed``; 503
    ``storefront_needs_s3``. After the commit each claimed file's watcher is told."""
    from scadbuddy.orders.storefront.drafts import MAX_DRAFT_ITEMS, draft_changed, draft_full
    from scadbuddy.orders.storefront.intake import start_checks
    from scadbuddy.orders.storefront.staged_files import poke

    orders = _orders(state)
    links = _checked_links(body)
    note = _checked_note(body.note)
    if not body.uploads and not any(link.purpose == "print" for link in links):
        raise _unprocessable(
            "model_request_empty", "Add a model file or a link to a model to print."
        )
    colours = await asyncio.to_thread(_checked_colours, orders, body)
    asked = {"visitor": token_sha256(visitor_id), **body.model_dump(mode="json")}
    reply = await asyncio.to_thread(_stored, state, key, MODELS_ROUTE, asked)
    if reply is not None:
        return reply
    backend = _backend(state)
    slots = await _claimable(state, backend, visitor_id, body.uploads)
    visitor = await asyncio.to_thread(VisitorStore(state.projection.pool).ensure, visitor_id)
    by = visitor_actor(visitor)
    currency = await asyncio.to_thread(lambda: orders.price_book.policy()[0])
    entry = NewRequestItem(title=MODEL_REQUEST_TITLE, quantity=body.quantity, notes=note)

    #: The file sources this call made, whose checks start after the commit.
    checks: list[str] = []

    def write() -> Replay:
        # Outside the ledger's transaction: two first adds at once make one draft.
        draft = orders.lifecycle.ensure_draft(visitor.id, by, currency=currency)
        changes: list[tuple[Order, list[OrderItem]]] = []
        checks.clear()

        def add(conn: Any) -> Replay:
            [item] = orders.lifecycle.add_items(
                draft.id, by, [entry], cart_of=visitor.id, conn=conn
            )
            if len(orders.store.items(draft.id, conn=conn)) > MAX_DRAFT_ITEMS:
                raise draft_full()
            if slots:
                claimed = conn.execute(
                    "UPDATE storefront_uploads AS u SET claimed_at = now(), frozen_etag = v.etag"
                    " FROM unnest(%s::text[], %s::text[]) AS v(id, etag)"
                    " WHERE u.id = v.id AND u.visitor_id = %s AND u.claimed_at IS NULL",
                    ([slot.id for slot in slots], [slot.etag for slot in slots], visitor.id),
                ).rowcount
                if claimed != len(slots):
                    raise _upload_taken()
            conn.execute(
                "INSERT INTO order_item_model_requests (item_id, colour_preferences)"
                " VALUES (%s, %s)",
                (item.id, Jsonb(colours)),
            )
            position = 0
            for slot in slots:
                made = conn.execute(
                    "INSERT INTO order_item_sources (order_id, item_id, position, kind,"
                    " purpose, upload_id, filename, size, media_type, check_status, status)"
                    " VALUES (%s, %s, %s, 'file', 'print', %s, %s, %s, %s, 'checking', 'open')"
                    " RETURNING id",
                    (
                        draft.id,
                        item.id,
                        position,
                        slot.id,
                        slot.filename,
                        slot.size,
                        slot.media_type,
                    ),
                ).fetchone()
                assert made is not None
                checks.append(made["id"])
                position += 1
            for link in links:
                conn.execute(
                    "INSERT INTO order_item_sources (order_id, item_id, position, kind,"
                    " purpose, url, link_type, canonical, note, status)"
                    " VALUES (%s, %s, %s, 'link', %s, %s, %s, %s, %s, %s)",
                    (
                        draft.id,
                        item.id,
                        position,
                        link.purpose,
                        link.url,
                        link.link_type,
                        link.canonical,
                        link.note,
                        "pending" if link.purpose == "purchase" else _print_status(link),
                    ),
                )
                position += 1
            order = orders.store.order(draft.id, conn=conn)
            assert order is not None
            changes.append((order, [item]))
            return Replay(200, {"order_id": draft.id, "item_id": item.id})

        with state.projection.pool.connection() as conn:
            done = idempotent_write(conn, "storefront", key, MODELS_ROUTE, asked, lambda: add(conn))
        for order, items in changes:
            orders.lifecycle.told(order, items)
        return done

    try:
        done = await asyncio.to_thread(write)
    except LifecycleError:
        raise draft_changed() from None
    await _drop_originals(backend, slots)
    await poke(state, [slot.id for slot in slots], "claimed")
    await start_checks(state, checks)
    return done


def _print_status(link: _Link) -> str:
    """A print link's first status: ``open`` for a type the flow imports (MakerWorld,
    #6128), ``manual`` for every other, which the shop handles by hand."""
    return "open" if link.link_type == "makerworld" else "manual"


# -- the draft's view -------------------------------------------------------------------


def draft_sources(state: AppState, order_id: str) -> dict[str, tuple[bool, list[DraftSource]]]:
    """Each request item's sources, by item id, with whether it is an own-model request."""
    with state.projection.pool.connection() as conn:
        own = {
            row["item_id"]
            for row in conn.execute(
                "SELECT m.item_id FROM order_item_model_requests m"
                " JOIN order_items i ON i.id = m.item_id WHERE i.order_id = %s",
                (order_id,),
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT id, item_id, kind, purpose, filename, size, url, note, check_status,"
            " check_reason, status FROM order_item_sources WHERE order_id = %s"
            " ORDER BY item_id, position",
            (order_id,),
        ).fetchall()
    found: dict[str, tuple[bool, list[DraftSource]]] = {item_id: (True, []) for item_id in own}
    for row in rows:
        item_id = row.pop("item_id")
        found.setdefault(item_id, (item_id in own, []))[1].append(DraftSource(**row))
    return found
