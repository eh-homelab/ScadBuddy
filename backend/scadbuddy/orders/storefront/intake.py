"""The CRUD side of a customer file's check (#6133, spec 2026-11-13 §7).

The check runs in the intake worker, which holds no database secret (#5295, #5296). This
process never opens the file: it presigns a GET of the staging object, presigns a PUT of
its preview, and records the verdict the worker posts. The preview is taken as opaque
bytes too: read capped, stored in the content store as `preview_key` (``customer-preview-
<sha256>``), held by the source (``blob_refs`` holder `PREVIEW_HOLDER`; the migration
``…_intake_worker.sql`` drops the hold with the source), and the staging copy deleted.

`start_checks` starts one ``CustomerFileCheck`` per file source on the intake worker's queue
after a claim. A start that fails is logged; the staged files' backstop
(`staged_files.sweep_staged`) starts it again for a source still ``checking`` past
`CHECK_STALE`, so a lost start costs time, never a check.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final, Protocol

from fastapi import status
from psycopg.types.json import Jsonb
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError

from scadbuddy.core.events import OrderChanged, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent_write
from scadbuddy.intake_worker.wire import (
    CHECK_WORKFLOW,
    MAX_PREVIEW_BYTES,
    URL_TTL,
    CheckRecorded,
    CheckResult,
    PreviewAsk,
    PreviewSlot,
    SourceRead,
    check_workflow_id,
)
from scadbuddy.orders.storefront.listings import needs_s3, problem
from scadbuddy.orders.storefront.staged_files import poke, staged_backend
from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.s3 import STAGED_PUT_HEADERS, S3ContentBackend, staging_key
from scadbuddy.workflows.commands import execution_gone

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

logger = logging.getLogger(__name__)

#: The ``blob_refs`` holder of a checked file's preview.
PREVIEW_HOLDER: Final = "order_item_source"
#: The ledger route of a verdict.
CHECK_ROUTE: Final = "intake.check"
#: A read, a preview or a verdict for a source that no longer waits for a check.
NOT_CHECKING: Final = "not_checking"
SOURCE_NOT_FOUND: Final = "source_not_found"
#: The verdict of a ready check names a preview the worker never PUT.
PREVIEW_MISSING: Final = "preview_missing"


def preview_key(sha256: str) -> str:
    """The content store's key of a checked file's preview."""
    return f"customer-preview-{sha256}"


class Checker(Protocol):
    """Starts the files' checks (`TemporalChecker`; a test's fake)."""

    async def start(self, source_ids: Sequence[str]) -> None: ...

    async def running(self, source_id: str) -> bool | None: ...


class TemporalChecker:
    def __init__(self, client: Client, task_queue: str) -> None:
        self.client = client
        self.task_queue = task_queue

    async def start(self, source_ids: Sequence[str]) -> None:
        """One running check per source: a check already running is joined, and one that
        ended is started again (its read answers 409 for a source that no longer waits)."""
        for source_id in source_ids:
            await self.client.start_workflow(
                CHECK_WORKFLOW,
                source_id,
                id=check_workflow_id(source_id),
                task_queue=self.task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )

    async def running(self, source_id: str) -> bool | None:
        """Whether the source's check runs; None when Temporal does not answer."""
        try:
            described = await self.client.get_workflow_handle(
                check_workflow_id(source_id)
            ).describe()
        except RPCError as error:
            return False if execution_gone(error) else None
        return described.status == WorkflowExecutionStatus.RUNNING


def checker(state: AppState) -> Checker:
    """The app's checker: the operations' client, on the intake worker's queue."""
    from scadbuddy.operations.component import OPERATIONS

    ops = state.components.get(OPERATIONS)
    return TemporalChecker(ops.client, state.settings.temporal_task_queue_customer_files)


async def start_checks(state: AppState, source_ids: Sequence[str]) -> None:
    """Start the sources' checks; a failure is logged, never raised (the backstop)."""
    if source_ids:
        try:
            await checker(state).start(source_ids)
        except Exception:
            logger.warning("could not start the checks of %d customer files", len(source_ids))


# -- the worker's routes ----------------------------------------------------------------


def _backend(state: AppState) -> S3ContentBackend:
    backend = staged_backend(state)
    if backend is None:
        raise needs_s3()
    return backend


def _waiting(conn: Any, source_id: str, *, lock: bool = False) -> Any:
    """The file source, while it waits for its check: 404 for none, 409 for one that no
    longer waits (checked, or its staged file gone)."""
    row = conn.execute(
        "SELECT id, order_id, upload_id, filename, size, media_type, check_status"
        " FROM order_item_sources WHERE id = %s AND kind = 'file'"
        + (" FOR UPDATE" if lock else ""),
        (source_id,),
    ).fetchone()
    if row is None:
        raise problem(status.HTTP_404_NOT_FOUND, SOURCE_NOT_FOUND, "No such file.")
    if row["check_status"] != "checking" or row["upload_id"] is None:
        raise problem(
            status.HTTP_409_CONFLICT, NOT_CHECKING, "This file no longer waits for a check."
        )
    return row


def _read(state: AppState, source_id: str) -> Any:
    with state.projection.pool.connection() as conn:
        return _waiting(conn, source_id)


async def read_source(state: AppState, source_id: str) -> SourceRead:
    """``POST …/intake/sources/{id}/read``: a presigned GET of the staged file."""
    backend = _backend(state)
    row = await asyncio.to_thread(_read, state, source_id)
    key = staging_key(backend.settings.prefix, row["upload_id"])
    url = await backend.presign_staged_get(key, URL_TTL)
    return SourceRead(
        source_id=row["id"],
        filename=row["filename"],
        size=row["size"],
        media_type=row["media_type"],
        url=url,
    )


async def preview_slot(state: AppState, source_id: str, ask: PreviewAsk) -> PreviewSlot:
    """``POST …/intake/sources/{id}/preview``: a presigned PUT of the preview PNG, of
    exactly ``ask.size`` bytes."""
    backend = _backend(state)
    row = await asyncio.to_thread(_read, state, source_id)
    key = staging_key(backend.settings.prefix, row["upload_id"], preview=True)
    url = await backend.presign_put(key, size=ask.size, media_type="image/png", expires=URL_TTL)
    return PreviewSlot(url=url, headers={"Content-Type": "image/png", **STAGED_PUT_HEADERS})


async def _store_preview(
    state: AppState, backend: S3ContentBackend, upload_id: str, sha: str
) -> str:
    """The staged preview copied into the content store as opaque bytes; its key."""
    content = state.store.content
    assert content is not None
    key = preview_key(sha)
    stat = await asyncio.to_thread(content.index.get, key)
    if stat is not None and stat.ref.backend == content.name:
        return key
    staged = staging_key(backend.settings.prefix, upload_id, preview=True)
    data = await backend.read_staged(staged, MAX_PREVIEW_BYTES)
    if data is None:
        raise problem(
            status.HTTP_409_CONFLICT, PREVIEW_MISSING, "The check's preview was not uploaded."
        )
    await content.put(
        "media", io.BytesIO(data), name=f"{upload_id}.png", scope=BlobScope(), key=key
    )
    return key


async def record_check(
    state: AppState, source_id: str, key: str, result: CheckResult
) -> CheckRecorded:
    """``PUT …/intake/sources/{id}/check``: the verdict, once per ``key``. A ready file's
    preview is stored first (`_store_preview`); then, under the order's lock, the source
    takes its status, reason, facts, SHA-256 and preview, and the preview's hold. A source
    that stopped waiting meanwhile records nothing (``recorded`` false). After the commit
    the staged preview is deleted, the order's page told and the file's watcher woken."""
    backend = _backend(state)
    asked = result.model_dump(mode="json")
    first = await asyncio.to_thread(_read_any, state, source_id)
    stored: str | None = None
    if result.outcome == "ready" and first is not None and first["upload_id"] is not None:
        assert result.sha256 is not None
        stored = await _store_preview(state, backend, first["upload_id"], result.sha256)
    told: list[Any] = []

    def write() -> Replay:
        with state.projection.pool.connection() as conn:

            def apply() -> Replay:
                found = conn.execute(
                    "SELECT order_id FROM order_item_sources WHERE id = %s", (source_id,)
                ).fetchone()
                if found is None:
                    return Replay(200, {"recorded": False})
                order = conn.execute(
                    "SELECT id, status, submitted_at FROM orders WHERE id = %s FOR UPDATE",
                    (found["order_id"],),
                ).fetchone()
                try:
                    row = _waiting(conn, source_id, lock=True)
                except ApiError:
                    return Replay(200, {"recorded": False})
                if result.outcome == "ready" and stored is None:
                    return Replay(200, {"recorded": False})
                conn.execute(
                    "UPDATE order_item_sources SET check_status = %s, check_reason = %s,"
                    " facts = %s, sha256 = %s, preview_key = %s, updated_at = now()"
                    " WHERE id = %s",
                    (
                        result.outcome,
                        result.reason,
                        Jsonb(result.facts.model_dump(mode="json")) if result.facts else None,
                        result.sha256,
                        stored,
                        source_id,
                    ),
                )
                if stored is not None:
                    conn.execute(
                        "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
                        " ON CONFLICT DO NOTHING",
                        (stored, PREVIEW_HOLDER, source_id),
                    )
                told.append((order, row["upload_id"]))
                return Replay(200, {"recorded": True})

            return idempotent_write(conn, "intake", key, CHECK_ROUTE, asked, apply)

    done = await asyncio.to_thread(write)
    for order, upload_id in told:
        if stored is not None:
            await backend.remove_staged(
                staging_key(backend.settings.prefix, upload_id, preview=True)
            )
        emit(
            state.events,
            OrderChanged(
                order_id=order["id"], status=order["status"], cart=order["submitted_at"] is None
            ),
        )
        await poke(state, [upload_id], "checked")
    return CheckRecorded.model_validate(done.body)


def _read_any(state: AppState, source_id: str) -> Any:
    """The source's upload, read with no lock, or None."""
    with state.projection.pool.connection() as conn:
        return conn.execute(
            "SELECT upload_id FROM order_item_sources WHERE id = %s AND kind = 'file'",
            (source_id,),
        ).fetchone()
