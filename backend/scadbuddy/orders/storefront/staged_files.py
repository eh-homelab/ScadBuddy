"""Each staged customer file's life, owned by its watcher (#6133, spec 2026-11-13 §6.5).

`watch` starts one ``StagedFile`` workflow per upload (`workflows/staged_file.py`) and the
presign route calls it before it answers any URL: a start that fails answers 503 and no
URL, so no file lands in the staging area without a watcher. `poke` signals the watchers
of a change (a claim, a removed line, a submit); a signal only wakes a watcher early, so a
lost one costs time, never a file.

`step` is the watcher's one activity. It reads the file's state under the order's lock (the
same lock a claim, a line's removal and a submit take) and cleans the file up when it is
due, in the same transaction:

- an upload never claimed, `UNCLAIMED_GRACE` past its URL's expiry (``unclaimed``);
- a claimed upload whose line is gone (``removed``);
- a claimed upload of a cart not submitted `STAGING_TTL` after its claim (``expired``: the
  line reads `EXPIRED`);
- an upload of a submitted order a person declined (``declined``);
- an upload of a submitted order cancelled or closed `ENDED_GRACE` ago (``order_ended``: the
  source reads ``declined``, `ENDED`). A reopen within the grace clears the order's
  ``cancelled_at``/``closed_at``, so the next step reads it live again (#6164).

A live submitted order's file has no deadline: only its import (a later PR), a decline or
the order's end ends it.
A clean-up deletes the row, commits, then deletes the staging object and its preview (a
missing object is no error); a step retried after a crash in between finds no row and
deletes them again (``gone``). It never reads a byte of the file (#5295, #5296).

`sweep_staged` is the backstop, in the storefront drafts sweep: a staging object no row
names past `ORPHAN_AGE` is deleted, a row past `ORPHAN_AGE` whose watcher is not running
gets its watcher started again, and a file source still waiting for its check past
`CHECK_STALE` whose check is not running gets its check started again. A pass checks at
most `SWEEP_PASS_BATCHES` batches of rows and the next pass goes on from there (the
cursor is the process's; a restart starts again from the oldest row). Each counts
`scadbuddy_staged_files_repaired`; any count is
a bug to look into.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

from fastapi import status
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError

from scadbuddy.core.events import OrderChanged, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.storefront.listings import problem
from scadbuddy.orders.storefront.model_requests import (
    CHECK_STALE,
    ENDED,
    ENDED_GRACE,
    EXPIRED,
    ORPHAN_AGE,
    STAGING_TTL,
    SWEEP_BATCH,
    SWEEP_PASS_BATCHES,
    UNCLAIMED_GRACE,
)
from scadbuddy.store.s3 import S3ContentBackend, staged_upload, staging_key
from scadbuddy.workflows.commands import execution_gone
from scadbuddy.workflows.staged_file import (
    STAGED_FILE_WORKFLOW,
    STEP_ACTIVITY,
    StagedFileState,
    workflow_id,
)

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

logger = logging.getLogger(__name__)

#: The 503 a presign answers when its watchers cannot be started.
WATCH_UNAVAILABLE: Final = "uploads_unavailable"


class Watcher(Protocol):
    """Starts and signals the files' watchers (`TemporalWatcher`; a test's fake)."""

    async def watch(self, upload_ids: Sequence[str]) -> None: ...

    async def poke(self, upload_ids: Sequence[str], signal: str) -> None: ...

    async def running(self, upload_id: str) -> bool | None: ...


class TemporalWatcher:
    def __init__(self, client: Client, task_queue: str) -> None:
        self.client = client
        self.task_queue = task_queue

    async def watch(self, upload_ids: Sequence[str]) -> None:
        """One running watcher per upload: a watcher already running is joined (a presign
        sent again re-signs the same slots), and one that ended is started again, as its
        step reads the file's state afresh."""
        for upload_id in upload_ids:
            await self.client.start_workflow(
                STAGED_FILE_WORKFLOW,
                upload_id,
                id=workflow_id(upload_id),
                task_queue=self.task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )

    async def poke(self, upload_ids: Sequence[str], signal: str) -> None:
        for upload_id in upload_ids:
            try:
                await self.client.get_workflow_handle(workflow_id(upload_id)).signal(signal)
            except Exception:
                # The watcher reads the state at its next deadline anyway.
                logger.warning("could not signal %s to staged file %s", signal, upload_id)

    async def running(self, upload_id: str) -> bool | None:
        """Whether the upload's watcher runs; None when Temporal does not answer."""
        try:
            described = await self.client.get_workflow_handle(workflow_id(upload_id)).describe()
        except RPCError as error:
            return False if execution_gone(error) else None
        return described.status == WorkflowExecutionStatus.RUNNING


def watcher(state: AppState) -> Watcher:
    """The app's watcher: the operations' client, on the ``library`` queue."""
    from scadbuddy.operations.component import OPERATIONS

    ops = state.components.get(OPERATIONS)
    return TemporalWatcher(ops.client, state.settings.temporal_task_queue_library)


def unavailable() -> ApiError:
    """503 ``uploads_unavailable``: send the presign again in a minute."""
    return problem(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        WATCH_UNAVAILABLE,
        "Uploads are not available now. Try again in a minute.",
    )


async def watch(state: AppState, upload_ids: Sequence[str]) -> None:
    """Start the uploads' watchers; 503 ``uploads_unavailable`` when Temporal refuses."""
    try:
        await watcher(state).watch(upload_ids)
    except Exception:
        logger.warning("could not start the watchers of %d staged files", len(upload_ids))
        raise unavailable() from None


async def poke(state: AppState, upload_ids: Sequence[str], signal: str) -> None:
    """Wake the uploads' watchers; a failure is logged, never raised."""
    if upload_ids:
        try:
            await watcher(state).poke(upload_ids, signal)
        except Exception:
            logger.warning("could not signal %s to %d staged files", signal, len(upload_ids))


def uploads_of(
    state: AppState,
    *,
    order_id: str | None = None,
    item_id: str | None = None,
    conn: Any = None,
) -> list[str]:
    """The staged uploads the order's (or the item's) sources name; on ``conn`` when given
    (the caller's transaction and its locks), else on a connection of its own."""
    column, value = ("order_id", order_id) if item_id is None else ("item_id", item_id)
    sql = f"SELECT upload_id FROM order_item_sources WHERE {column} = %s AND upload_id IS NOT NULL"
    if conn is not None:
        return [row["upload_id"] for row in conn.execute(sql, (value,)).fetchall()]
    with state.projection.pool.connection() as own:
        return [row["upload_id"] for row in own.execute(sql, (value,)).fetchall()]


# -- the watcher's step -----------------------------------------------------------------


def staged_backend(state: AppState) -> S3ContentBackend | None:
    content = state.store.content
    if state.store.backend != "s3" or content is None:
        return None
    backend = content.backend
    return backend if isinstance(backend, S3ContentBackend) else None


def _ended_at(order: Any) -> datetime | None:
    """When a cancelled or closed order ended; None while it is live."""
    if order["status"] == "cancelled":
        return order["cancelled_at"]  # type: ignore[no-any-return]
    if order["status"] == "closed":
        return order["closed_at"]  # type: ignore[no-any-return]
    return None


def _decide(state: AppState, upload_id: str, now: datetime) -> tuple[StagedFileState, Any]:
    """The file's state, and the order changed by a clean-up (or None). A file due for
    clean-up loses its row in this transaction; the caller deletes its objects."""
    with state.projection.pool.connection() as conn, conn.transaction():
        found = conn.execute(
            "SELECT s.order_id FROM storefront_uploads u"
            " LEFT JOIN order_item_sources s ON s.upload_id = u.id WHERE u.id = %s",
            (upload_id,),
        ).fetchone()
        if found is None:
            return StagedFileState(phase="ended", reason="gone"), None
        order = None
        if found["order_id"] is not None:
            # The order's lock first, as a claim, a line's removal and a submit take it.
            order = conn.execute(
                "SELECT id, status, submitted_at, cancelled_at, closed_at FROM orders"
                " WHERE id = %s FOR UPDATE",
                (found["order_id"],),
            ).fetchone()
        row = conn.execute(
            "SELECT u.expires_at, u.claimed_at, s.id AS source_id, s.order_id, s.status"
            " FROM storefront_uploads u LEFT JOIN order_item_sources s ON s.upload_id = u.id"
            " WHERE u.id = %s FOR UPDATE OF u",
            (upload_id,),
        ).fetchone()
        if row is None:
            return StagedFileState(phase="ended", reason="gone"), None
        if row["source_id"] is None:
            if row["claimed_at"] is not None:
                reason = "removed"
            else:
                due = row["expires_at"] + UNCLAIMED_GRACE
                if now < due:
                    return StagedFileState(phase="unclaimed", due=due), None
                reason = "unclaimed"
            order = None
        elif order is None or order["id"] != row["order_id"]:
            # The line moved since the first read: read it again on the next step.
            return StagedFileState(phase="claimed", due=now), None
        elif order["submitted_at"] is None:
            due = row["claimed_at"] + STAGING_TTL
            if now < due:
                return StagedFileState(phase="claimed", due=due), None
            reason = "expired"
            conn.execute(
                "UPDATE order_item_sources SET check_status = 'refused', check_reason = %s,"
                " updated_at = now() WHERE id = %s",
                (EXPIRED, row["source_id"]),
            )
        elif row["status"] == "declined":
            reason = "declined"
        else:
            ended = _ended_at(order)
            if ended is None:
                return StagedFileState(phase="submitted"), None
            due = ended + ENDED_GRACE
            if now < due:
                return StagedFileState(phase="submitted", due=due), None
            reason = "order_ended"
            conn.execute(
                "UPDATE order_item_sources SET status = 'declined', status_reason = %s,"
                " updated_at = now() WHERE id = %s",
                (ENDED, row["source_id"]),
            )
        conn.execute("DELETE FROM storefront_uploads WHERE id = %s", (upload_id,))
        return StagedFileState(phase="ended", reason=reason), order


async def step(state: AppState, upload_id: str, *, now: datetime | None = None) -> StagedFileState:
    """One turn of the file's watcher (the module docstring): its state, and a clean-up
    when it is due. Safe to run again at any point."""
    backend = staged_backend(state)
    if backend is None:
        # No slot is ever made on another store, so there is nothing to watch.
        return StagedFileState(phase="ended", reason="gone")
    now = datetime.now(UTC) if now is None else now
    found, order = await asyncio.to_thread(_decide, state, upload_id, now)
    if found.phase != "ended":
        return found
    prefix = backend.settings.prefix
    # The upload, its preview and every frozen copy, a losing claim attempt's included.
    await backend.remove_staged_of(staging_key(prefix, upload_id))
    if order is not None:
        emit(
            state.events,
            OrderChanged(
                order_id=order["id"], status=order["status"], cart=order["submitted_at"] is None
            ),
        )
    logger.info("staged file %s ended: %s", upload_id, found.reason)
    return found


def staged_file_activities(state: AppState) -> list[Any]:
    """The watcher's activity, for the ``library`` worker."""

    @activity.defn(name=STEP_ACTIVITY)
    async def staged_file_step(upload_id: str) -> StagedFileState:
        return await step(state, upload_id)

    return [staged_file_step]


# -- the backstop -----------------------------------------------------------------------


def _old_rows(
    state: AppState, now: datetime, after: tuple[datetime, str] | None
) -> list[tuple[datetime, str]]:
    """The next `SWEEP_BATCH` rows older than `ORPHAN_AGE`, in (``created_at``, ``id``)
    order after ``after``: the backstop walks every old row, a batch at a time."""
    floor = (datetime.min.replace(tzinfo=UTC), "") if after is None else after
    with state.projection.pool.connection() as conn:
        rows = conn.execute(
            "SELECT created_at, id FROM storefront_uploads"
            " WHERE created_at < %s AND (created_at, id) > (%s, %s)"
            " ORDER BY created_at, id LIMIT %s",
            (now - ORPHAN_AGE, floor[0], floor[1], SWEEP_BATCH),
        ).fetchall()
    return [(row["created_at"], row["id"]) for row in rows]


#: Where each app's next backstop pass goes on from (by the state's id; a test's app has its
#: own).
_resume: dict[int, tuple[datetime, str]] = {}


def _named(state: AppState, keys: Sequence[str]) -> set[str]:
    with state.projection.pool.connection() as conn:
        rows = conn.execute(
            "SELECT object_key FROM storefront_uploads WHERE object_key = ANY(%s)", (list(keys),)
        ).fetchall()
    return {row["object_key"] for row in rows}


async def sweep_staged(state: AppState, *, now: datetime | None = None) -> int:
    """The backstop (the module docstring); how many things it repaired. It should find
    nothing: each repair is logged and counted. Nothing on a store that is not S3."""
    backend = staged_backend(state)
    if backend is None:
        return 0
    now = datetime.now(UTC) if now is None else now
    repaired = 0
    old: list[str] = []
    async for object_key, written in backend.staged_objects():
        if written < now - ORPHAN_AGE:
            old.append(object_key)
    if old:
        uploads = [staged_upload(backend.settings.prefix, key) or key for key in old]
        named = await asyncio.to_thread(_named, state, uploads)
        for object_key, upload in zip(old, uploads, strict=True):
            if upload not in named:
                logger.warning("deleting a staging object no row names: %s", object_key)
                await backend.remove_staged(object_key)
                state.metrics.staged_files_repaired.labels("orphan_object").inc()
                repaired += 1
    watching = watcher(state)
    after = _resume.pop(id(state), None)
    for _ in range(SWEEP_PASS_BATCHES):
        batch = await asyncio.to_thread(_old_rows, state, now, after)
        if not batch:
            break
        for _created, upload_id in batch:
            if await watching.running(upload_id) is False:
                logger.warning("staged file %s had no running watcher; starting one", upload_id)
                try:
                    await watching.watch([upload_id])
                except Exception:
                    logger.warning("could not start the watcher of staged file %s", upload_id)
                    continue
                state.metrics.staged_files_repaired.labels("unwatched_row").inc()
                repaired += 1
        after = batch[-1]
    else:
        # The pass stopped before the last row: the next one goes on from here.
        if after is not None:
            _resume[id(state)] = after
    return repaired + await _restart_checks(state, now)


def _unchecked(state: AppState, now: datetime) -> list[str]:
    """The file sources still waiting for their check past `CHECK_STALE`, oldest first."""
    with state.projection.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM order_item_sources WHERE kind = 'file' AND check_status = 'checking'"
            " AND upload_id IS NOT NULL AND updated_at < %s ORDER BY updated_at, id LIMIT %s",
            (now - CHECK_STALE, SWEEP_BATCH * SWEEP_PASS_BATCHES),
        ).fetchall()
    return [row["id"] for row in rows]


async def _restart_checks(state: AppState, now: datetime) -> int:
    """A file source whose check is not running (its start was lost, or the check ended
    without a verdict) gets its check started again (#6133 PR 2)."""
    from scadbuddy.orders.storefront.intake import checker

    checking = checker(state)
    repaired = 0
    for source_id in await asyncio.to_thread(_unchecked, state, now):
        if await checking.running(source_id) is False:
            logger.warning("customer file %s had no running check; starting one", source_id)
            try:
                await checking.start([source_id])
            except Exception:
                logger.warning("could not start the check of customer file %s", source_id)
                continue
            state.metrics.staged_files_repaired.labels("unchecked_source").inc()
            repaired += 1
    return repaired
