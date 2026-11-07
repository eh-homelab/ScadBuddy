"""The render worker's CRUD routes (#5298): the store settings it renders with (inventory
R2), for `crud/render.py` `RemoteRenderSettingsSource`, its jobs' rows (R3-R10), for
`RemoteJobProjection`, and its jobs' blob refs and the piece geometry (R11, R12), for
`RemoteBlobRefs` and `RemoteGeometryIndex`, the blob index (R13-R16), for
`RemoteBlobIndex`, the Bambuddy store's folders (R17, R18), for `RemoteStoreFolders`, and
the uploads' ``assets`` rows (R19-R21), for `RemoteAssetRows`.

The settings answer holds secrets in plaintext, released only to ``crud.render`` (plan D8): the
render key (or the full key while no render key is saved) and the S3 keys. The CRUD
router answers ``Cache-Control: no-store``, and each answer is logged with the secrets it
released and the credential it released them to, never their values.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
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
)
from scadbuddy.library.settings_store import RenderStoreSettings, load_render_store_settings
from scadbuddy.render.inputs import legacy_inputs
from scadbuddy.render.job_models import Job, JobNotFoundError, QueueFullError, StepInfo
from scadbuddy.render.projection import PROJECT_POOL_TIMEOUT
from scadbuddy.store.assets import asset_key
from scadbuddy.store.bambuddy import FolderFinder, FolderRole, Inbox, instance_key, render_target
from scadbuddy.store.content_models import BlobRef as IndexedRef
from scadbuddy.store.content_models import BlobStat, ReuseLostError
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
#: A blob-index put, swap or mark of a key only the API writes: a 403.
API_OWNED_KEY = "api-owned-key"
#: The index keys only the API writes: an upload's copy (`store/assets.py` `asset_key`,
#: whose meta an adopt writes the upload's row from), a font family's
#: (`store/fonts.py` `font_key`) and a template revision's snapshot
#: (`store/snapshots.py` `snapshot_key`). The render worker reads them, and deletes or
#: forgets a copy it cannot read, but never stores one.
API_OWNED_PREFIXES = ("asset-", "font-", "src-")
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


def released(current: RenderStoreSettings) -> list[str]:
    """The names of the secrets in ``current``, for the log."""
    names = []
    if current.api_key is not None:
        names.append("bambuddy_api_key" if current.key_is_fallback else "bambuddy_render_api_key")
    if current.s3 is not None:
        names += ["store_s3_access_key_id", "store_s3_secret_access_key"]
    return names


@router.get("/settings", name="crud_render_settings")
async def render_settings(request: Request, state: StateDep) -> RenderStoreSettings:
    current = await asyncio.to_thread(
        load_render_store_settings, state.settings_store.pool, state.settings
    )
    # The guard admitted only a render `Service`.
    principal = caller_of(request.scope)
    assert isinstance(principal, Service)
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
    credential cannot pin blobs under another holder."""
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
# swap or mark of a key only the API writes (`API_OWNED_PREFIXES`) is a 403.


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


def _worker_writable(*keys: str) -> None:
    """Refuse a write of a key only the API writes (`API_OWNED_PREFIXES`): a render
    credential could otherwise forge an upload's metadata for an adopt, or a font's or
    snapshot's object for another render."""
    owned = sorted(key for key in keys if key.startswith(API_OWNED_PREFIXES))
    if owned:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            f"The render worker does not store {owned[0]!r}: only the API writes that key.",
            reason=API_OWNED_KEY,
        )


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
    """R13: the rows by kind (any, without one), template and backend."""
    return await asyncio.to_thread(_index(state).stats, kind, slug, backend=backend)


@router.get("/blob-index", name="crud_render_index_by_sha")
async def index_by_sha(
    state: StateDep, sha256: str = Query(), kind: str = Query(), backend: str = Query()
) -> IndexedRef | None:
    """R13: an object already stored with these bytes, or null."""
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
    ``cutoff``. A key only the API writes is a 403 ``api-owned-key``."""
    _worker_writable(*body.keys)
    await asyncio.to_thread(
        _indexed,
        state,
        ",".join(sorted(body.keys)),
        idempotency_key,
        "POST /blob-index/mark",
        body.model_dump(mode="json"),
        lambda index, conn: BlobIndex.mark_in(
            conn, body.keys, backend=body.backend, cutoff=body.cutoff, meta=body.meta
        ),
    )


@router.get("/blob-index/{key:path}", name="crud_render_index_get")
async def index_get(key: str, state: StateDep) -> BlobStat | None:
    """R13: ``key``'s row, or null."""
    return await asyncio.to_thread(_index(state).get, key)


@router.put("/blob-index/{key:path}", name="crud_render_index_put")
async def index_put(
    key: str, body: IndexPut, state: StateDep, idempotency_key: MaybeIdempotencyKey = None
) -> Previous:
    """R14: point ``key`` at ``ref``; ``previous``, what it named before. A reuse of an
    object freed first is a 409 ``reuse-lost`` with nothing written; a key only the API
    writes, a 403 ``api-owned-key``."""
    _worker_writable(key)
    answer = await asyncio.to_thread(
        _indexed,
        state,
        key,
        idempotency_key,
        "PUT /blob-index/{key}",
        {"key": key, **body.model_dump(mode="json")},
        lambda index, conn: {
            "previous": _json(
                index.put_in(conn, key, body.ref, slug=body.slug, meta=body.meta, reuse=body.reuse)
            )
        },
    )
    return Previous.model_validate(answer)


@router.post("/blob-index/{key:path}/swap", name="crud_render_index_swap")
async def index_swap(
    key: str, body: IndexSwap, state: StateDep, idempotency_key: MaybeIdempotencyKey = None
) -> Swapped:
    """R14: point ``key`` at ``ref`` only if it still names ``expected`` (null: no row).
    A key only the API writes is a 403 ``api-owned-key``."""
    _worker_writable(key)
    answer = await asyncio.to_thread(
        _indexed,
        state,
        key,
        idempotency_key,
        "POST /blob-index/{key}/swap",
        {"key": key, **body.model_dump(mode="json")},
        lambda index, conn: {
            "swapped": index.swap_in(
                conn,
                key,
                body.ref,
                expected=body.expected,
                slug=body.slug,
                meta=body.meta,
                reuse=body.reuse,
            )
        },
    )
    return Swapped.model_validate(answer)


@router.post("/blob-index/{key:path}/touch", name="crud_render_index_touch")
async def index_touch(key: str, state: StateDep) -> None:
    """R15: mark ``key`` wanted now. Natural: a resend stamps it again."""
    await asyncio.to_thread(_index(state).touch, key)


@router.delete("/blob-index/{key:path}/stale", name="crud_render_index_delete_stale")
async def index_delete_stale(
    key: str,
    state: StateDep,
    before: Annotated[datetime, Query()],
    backend: str | None = None,
    idempotency_key: MaybeIdempotencyKey = None,
) -> BlobStat | None:
    """R15: drop ``key``'s row if untouched since ``before``; the row taken, or null."""
    answer = await asyncio.to_thread(
        _indexed,
        state,
        key,
        idempotency_key,
        "DELETE /blob-index/{key}/stale",
        {"key": key, "before": before.isoformat(), "backend": backend},
        lambda index, conn: _json(BlobIndex.delete_if_stale_in(conn, key, before, backend=backend)),
    )
    return None if answer is None else BlobStat.model_validate(answer)


@router.delete("/blob-index/{key:path}", name="crud_render_index_delete")
async def index_delete(
    key: str,
    state: StateDep,
    backend: str | None = None,
    idempotency_key: MaybeIdempotencyKey = None,
) -> BlobStat | None:
    """R15: drop ``key``'s row (with ``backend``, only a row on it); the row, or null."""
    answer = await asyncio.to_thread(
        _indexed,
        state,
        key,
        idempotency_key,
        "DELETE /blob-index/{key}",
        {"key": key, "backend": backend},
        lambda index, conn: _json(BlobIndex.delete_in(conn, key, backend=backend)),
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
    the API's own copy's meta. A 422 for an id that is not one, a body of another id, or
    a copy of another kind or size; a 404 when the blob store holds no copy of it."""
    if not ASSET_ID_RE.fullmatch(asset_id) or body.id != asset_id:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "Not this route's upload id.")
    stat = await asyncio.to_thread(_index(state).get, asset_key(asset_id))
    try:
        held = None if stat is None else AssetMeta.model_validate(stat.meta)
    except ValidationError:
        held = None
    if held is None or held.id != asset_id:
        raise ApiError(status.HTTP_404_NOT_FOUND, "The blob store holds no copy of this upload.")
    if (held.kind, held.size) != (body.kind, body.size):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The blob store's copy of this upload is not the file named.",
        )
    await asyncio.to_thread(state.assets.adopt_row, held)


@router.post("/assets/last-used", name="crud_render_assets_last_used")
async def assets_last_used(body: LastUsedAsk, state: StateDep) -> dict[str, datetime | None]:
    """R21: each id's last use, null for one with no row; at most `LAST_USED_MAX` ids."""
    return await asyncio.to_thread(state.assets.last_used, body.ids)
