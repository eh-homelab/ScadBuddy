"""The render worker's CRUD routes (#5298): the store settings it renders with (inventory
R2), for `crud/render.py` `RemoteRenderSettingsSource`, its jobs' rows (R3-R10), for
`RemoteJobProjection`, and its jobs' blob refs and the piece geometry (R11, R12), for
`RemoteBlobRefs` and `RemoteGeometryIndex`, the blob index (R13-R16), for
`RemoteBlobIndex`, the Bambuddy store's folders (R17, R18), for `RemoteStoreFolders`, the
uploads' ``assets`` rows (R19-R21), for `RemoteAssetRows`, and the library leases, pin
holds and install slots (R22-R24), for `RemoteCheckoutLeases` and `RemoteInstallSlots`.

The settings answer holds secrets in plaintext, released only to ``crud.render`` (plan D8): the
render key and the S3 keys. While no render key is saved it holds no Bambuddy key: the
full key is never released to the pod that runs template Python (#5298 final review). The
CRUD router answers ``Cache-Control: no-store``, and each answer is logged with the
secrets it released and the credential it released them to, never their values.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Callable
from contextlib import aclosing
from datetime import datetime
from typing import Annotated, Any

from fastapi import Query, Request, status
from psycopg import Connection
from psycopg.errors import DeadlockDetected, LockNotAvailable
from psycopg.rows import DictRow
from pydantic import BaseModel, Field, ValidationError

from scadbuddy.api.deps import AppState, StateDep
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Service
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.crud.idempotency import (
    IdempotencyKey,
    MaybeIdempotencyKey,
    Replay,
    idempotent_write,
    required_key,
)
from scadbuddy.library.assets import (
    ASSET_ID_PATTERN,
    ASSET_ID_RE,
    LAST_USED_MAX,
    AssetMeta,
    AssetNotFoundError,
    AssetRejectedError,
)
from scadbuddy.library.libraries import (
    COMMIT_PATTERN,
    LEASE_TTL,
    LEASE_TTL_MAX,
    NAME_PATTERN,
    CheckoutLeases,
    InstallSlotRows,
    LeaseTokenTakenError,
)
from scadbuddy.library.settings_store import RenderStoreSettings, load_render_store_settings
from scadbuddy.render.inputs import legacy_inputs
from scadbuddy.render.job_models import Job, JobNotFoundError, QueueFullError, StepInfo
from scadbuddy.render.projection import PROJECT_POOL_TIMEOUT
from scadbuddy.store.assets import asset_key
from scadbuddy.store.bambuddy import FolderFinder, FolderRole, Inbox, instance_key, render_target
from scadbuddy.store.content_models import (
    BlobCorruptError,
    BlobMissingError,
    BlobStat,
    ReuseLostError,
)
from scadbuddy.store.content_models import BlobRef as IndexedRef
from scadbuddy.store.geometry import GeometryIndex
from scadbuddy.store.index import BlobIndex

logger = logging.getLogger(__name__)

router = crud_router("render")
#: A full queue's refusal of an accept: `crud/render.py` `QUEUE_FULL`.
QUEUE_FULL = "queue-full"
#: A ref for any holder but a render job: `RemoteBlobRefs`.
HOLDER_KIND = "holder-kind"
#: The finish's name in the idempotency ledger.
FINISH_ROUTE = "POST /jobs/{job_id}/finish"
#: An index write whose reused object was freed first: `crud/render.py` `REUSE_LOST`.
REUSE_LOST = "reuse-lost"
#: An index write Postgres picked as a deadlock's victim on every try:
#: `crud/render.py` `DEADLOCK`.
DEADLOCK = "deadlock"
#: A blob-index write (put, swap, mark, delete, stale delete) of a key that is not a
#: render job's or of a row of another kind, a hold of a key no render job holds, or a
#: read of a key or kind the render worker does not read: a 403.
API_OWNED_KEY = "api-owned-key"
#: The index keys a render job writes, every one of kind ``piece`` (`store/cache.py`
#: `CachedBlobStore._publish`): a piece's (`workflows/models.py` `piece_key`, 64 hex), a
#: template call's emitted files (`workflows/template_process.py` `template_out_key`) and
#: an output's files (`workflows/outputs.py` `output_key`, with the 32-hex job id
#: `render_accept` mints). An allowlist, not a denylist (#5298 final review): `store_blobs`
#: holds every service's keys (a print library file's, an agent workspace's, a customer's
#: files, a listing's media, a slicer's input and output, an upload's copy, a font's, a
#: template revision's snapshot), and template ``pipeline/`` Python, which runs in the
#: render pod, can reach a render credential. Every other key is the API's: a copy the
#: worker cannot read, the API drops (an upload's, ``POST …/assets/{id}/drop-copy``) or
#: forgets (a font's or snapshot's, ``POST …/blob-index/{key}/forget-copy``) for it.
RENDER_KEY = re.compile(r"[0-9a-f]{64}|act-[0-9a-f]{40}|output-[0-9a-f]{32}-[0-9]+")
#: The one kind a render job writes.
RENDER_KIND = "piece"
#: A library file's object that Arrange lays out (`bambuddy/library_objects.py`
#: `piece_key`, stored by the API).
LIBRARY_PIECE_KEY = re.compile(r"lib3-[0-9a-f]{48}-[0-9]+")
#: The keys a render job holds (`RemoteBlobRefs`: `render_finish`'s blob keys,
#: `TemplateActivities._hold`, an output's files), fetches and touches (`CachedBlobStore`):
#: its own, and a library file's object.
HELD_KEY = re.compile(rf"{RENDER_KEY.pattern}|{LIBRARY_PIECE_KEY.pattern}")
#: Beside those, the one key the render worker touches: the snapshot it renders from
#: (`SnapshotStore`), so the sweep keeps it.
TOUCHED_PREFIXES = ("src-",)
#: Beside the keys it holds, the render worker reads a template revision's snapshot
#: (`SnapshotStore`), a font family's copy (`FontMirror`) and an upload's (`RemoteAssets`).
READ_PREFIXES = ("src-", "font-", "asset-")
#: The kinds it lists (`FontMirror.sync`).
READ_KINDS = frozenset({"piece", "snapshot", "font", "asset"})
#: The one kind it finds by SHA-256: `ContentStore.put` of a piece, before it stores one.
FOUND_KINDS = frozenset({RENDER_KIND})
#: The owned keys a worker may have the API forget (the row only): `FontMirror.sync` and
#: `SnapshotStore.materialize` forget a copy they cannot read, so the API stores it again.
#: A library file's object (`LIBRARY_PIECE_KEY`) is forgotten only when the API cannot
#: read it either (`index_forget_copy`).
FORGETTABLE_PREFIXES = ("font-", "src-")
#: A forget of a library file's object that the API reads whole, could not read in
#: time, read a short while ago, or that was stored again meanwhile: a 409, the row kept.
COPY_INTACT = "copy-intact"
#: How long the API reads a library file's object before it gives up (a 409).
FORGET_READ_TIMEOUT = 60.0
#: How long after a full read of a key the API reads it again for a forget.
FORGET_CHECK_COOLDOWN = 600.0
#: How many keys' last reads are kept (`_recently_checked`).
FORGET_CHECKS_KEPT = 4096
#: Each key's last full read for a forget, by `time.monotonic`, in this process.
_LAST_CHECKED: dict[str, float] = {}
#: An adopt's own refusals, `crud/render.py` `RemoteAssetRows.adopt`: no copy of the upload
#: in the blob store (a 404), or a copy of another kind or size (a 422).
NO_COPY = "no-copy"
COPY_MISMATCH = "copy-mismatch"
#: A folder find for a Bambuddy or inbox that is not the API's render target:
#: `crud/render.py` `RemoteStoreFolders`.
TARGET_MOVED = "target-moved"
#: A folder find Bambuddy did not answer (unreachable, a timeout, a 5xx): a 424, which
#: the CRUD client does not retry, so the call fails at once as the SQL find did and
#: the activity's own retry takes over. `crud/render.py` `BAMBUDDY_UNAVAILABLE`.
BAMBUDDY_UNAVAILABLE = "bambuddy-unavailable"
#: A folder find whose lock the API waited `FOLDER_LOCK_SECONDS` for in vain: a 423, which
#: the client does not retry, so the call fails after one wait as the SQL find did.
#: `crud/render.py` `FOLDER_LOCKED`.
FOLDER_LOCKED = "folder-locked"
#: Seconds a lease or pin hold waits for a removal's `REMOVAL_LOCK` before the route
#: answers a 423 ``removal-running``, which the worker sends again: the take keeps
#: waiting as long as the removal runs, as the SQL take did, and no request holds a
#: thread here past this: a waiting take holds an API pool connection and an executor
#: thread for at most 2 s, at the cost of more 423 rounds while a removal runs.
#: `crud/render.py` `REMOVAL_RUNNING`.
LEASE_LOCK_WAIT = 2.0
REMOVAL_RUNNING = "removal-running"
#: A lease taken under a token that holds another lease: `crud/render.py` `TOKEN_TAKEN`.
TOKEN_TAKEN = "token-taken"
#: A renew of a lease or a hold with no row: `crud/render.py` `NO_LEASE`, `NO_HOLD`.
NO_LEASE = "no-lease"
NO_HOLD = "no-hold"
#: A renew or release of an install slot another token holds: `crud/render.py`
#: `NOT_HOLDER`.
NOT_HOLDER = "not-holder"
#: The most checkouts one lease names: a render's ``OPENSCADPATH`` holds one per pinned
#: library.
LEASE_CHECKOUTS_MAX = 256


def released(current: RenderStoreSettings) -> list[str]:
    """The names of the secrets in ``current``, for the log."""
    names = []
    if current.api_key is not None:
        names.append("bambuddy_render_api_key")
    if current.s3 is not None:
        names += ["store_s3_access_key_id", "store_s3_secret_access_key"]
    return names


def withheld(current: RenderStoreSettings) -> RenderStoreSettings:
    """``current`` without the full Bambuddy key it falls back to while no render key is
    saved. ``key_is_fallback`` stays true, so the worker's Bambuddy store can say that it
    needs ``bambuddy_render_api_key`` (`store/bambuddy.py` `render_target`)."""
    if not current.key_is_fallback:
        return current
    return current.model_copy(update={"api_key": None})


@router.get("/settings", name="crud_render_settings")
async def render_settings(request: Request, state: StateDep) -> RenderStoreSettings:
    loaded = await asyncio.to_thread(
        load_render_store_settings, state.settings_store.pool, state.settings
    )
    current = withheld(loaded)
    # The guard admitted only a render `Service`.
    principal = caller_of(request.scope)
    assert isinstance(principal, Service)
    if current is not loaded:
        logger.warning(
            "withheld the full Bambuddy key from service credential %s: no"
            " bambuddy_render_api_key is saved, so the render worker gets no Bambuddy key",
            principal.credential_id,
        )
    logger.info(
        "released the render settings to service credential %s, secrets: %s",
        principal.credential_id,
        ", ".join(released(current)) or "none",
    )
    return current


# -- the jobs' rows (inventory R3-R10) -------------------------------------------
#
# Each route is the API's own `JobProjection` method: a write that changes a row's state
# commits its activity row and its ``job.*`` event in the same transaction, the event
# log's lock taken first (`core/pg_events.py`, "Lock order"), as the worker's SQL did. So
# an older worker still writing over SQL during a roll takes the locks in the same order.
# Every write but the finish is natural (plan D5): sent again, it answers as it did the
# first time. The finish takes an Idempotency-Key, for its ``moved``.


class JobAccept(BaseModel):
    job: Job
    key: str
    workflow_id: str
    run_id: str
    max_pending: int = 0
    supersedes: str | None = None


class Claims(BaseModel):
    claims: int


class Settled(BaseModel):
    settled: bool
    moved: bool


def _no_such() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "No such render job.")


@router.post("/jobs/accept", name="crud_render_accept_job")
async def accept_job(body: JobAccept, state: StateDep) -> Job:
    """R3: the execution's row, inserted or found by its workflow ids. A full queue is a
    409 ``queue-full`` with nothing written."""
    job = body.job
    try:
        held = await asyncio.to_thread(
            state.projection.accept,
            job,
            body.key,
            workflow_id=body.workflow_id,
            run_id=body.run_id,
            max_pending=body.max_pending,
            supersedes=body.supersedes,
        )
    except QueueFullError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT, str(error), reason=QUEUE_FULL, depth=error.depth
        ) from None
    # A resend finds the row its first send inserted; its request is the same. Not the
    # job id: a retried activity mints a new one. Any other row, found by naming another
    # execution's workflow ids, is answered as a missing one.
    asked = (job.slug, job.kind, job.model_version, job.inputs or legacy_inputs(job.params))
    if (held.slug, held.kind, held.model_version, held.inputs) != asked:
        raise _no_such()
    return held


@router.put("/jobs/{job_id}/claims", name="crud_render_job_claims")
async def set_claims(job_id: str, body: Claims, state: StateDep) -> None:
    """R4: the workflow's claim count, while the row is open."""
    await asyncio.to_thread(state.projection.set_claims, job_id, body.claims)


@router.post("/jobs/{job_id}/started", name="crud_render_job_started")
async def mark_started(job_id: str, state: StateDep) -> Job | None:
    """R5: the row moved from pending to running, or null when it was not pending."""
    return await asyncio.to_thread(state.projection.mark_started, job_id)


@router.put("/jobs/{job_id}/steps", name="crud_render_job_steps")
async def set_steps(job_id: str, body: list[StepInfo], state: StateDep) -> None:
    """R6: the pipeline's steps, while the row is open."""
    await asyncio.to_thread(state.projection.set_steps, job_id, body)


@router.post("/jobs/{job_id}/finish", name="crud_render_finish_job")
async def finish_job(job_id: str, body: Job, key: IdempotencyKey, state: StateDep) -> Settled:
    """R7. ``settled``: whether the row holds this settle, true again for a resend that
    finds the row already holding this terminal state and ``finished_at``. ``moved``:
    whether this write changed the row, the only one that announces it and that the
    worker counts. The write takes an ``Idempotency-Key``, one per call of the worker's
    `finish`: a resend inside that call replays its ``moved``; a later call, a retried
    activity's, gets false."""
    if body.id != job_id:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "The job is not this route's.")
    if body.state not in ("done", "failed", "cancelled"):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"A job does not finish as {body.state}."
        )
    projection = state.projection
    wire = body.model_dump(mode="json")

    def write(conn: Connection[DictRow]) -> Replay:
        moved = projection.finish_in(conn, body)
        settled = moved
        if not moved:
            try:
                held = projection.read_in(conn, job_id)
            except JobNotFoundError:
                held = None
            settled = held is not None and (held.state, held.finished_at) == (
                body.state,
                body.finished_at,
            )
        return Replay(200, {"settled": settled, "moved": moved})

    def run() -> Replay:
        with projection.pool.connection(timeout=PROJECT_POOL_TIMEOUT) as conn:
            return idempotent_write(conn, "render", key, FINISH_ROUTE, wire, lambda: write(conn))

    reply = await asyncio.to_thread(run)
    return Settled.model_validate(reply.body)


@router.get("/jobs/{job_id}", name="crud_render_get_job")
async def read_job(job_id: str, state: StateDep) -> Job:
    """R8."""
    try:
        return await asyncio.to_thread(state.projection.read, job_id)
    except JobNotFoundError:
        raise _no_such() from None


# -- blob refs and piece geometry (inventory R11, R12) ----------------------------
#
# Both natural: a ref sent again is the row already there (ON CONFLICT DO NOTHING), a
# geometry put an upsert, and a forget of a row already gone a success.


class BlobRef(BaseModel):
    key: str
    holder_kind: str
    holder_id: str


class Geometry(BaseModel):
    piece_key: str
    colours: list[str]
    main: dict[str, Any]


@router.post("/blob-refs", name="crud_render_add_blob_ref")
async def add_blob_ref(body: BlobRef, state: StateDep) -> None:
    """R11: hold ``key`` for a render job. The worker holds blobs for its jobs only, so
    any other holder kind is a 403 and a holder that is no render job a 404: a render
    credential cannot pin blobs under another holder. A key no render job holds
    (`HELD_KEY`) is a 403 ``api-owned-key``: a render credential cannot keep a deleted
    customer's file, or any other service's blob, from the sweep."""
    if not HELD_KEY.fullmatch(body.key):
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            f"A render job does not hold {body.key!r}.",
            reason=API_OWNED_KEY,
        )
    if body.holder_kind != "job":
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "The render worker holds blobs for render jobs only.",
            reason=HOLDER_KIND,
        )
    try:
        await asyncio.to_thread(state.projection.read, body.holder_id)
    except JobNotFoundError:
        raise _no_such() from None
    await asyncio.to_thread(state.refs.add, body.key, "job", body.holder_id)


def _geometry(state: AppState) -> GeometryIndex:
    return GeometryIndex(state.refs.pool)


@router.get("/geometry/{geometry_key}", name="crud_render_get_geometry")
async def get_geometry(geometry_key: str, state: StateDep) -> Geometry | None:
    """R12: the finished piece a geometry key's pieces can be recoloured from, or null."""
    row = await asyncio.to_thread(_geometry(state).get, geometry_key)
    if row is None:
        return None
    return Geometry(piece_key=row.piece_key, colours=row.colours, main=row.main)


@router.put("/geometry/{geometry_key}", name="crud_render_put_geometry")
async def put_geometry(geometry_key: str, body: Geometry, state: StateDep) -> None:
    """R12: record the newest finished piece for ``geometry_key``."""
    await asyncio.to_thread(
        _geometry(state).put, geometry_key, body.piece_key, body.colours, body.main
    )


@router.delete("/geometry/{geometry_key}", name="crud_render_forget_geometry")
async def forget_geometry(geometry_key: str, state: StateDep, piece_key: str = Query()) -> None:
    """R12: drop the row, unless another piece has replaced it since it was read."""
    await asyncio.to_thread(_geometry(state).forget, geometry_key, piece_key)


# -- the blob index (inventory R13-R16) ------------------------------------------
#
# Each route is the API's own `BlobIndex` method, so the row locks are taken in the order
# an older worker still writing over SQL during a roll takes them. `put`, `swap`, `mark`
# and both deletes are not natural (a `put` answers the object it replaced, a delete the
# row it took), so each takes an ``Idempotency-Key`` (a 428 without one) and writes its
# ledger row in the index write's transaction, inside the deadlock retry, so a victim
# rolls back whole and is tried again whole. A key or object id may hold slashes. A put,
# swap, mark, delete or stale delete is a 403 unless every key is a render job's
# (`RENDER_KEY`), the ref it writes is a ``piece`` and no row it finds is of another kind;
# a read is a 403 for a key or kind the worker does not read (`HELD_KEY`,
# `READ_PREFIXES`, `READ_KINDS`, `FOUND_KINDS`). A touch is a 403 for any key but one the
# worker holds or the snapshot it renders from (`HELD_KEY`, `TOUCHED_PREFIXES`).


class IndexPut(BaseModel):
    ref: IndexedRef
    slug: str | None
    meta: dict[str, Any]
    reuse: bool = False


class IndexSwap(IndexPut):
    expected: str | None


class IndexMark(BaseModel):
    keys: list[str]
    backend: str
    cutoff: datetime
    meta: dict[str, Any]


class Previous(BaseModel):
    previous: IndexedRef | None


class Swapped(BaseModel):
    swapped: bool


class Shared(BaseModel):
    shared: bool


class IndexNow(BaseModel):
    now: datetime


class IndexUsage(BaseModel):
    count: int
    bytes: int
    by_kind: dict[str, int]


def _index(state: AppState) -> BlobIndex:
    return BlobIndex(state.settings_store.pool)


def _indexed(
    state: AppState,
    lock_key: str,
    key: str | None,
    route: str,
    body: dict[str, Any],
    write: Callable[[BlobIndex, Connection[DictRow]], Any],
) -> Any:
    """``write``'s JSON answer, once per Idempotency-Key ``key``: ``write`` and its ledger
    row in one transaction, run again whole when Postgres picks it as a deadlock's victim
    (`BlobIndex.retrying`). ``body`` is the request, its index key included."""
    idempotency_key = required_key(key)
    index = _index(state)

    def attempt(conn: Connection[DictRow]) -> Replay:
        return idempotent_write(
            conn, "render", idempotency_key, route, body, lambda: Replay(200, write(index, conn))
        )

    try:
        return index.retrying(lock_key, attempt).body
    except ReuseLostError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error), reason=REUSE_LOST) from None
    except DeadlockDetected as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error), reason=DEADLOCK) from None


def _owned(what: str) -> ApiError:
    return ApiError(status.HTTP_403_FORBIDDEN, what, reason=API_OWNED_KEY)


def _worker_writable(*keys: str, ref: IndexedRef | None = None) -> None:
    """Refuse a write of any key that is not a render job's (`RENDER_KEY`), or of a ref
    that is not a ``piece``: a render credential could otherwise point a print library
    file's key at its own 3MF, forge an upload's metadata for an adopt, or drop another
    service's row and leave its object untracked."""
    foreign = sorted(key for key in keys if not RENDER_KEY.fullmatch(key))
    if foreign:
        raise _owned(f"The render worker does not store {foreign[0]!r}: the API writes that key.")
    if ref is not None and ref.kind != RENDER_KIND:
        raise _owned(f"The render worker stores pieces only, not a {ref.kind!r}.")


def _only_pieces(conn: Connection[DictRow], keys: list[str]) -> None:
    """In the write's transaction: refuse when a row of ``keys`` holds another kind than a
    piece. No render key names one today; this keeps it so."""
    row = conn.execute(
        "SELECT key, kind FROM store_blobs WHERE key = ANY(%s) AND kind <> %s ORDER BY key LIMIT 1",
        (keys, RENDER_KIND),
    ).fetchone()
    if row is not None:
        raise _owned(
            f"{row['key']!r} holds a {row['kind']!r}: the render worker writes pieces only."
        )


def _kept_piece(stat: BlobStat | IndexedRef | None) -> None:
    """Refuse, so the transaction rolls back, a write whose row (read under its lock) held
    another kind than a piece."""
    ref = stat.ref if isinstance(stat, BlobStat) else stat
    if ref is not None and ref.kind != RENDER_KIND:
        raise _owned(f"The row held a {ref.kind!r}: the render worker writes pieces only.")


def _worker_readable(key: str) -> None:
    """Refuse a read of a key the render worker never reads: another service's row tells
    its file's name and scope."""
    if not (HELD_KEY.fullmatch(key) or key.startswith(READ_PREFIXES)):
        raise _owned(f"The render worker does not read {key!r}.")


def _worker_kinds(kinds: list[str] | None) -> None:
    """Refuse a listing of a kind the render worker never reads; any kind (none named)
    too."""
    if kinds is None or not set(kinds) <= READ_KINDS:
        raise _owned(f"The render worker reads only {sorted(READ_KINDS)}.")


def _worker_finds(kind: str) -> None:
    """Refuse a find by SHA-256 of a kind the render worker never stores."""
    if kind not in FOUND_KINDS:
        raise _owned(f"The render worker finds only {sorted(FOUND_KINDS)} by SHA-256.")


def _worker_touchable(key: str) -> None:
    """Refuse a touch of a key the render worker neither holds nor renders from: a touch
    keeps a row from its sweep."""
    if not (HELD_KEY.fullmatch(key) or key.startswith(TOUCHED_PREFIXES)):
        raise _owned(f"The render worker does not touch {key!r}.")


def _json(model: BaseModel | None) -> dict[str, Any] | None:
    return None if model is None else model.model_dump(mode="json")


@router.get("/blob-index/now", name="crud_render_index_now")
async def index_now(state: StateDep) -> IndexNow:
    """R13: the database's clock, which every ``touched_at`` is stamped with."""
    return IndexNow(now=await asyncio.to_thread(_index(state).now))


@router.get("/blob-index/usage", name="crud_render_index_usage")
async def index_usage(state: StateDep, backend: str = Query()) -> IndexUsage:
    """R13: the distinct objects on ``backend`` and their bytes, in all and per kind."""
    count, total, by_kind = await asyncio.to_thread(_index(state).usage, backend)
    return IndexUsage(count=count, bytes=total, by_kind=by_kind)


@router.get("/blob-index/stats", name="crud_render_index_stats")
async def index_stats(
    state: StateDep,
    kind: Annotated[list[str] | None, Query()] = None,
    slug: str | None = None,
    backend: str | None = None,
) -> list[BlobStat]:
    """R13: the rows by kind, template and backend. Only the kinds the worker reads
    (`READ_KINDS`), named: any other, or none, is a 403 ``api-owned-key``."""
    _worker_kinds(kind)
    return await asyncio.to_thread(_index(state).stats, kind, slug, backend=backend)


@router.get("/blob-index", name="crud_render_index_by_sha")
async def index_by_sha(
    state: StateDep, sha256: str = Query(), kind: str = Query(), backend: str = Query()
) -> IndexedRef | None:
    """R13: an object already stored with these bytes, or null. Any kind but a piece
    (`FOUND_KINDS`) is a 403 ``api-owned-key``."""
    _worker_finds(kind)
    return await asyncio.to_thread(_index(state).by_sha, sha256, kind, backend)


@router.get("/blob-index/backends/{backend}/{backend_id:path}", name="crud_render_index_shared")
async def index_shared(
    backend: str, backend_id: str, state: StateDep, copies: bool = True
) -> Shared:
    """R16: whether a row names the object, as its own or, with ``copies``, as its
    migrated copy (`shares_backend_id`); without, as its own only (`recorded`)."""
    index = _index(state)
    named = index.shares_backend_id if copies else index.recorded
    return Shared(shared=await asyncio.to_thread(named, backend, backend_id))


@router.post("/blob-index/mark", name="crud_render_index_mark")
async def index_mark(
    body: IndexMark, state: StateDep, idempotency_key: MaybeIdempotencyKey = None
) -> None:
    """R15: merge ``meta`` into the rows of ``keys`` on ``backend`` untouched since
    ``cutoff``. A key that is not a render job's, or a row of another kind than a piece,
    is a 403 ``api-owned-key``."""
    _worker_writable(*body.keys)

    def mark(index: BlobIndex, conn: Connection[DictRow]) -> None:
        BlobIndex.mark_in(conn, body.keys, backend=body.backend, cutoff=body.cutoff, meta=body.meta)
        # After `mark_in`, which locked the rows it matched: a refusal rolls it back.
        _only_pieces(conn, body.keys)

    await asyncio.to_thread(
        _indexed,
        state,
        ",".join(sorted(body.keys)),
        idempotency_key,
        "POST /blob-index/mark",
        body.model_dump(mode="json"),
        mark,
    )


@router.get("/blob-index/{key:path}", name="crud_render_index_get")
async def index_get(key: str, state: StateDep) -> BlobStat | None:
    """R13: ``key``'s row, or null. A key the worker does not read is a 403
    ``api-owned-key``."""
    _worker_readable(key)
    return await asyncio.to_thread(_index(state).get, key)


@router.put("/blob-index/{key:path}", name="crud_render_index_put")
async def index_put(
    key: str, body: IndexPut, state: StateDep, idempotency_key: MaybeIdempotencyKey = None
) -> Previous:
    """R14: point ``key`` at ``ref``; ``previous``, what it named before. A reuse of an
    object freed first is a 409 ``reuse-lost`` with nothing written; a key that is not a
    render job's, or a ref or row of another kind than a piece, a 403 ``api-owned-key``."""
    _worker_writable(key, ref=body.ref)

    def put(index: BlobIndex, conn: Connection[DictRow]) -> dict[str, Any]:
        _only_pieces(conn, [key])
        previous = index.put_in(
            conn, key, body.ref, slug=body.slug, meta=body.meta, reuse=body.reuse
        )
        # Read under the row's lock: a refusal rolls the put back.
        _kept_piece(previous)
        return {"previous": _json(previous)}

    answer = await asyncio.to_thread(
        _indexed,
        state,
        key,
        idempotency_key,
        "PUT /blob-index/{key}",
        {"key": key, **body.model_dump(mode="json")},
        put,
    )
    return Previous.model_validate(answer)


@router.post("/blob-index/{key:path}/swap", name="crud_render_index_swap")
async def index_swap(
    key: str, body: IndexSwap, state: StateDep, idempotency_key: MaybeIdempotencyKey = None
) -> Swapped:
    """R14: point ``key`` at ``ref`` only if it still names ``expected`` (null: no row).
    A key that is not a render job's, or a ref or row of another kind than a piece, is a
    403 ``api-owned-key``. The row's kind is read in the swap's transaction before the
    swap, and checked again under the row's lock: a row that became another kind between
    the two is not rewritten (``swapped`` false)."""
    _worker_writable(key, ref=body.ref)

    def swap(index: BlobIndex, conn: Connection[DictRow]) -> dict[str, Any]:
        _only_pieces(conn, [key])
        swapped = index.swap_in(
            conn,
            key,
            body.ref,
            expected=body.expected,
            slug=body.slug,
            meta=body.meta,
            reuse=body.reuse,
            only_kind=RENDER_KIND,
        )
        return {"swapped": swapped}

    answer = await asyncio.to_thread(
        _indexed,
        state,
        key,
        idempotency_key,
        "POST /blob-index/{key}/swap",
        {"key": key, **body.model_dump(mode="json")},
        swap,
    )
    return Swapped.model_validate(answer)


@router.post("/blob-index/{key:path}/touch", name="crud_render_index_touch")
async def index_touch(key: str, state: StateDep) -> None:
    """R15: mark ``key`` wanted now, so the sweeps keep it. Only a key the worker fetches
    or claims (`CachedBlobStore`: `HELD_KEY`) or the snapshot it renders from
    (`SnapshotStore`, ``src-``): any other is a 403 ``api-owned-key``, or a render
    credential could keep any service's row from its sweep. Natural: a resend stamps
    it again."""
    _worker_touchable(key)
    await asyncio.to_thread(_index(state).touch, key)


@router.post("/blob-index/{key:path}/forget-copy", name="crud_render_index_forget_copy")
async def index_forget_copy(key: str, state: StateDep) -> BlobStat | None:
    """A copy the worker could not read: the API drops its row (`ContentStore.forget`,
    the object stays), so it is stored again. A font's or snapshot's is dropped at once
    (`FontMirror` `backfill`, `SnapshotStore.pin` store it again). A library file's
    object (``lib3-``, `LIBRARY_PIECE_KEY`) only when the API's own read finds its object
    missing or not its SHA-256, so the next Arrange of the file writes it again
    (`bambuddy/library_objects.py` `publish_library_pieces`); one the API reads whole is
    a 409 ``copy-intact``, and its row stays. The row taken, or null. Served with the
    ``assets`` area. Any other key is a 403 ``api-owned-key``: the worker deletes its own
    keys through the index routes. Natural: a resend finds no row."""
    if LIBRARY_PIECE_KEY.fullmatch(key):
        return await _forget_unreadable(key, state)
    if not key.startswith(FORGETTABLE_PREFIXES):
        raise _owned("Only a font's, snapshot's or library object's copy is forgotten here.")
    return await asyncio.to_thread(_index(state).delete, key)


def _intact(detail: str) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, detail, reason=COPY_INTACT)


def _recently_checked(key: str, now: float) -> bool:
    """Whether the API read ``key``'s object whole less than `FORGET_CHECK_COOLDOWN` ago;
    if not, note this check. Best effort, per process: a render credential cannot make
    the API read one object over and over."""
    checked = _LAST_CHECKED.get(key)
    if checked is not None and now - checked < FORGET_CHECK_COOLDOWN:
        return True
    if len(_LAST_CHECKED) >= FORGET_CHECKS_KEPT:
        # Drop the oldest half; the record need not be complete.
        for old in sorted(_LAST_CHECKED, key=_LAST_CHECKED.__getitem__)[: FORGET_CHECKS_KEPT // 2]:
            del _LAST_CHECKED[old]
    _LAST_CHECKED[key] = now
    return False


async def _forget_unreadable(key: str, state: AppState) -> BlobStat | None:
    """Drop ``key``'s row only when the API cannot read its object whole: missing, or not
    its SHA-256. The read is bounded (`FORGET_READ_TIMEOUT`) and done at most once per key
    in `FORGET_CHECK_COOLDOWN`; the drop is a compare-and-delete, so a row stored again
    since the read stays. Every other outcome is a 409 ``copy-intact``, the row kept."""
    content = state.store.content
    if content is None:
        return None  # the local store keeps no index
    index = _index(state)
    stat = await asyncio.to_thread(index.get, key)
    if stat is None:
        return None
    if stat.ref.backend != content.name:
        # Mid `/store/migrate`: another backend's row, which this store cannot judge.
        raise _intact("The copy is on another backend; it is not forgotten.")
    if _recently_checked(key, time.monotonic()):
        raise _intact("The API checked this copy a short while ago; it is not forgotten.")
    try:
        async with asyncio.timeout(FORGET_READ_TIMEOUT):
            if not await content.backend.exists(stat.ref.backend_id):
                # Checked first: a download cache could still hold the bytes.
                raise BlobMissingError(key)
            async with aclosing(content.get(stat.ref)) as chunks:
                async for _ in chunks:
                    pass
    except (BlobMissingError, BlobCorruptError):
        taken = await asyncio.to_thread(index.delete_if_names, key, stat.ref)
        if taken is None:
            raise _intact("The copy was stored again meanwhile; it is not forgotten.") from None
        return taken
    except TimeoutError:
        raise _intact("The API could not read this copy in time; it is not forgotten.") from None
    raise _intact("The API reads this copy whole; it is not forgotten.")


@router.delete("/blob-index/{key:path}/stale", name="crud_render_index_delete_stale")
async def index_delete_stale(
    key: str,
    state: StateDep,
    before: Annotated[datetime, Query()],
    backend: str | None = None,
    idempotency_key: MaybeIdempotencyKey = None,
) -> BlobStat | None:
    """R15: drop ``key``'s row if untouched since ``before``; the row taken, or null. A
    key that is not a render job's, or a row of another kind than a piece, is a 403
    ``api-owned-key``."""
    _worker_writable(key)

    def delete(index: BlobIndex, conn: Connection[DictRow]) -> dict[str, Any] | None:
        taken = BlobIndex.delete_if_stale_in(conn, key, before, backend=backend)
        _kept_piece(taken)
        return _json(taken)

    answer = await asyncio.to_thread(
        _indexed,
        state,
        key,
        idempotency_key,
        "DELETE /blob-index/{key}/stale",
        {"key": key, "before": before.isoformat(), "backend": backend},
        delete,
    )
    return None if answer is None else BlobStat.model_validate(answer)


@router.delete("/blob-index/{key:path}", name="crud_render_index_delete")
async def index_delete(
    key: str,
    state: StateDep,
    backend: str | None = None,
    idempotency_key: MaybeIdempotencyKey = None,
) -> BlobStat | None:
    """R15: drop ``key``'s row (with ``backend``, only a row on it); the row, or null. A
    key that is not a render job's, or a row of another kind than a piece, is a 403
    ``api-owned-key``."""
    _worker_writable(key)

    def delete(index: BlobIndex, conn: Connection[DictRow]) -> dict[str, Any] | None:
        taken = BlobIndex.delete_in(conn, key, backend=backend)
        _kept_piece(taken)
        return _json(taken)

    answer = await asyncio.to_thread(
        _indexed,
        state,
        key,
        idempotency_key,
        "DELETE /blob-index/{key}",
        {"key": key, "backend": backend},
        delete,
    )
    return None if answer is None else BlobStat.model_validate(answer)


# -- the Bambuddy store's folders (inventory R17, R18) ----------------------------
#
# Plan D6: a find holds the folder's advisory lock across its Bambuddy calls, so the
# whole find runs here, in one request, with the API's own client on its render target
# (the render key, as the worker's was), and no lock outlives the request. It takes the
# lock an older worker still finding over SQL takes (`folder_lock_key`, the same 30 s
# `lock_timeout`), so the two exclude each other during a roll. A lock waited for in vain
# (`LockNotAvailable`) and Bambuddy not answering are 4xx answers the worker's client does
# not send again (a 423 ``folder-locked``, a 424 ``bambuddy-unavailable``): the call fails
# after the one wait, with the error the SQL find raised, and Temporal's retry takes
# over. Every route is natural; a forget of nothing is a success.


class FolderFind(BaseModel):
    slug: str
    role: FolderRole
    #: The template's title, which names its folder (`folder_name`).
    title: str
    #: The worker's Bambuddy (`instance_key`) and inbox, which must be the API's.
    instance: str
    inbox_id: int


class FolderFound(BaseModel):
    folder_id: int


def _folders(state: AppState) -> FolderFinder:
    return FolderFinder(state.settings_store.pool)


@router.post("/store-folders/find", name="crud_render_find_store_folder")
async def find_store_folder(body: FolderFind, state: StateDep) -> FolderFound:
    """R17: the folder of ``slug`` in ``role``, found in Bambuddy or made there, and
    recorded. A worker whose Bambuddy or inbox is not the API's (its settings older than
    a change in Settings) is a 409 ``target-moved``: a folder id of one Bambuddy is
    another folder on the other. Bambuddy not answering is a 424
    ``bambuddy-unavailable`` carrying the status and title its client raised
    (``upstream_status``), and a lock waited for in vain a 423 ``folder-locked``: never a
    5xx the worker's client would send again."""
    pool = state.settings_store.pool
    target = render_target(
        await asyncio.to_thread(load_render_store_settings, pool, state.settings)
    )
    inbox = Inbox(target.instance, target.inbox_id)
    if Inbox(instance_key(body.instance), body.inbox_id) != inbox:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "The Bambuddy store's URL or library folder has changed in Settings.",
            reason=TARGET_MOVED,
        )
    try:
        async with BambuddyClient(target.config) as client:
            found = await _folders(state).find(client, inbox, body.slug, body.role, body.title)
    except LockNotAvailable as error:
        raise ApiError(status.HTTP_423_LOCKED, str(error).strip(), reason=FOLDER_LOCKED) from error
    except ApiError as error:
        if error.type != UNAVAILABLE_PROBLEM:
            raise
        raise ApiError(
            status.HTTP_424_FAILED_DEPENDENCY,
            error.detail,
            title=error.title,
            type_=error.type,
            reason=BAMBUDDY_UNAVAILABLE,
            upstream_status=error.status,
            **error.extensions,
        ) from error
    return FolderFound(folder_id=found)


@router.delete("/store-folders", name="crud_render_forget_store_folders")
async def forget_store_folders(
    state: StateDep, slug: str = Query(), instance: str = Query(), inbox_id: int = Query()
) -> None:
    """R18: drop a template's recorded folders (an empty ``slug``: the fonts')."""
    await _folders(state).forget(Inbox(instance, inbox_id), slug)


@router.get("/store-folders/work", name="crud_render_work_store_folders")
async def work_store_folders(
    state: StateDep, instance: str = Query(), inbox_id: int = Query()
) -> list[int]:
    """R18: the `Work/` folders recorded under one inbox of one Bambuddy."""
    return sorted(await _folders(state).work_folders(Inbox(instance, inbox_id)))


# -- the uploads' rows (inventory R19-R21) ---------------------------------------
#
# The files stay on the worker's volume; these are the API's `AssetStore` rows. A `use`
# waits on the row lock the sweep takes before it re-checks and deletes, as the worker's
# SQL did. An adopt takes the asset's advisory lock, as an upload and the sweep do, and is
# natural (ON CONFLICT: adopted twice, one row). The worker sets no field of the row: the
# API writes it from its own blob-store copy's meta (`RemoteAssets.mirror`), which must be
# the file the worker names (id, kind, size), so a worker cannot record a name, source,
# licence or shape the upload never had.


class LastUsedAsk(BaseModel):
    ids: list[Annotated[str, Field(pattern=ASSET_ID_PATTERN)]] = Field(max_length=LAST_USED_MAX)


def _no_asset() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "No such upload.")


@router.get("/assets/{asset_id}", name="crud_render_get_asset")
async def get_asset(asset_id: str, state: StateDep) -> AssetMeta:
    """R19: the upload's row; a 404 for none, or an id that is not one."""
    try:
        return await asyncio.to_thread(state.assets.row, asset_id)
    except AssetNotFoundError:
        raise _no_asset() from None


@router.post("/assets/{asset_id}/use", name="crud_render_use_asset")
async def use_asset(asset_id: str, state: StateDep) -> AssetMeta:
    """R19: mark the upload used now and answer its row, so a sweep under way keeps it;
    a 404 once the sweep has deleted it."""
    try:
        return await asyncio.to_thread(state.assets.use_row, asset_id)
    except AssetNotFoundError:
        raise _no_asset() from None


@router.post("/assets/{asset_id}/adopt", name="crud_render_adopt_asset")
async def adopt_asset(asset_id: str, body: AssetMeta, state: StateDep) -> None:
    """R20: record an upload the worker fetched from the blob store, the row written from
    the API's own copy's meta. A 422 for an id that is not one or a body of another id;
    a 422 ``copy-mismatch`` for a copy of another kind or size; a 404 ``no-copy`` when the
    blob store holds no copy of it."""
    if not ASSET_ID_RE.fullmatch(asset_id) or body.id != asset_id:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "Not this route's upload id.")
    stat = await asyncio.to_thread(_index(state).get, asset_key(asset_id))
    try:
        held = None if stat is None else AssetMeta.model_validate(stat.meta)
    except ValidationError:
        held = None
    if held is None or held.id != asset_id:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "The blob store holds no copy of this upload.",
            reason=NO_COPY,
        )
    if (held.kind, held.size) != (body.kind, body.size):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The blob store's copy of this upload is not the file named.",
            reason=COPY_MISMATCH,
        )
    try:
        await asyncio.to_thread(state.assets.adopt_row, held)
    except AssetRejectedError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


class Dropped(BaseModel):
    dropped: bool


@router.post("/assets/{asset_id}/drop-copy", name="crud_render_drop_asset_copy")
async def drop_asset_copy(asset_id: str, state: StateDep) -> Dropped:
    """An upload's copy in the blob store the worker could not read (gone or altered):
    the API drops it with its own `ContentStore.delete`: the row, then the object, the
    row put back if the object cannot be released, so it stays tracked and a later pass
    retries. ``dropped``: whether it went. A release that fails is logged and answered
    false, not a 5xx the worker's client would send again; the copy is then still the
    API's `backfill`'s and sweep's to deal with. Natural: a resend finds no row."""
    if not ASSET_ID_RE.fullmatch(asset_id):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "Not an upload id.")
    content = state.store.content
    if content is None:
        return Dropped(dropped=False)  # the local store keeps no copies
    key = asset_key(asset_id)
    if await asyncio.to_thread(content.index.get, key) is None:
        return Dropped(dropped=False)
    try:
        await content.delete(key)
    except Exception:
        logger.exception("could not drop an unreadable upload's copy", extra={"key": key})
        return Dropped(dropped=False)
    return Dropped(dropped=True)


@router.post("/assets/last-used", name="crud_render_assets_last_used")
async def assets_last_used(body: LastUsedAsk, state: StateDep) -> dict[str, datetime | None]:
    """R21: each id's last use, null for one with no row; at most `LAST_USED_MAX` ids."""
    return await asyncio.to_thread(state.assets.last_used, body.ids)


# -- library leases, pin holds and install slots (inventory R22-R24) --------------
#
# Each route is the API's own `CheckoutLeases` or `InstallSlotRows` method, on the API's
# gate and permits. A lease or a hold is inserted under `REMOVAL_LOCK` shared, the key an
# older worker on SQL and the API's removal take, so the three exclude each other during
# a roll. Every write is natural (plan D5): the worker chooses the token, and a take sent
# again finds the same lease, hold or slot. A renew or release names the token; one that
# matches no row of the caller's answers a 4xx with a ``reason``, never a silent success,
# so a render credential renews and releases only what it took. ``ttl_s`` is at most
# `LEASE_TTL_MAX` (300 s): a row the worker stops renewing lapses within it.


class Checkout(BaseModel):
    library: str = Field(pattern=NAME_PATTERN)
    commit: str = Field(pattern=COMMIT_PATTERN)


class Ttl(BaseModel):
    ttl_s: float = Field(default=LEASE_TTL, gt=0, le=LEASE_TTL_MAX)


class LeaseTake(Ttl):
    token: uuid.UUID
    holder: str = Field(min_length=1, max_length=256)
    checkouts: list[Checkout] = Field(min_length=1, max_length=LEASE_CHECKOUTS_MAX)


class PinHoldTake(Ttl):
    token: uuid.UUID


class SlotClaim(BaseModel):
    token: uuid.UUID


class SlotClaimed(BaseModel):
    slot: int | None


def _leases(state: AppState) -> CheckoutLeases:
    leases = state.checkouts.shared
    assert isinstance(leases, CheckoutLeases)
    return leases


def _slots(state: AppState) -> InstallSlotRows:
    slots = state.installs.slots
    assert isinstance(slots, InstallSlotRows)
    return slots


def _removal_running() -> ApiError:
    return ApiError(
        status.HTTP_423_LOCKED,
        "A library removal is running; send the take again.",
        reason=REMOVAL_RUNNING,
    )


@router.post("/leases", name="crud_render_take_lease")
async def take_lease(body: LeaseTake, state: StateDep) -> None:
    """R22: a lease for ``holder`` on ``checkouts`` under ``token``, once no removal
    runs. A removal still running after `LEASE_LOCK_WAIT` is a 423 ``removal-running``;
    a token holding another lease, a 409 ``token-taken``."""
    try:
        await asyncio.to_thread(
            _leases(state).take_named,
            body.token,
            body.holder,
            [(checkout.library, checkout.commit) for checkout in body.checkouts],
            ttl=body.ttl_s,
            lock_wait=LEASE_LOCK_WAIT,
        )
    except LockNotAvailable:
        raise _removal_running() from None
    except LeaseTokenTakenError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "This token holds another lease.",
            reason=TOKEN_TAKEN,
        ) from None


@router.put("/leases/{token}", name="crud_render_renew_lease")
async def renew_lease(token: uuid.UUID, body: Ttl, state: StateDep) -> None:
    """R22: the lease's rows live ``ttl_s`` more; a 404 ``no-lease`` when it has none."""
    if not await asyncio.to_thread(_leases(state).renew, token, ttl=body.ttl_s):
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such lease.", reason=NO_LEASE)


@router.delete("/leases/{token}", name="crud_render_drop_lease")
async def drop_lease(token: uuid.UUID, state: StateDep) -> None:
    """R22: release the lease; a lease already gone is a success."""
    await asyncio.to_thread(_leases(state).drop, token)


@router.post("/pin-holds", name="crud_render_take_pin_hold")
async def take_pin_hold(body: PinHoldTake, state: StateDep) -> None:
    """R23: a pin's hold under ``token``, once no removal runs; a 423
    ``removal-running`` as a lease's."""
    try:
        await asyncio.to_thread(
            _leases(state).take_pin, body.token, ttl=body.ttl_s, lock_wait=LEASE_LOCK_WAIT
        )
    except LockNotAvailable:
        raise _removal_running() from None


@router.put("/pin-holds/{token}", name="crud_render_renew_pin_hold")
async def renew_pin_hold(token: uuid.UUID, body: Ttl, state: StateDep) -> None:
    """R23: the hold lives ``ttl_s`` more; a 404 ``no-hold`` when it has no row."""
    if not await asyncio.to_thread(_leases(state).renew_pin, token, ttl=body.ttl_s):
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such pin hold.", reason=NO_HOLD)


@router.delete("/pin-holds/{token}", name="crud_render_drop_pin_hold")
async def drop_pin_hold(token: uuid.UUID, state: StateDep) -> None:
    """R23: release the hold; a hold already gone is a success."""
    await asyncio.to_thread(_leases(state).drop_pin, token)


@router.post("/install-slots", name="crud_render_claim_install_slot")
async def claim_install_slot(body: SlotClaim, state: StateDep) -> SlotClaimed:
    """R24: the slot ``token`` holds, or a free one of the API's, taken for the API's
    TTL; null while all are held. Sent again, the same slot."""
    return SlotClaimed(slot=await asyncio.to_thread(_slots(state).claim, body.token))


def _not_holder() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT, "This token does not hold that slot.", reason=NOT_HOLDER
    )


@router.put("/install-slots/{slot}", name="crud_render_renew_install_slot")
async def renew_install_slot(
    slot: int, state: StateDep, token: Annotated[uuid.UUID, Query()]
) -> None:
    """R24: the slot lives the API's TTL more; a 409 ``not-holder`` unless ``token``
    holds it."""
    if not await asyncio.to_thread(_slots(state).renew, slot, token):
        raise _not_holder()


@router.delete("/install-slots/{slot}", name="crud_render_drop_install_slot")
async def drop_install_slot(
    slot: int, state: StateDep, token: Annotated[uuid.UUID, Query()]
) -> None:
    """R24: free the slot. A free slot is a success (a release sent again); one another
    token holds, a 409 ``not-holder``."""
    if not await asyncio.to_thread(_slots(state).drop, slot, token):
        raise _not_holder()
