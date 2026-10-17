"""Recorded blobs copied from one content backend to another (spec 2026-10-16 §4.3):
each object copied, checked against its row's sha256, and the copy recorded beside the
row (`migrated_backend`). The row keeps naming the source, which the running processes
still read, until a start on the target switches it (`BlobIndex.switch_migrated`)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import tempfile
from collections.abc import Callable
from contextlib import AsyncExitStack, aclosing
from datetime import datetime
from time import monotonic
from typing import Any

from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.core.config import StoreBackend
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import StoreNotReadyError, load_render_store_settings
from scadbuddy.store.backends import build_backend
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.content import ContentBackend
from scadbuddy.store.content_models import BlobScope, BlobStat
from scadbuddy.store.index import BlobIndex, Pool

logger = logging.getLogger(__name__)

MIGRATE_BATCH = "store_migrate_batch"
#: The most heartbeats a batch sends per second, whatever its chunks and parts.
HEARTBEAT_INTERVAL = 1.0
#: What a row keeps of the error that stopped its copy.
ERROR_CHARS = 500


class MigrationBatch(BaseModel):
    moved: int
    #: Rows still to copy in this run: not those whose copy failed since it started.
    remaining: int
    #: This batch's keys whose copy failed; each row says why (`migrated_error`).
    failed: list[str]


class MigrateBatchInput(BaseModel):
    source: StoreBackend
    target: StoreBackend
    limit: int
    #: The run's start, by the database's clock: a row whose copy failed before it is
    #: tried again.
    started: datetime


class _Coalesced:
    """``beat(key)`` at most once per `HEARTBEAT_INTERVAL`."""

    def __init__(self, beat: Callable[[str], None]) -> None:
        self._beat = beat
        self._last: float | None = None

    def __call__(self, key: str) -> None:
        now = monotonic()
        if self._last is None or now - self._last >= HEARTBEAT_INTERVAL:
            self._last = now
            self._beat(key)


class _Beating(io.BufferedRandom):
    """A spool that heartbeats on each read, so an upload a part at a time does."""

    def __init__(self, raw: io.RawIOBase, beat: Callable[[], None]) -> None:
        super().__init__(raw)
        self._beating = beat

    def read(self, size: int | None = -1, /) -> bytes:
        self._beating()
        return super().read(size)


async def migrate_batch(
    index: BlobIndex,
    source: ContentBackend,
    target: ContentBackend,
    *,
    limit: int,
    started: datetime,
    heartbeat: Callable[[str], None],
) -> MigrationBatch:
    """Copy up to ``limit`` of ``source``'s rows not yet on ``target``, least recently
    touched first, but those whose copy failed since ``started``. A row whose object
    cannot be read or is not its sha256, or whose copy the target refused, records the
    error and is ``failed``; one that moved on meanwhile (its mark missed) is left for
    the next batch. ``heartbeat`` is called with the key per chunk and per read of the
    copy, at most once a second."""
    beat = _Coalesced(heartbeat)
    rows = await asyncio.to_thread(
        index.unmigrated, source.backend, target.backend, limit=limit, started=started
    )
    moved, failed = 0, []
    for row in rows:
        beat(row.key)
        error = await _copy(index, source, target, row, beat)
        if error is None:
            moved += 1
        elif error:
            failed.append(row.key)
            await asyncio.to_thread(
                index.mark_migration_failed, row.key, row.ref, error[:ERROR_CHARS]
            )
    remaining, _, _ = await asyncio.to_thread(
        index.migration_counts, source.backend, target.backend, started=started
    )
    return MigrationBatch(moved=moved, remaining=remaining, failed=failed)


def _error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


async def _copy(
    index: BlobIndex,
    source: ContentBackend,
    target: ContentBackend,
    row: BlobStat,
    beat: Callable[[str], None],
) -> str | None:
    """None once copied and recorded, "" when the row moved on, else the error."""
    ref = row.ref
    # Closed by the `_Beating` that wraps it.
    raw = await asyncio.to_thread(lambda: tempfile.TemporaryFile("w+b", buffering=0))  # noqa: SIM115
    assert isinstance(raw, io.RawIOBase)
    with _Beating(raw, lambda: beat(row.key)) as spool:
        digest = hashlib.sha256()
        try:
            async with aclosing(source.download(ref.backend_id)) as chunks:
                async for chunk in chunks:
                    beat(row.key)
                    digest.update(chunk)
                    await asyncio.to_thread(spool.write, chunk)
        except Exception as error:
            # Missing, unreadable or refused: one bad object never fails the batch.
            logger.warning("could not read a blob to copy", extra={"key": row.key}, exc_info=True)
            return _error(error)
        if digest.hexdigest() != ref.sha256:
            logger.warning("a blob to copy is not its sha256", extra={"key": row.key})
            return f"sha256 mismatch: the source object is {digest.hexdigest()}"
        await asyncio.to_thread(spool.seek, 0)
        try:
            backend_id = await target.upload(
                ref.kind, spool, name=row.key, scope=BlobScope(slug=row.slug)
            )
        except Exception as error:
            logger.warning("the target refused a blob", extra={"key": row.key}, exc_info=True)
            return _error(error)
    marked = await asyncio.to_thread(
        index.mark_migrated, row.key, ref, backend=target.backend, backend_id=backend_id
    )
    return None if marked else ""


def migrate_activity(pool: Pool, defaults: Settings) -> Callable[..., Any]:
    """`store_migrate_batch`, in the API process (its `library` worker): both backends
    built from the settings as they are now, so the target's are saved before
    `store_backend` names it."""

    @activity.defn(name=MIGRATE_BATCH)
    async def store_migrate_batch(params: MigrateBatchInput) -> MigrationBatch:
        current = await asyncio.to_thread(load_render_store_settings, pool, defaults)
        settings = RenderSettingsSource(pool, defaults)
        settings.seed(current)
        async with AsyncExitStack() as stack:
            try:
                source = build_backend(params.source, current, settings, pool)
                stack.push_async_callback(source.aclose)
                target = build_backend(params.target, current, settings, pool)
                stack.push_async_callback(target.aclose)
            except StoreNotReadyError as error:
                # Not worth retrying until Settings change: the run ends and says why.
                raise ApplicationError(str(error), non_retryable=True) from error
            return await migrate_batch(
                BlobIndex(pool),
                source,
                target,
                limit=params.limit,
                started=params.started,
                heartbeat=activity.heartbeat,
            )

    return store_migrate_batch
