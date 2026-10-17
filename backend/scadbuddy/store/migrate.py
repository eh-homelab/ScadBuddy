"""Recorded blobs moved from one content backend to another (spec 2026-10-16 §4.3):
each object copied, checked against its row's sha256, and its row swapped over."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from collections.abc import Callable, Collection
from contextlib import AsyncExitStack, aclosing
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
from scadbuddy.store.content_models import BlobMissingError, BlobRef, BlobScope, BlobStat
from scadbuddy.store.index import BlobIndex, Pool

logger = logging.getLogger(__name__)

MIGRATE_BATCH = "store_migrate_batch"


class MigrationBatch(BaseModel):
    moved: int
    #: Rows still on the source, but those skipped and those that failed.
    remaining: int
    failed: list[str]


class MigrateBatchInput(BaseModel):
    source: StoreBackend
    target: StoreBackend
    limit: int
    #: Keys an earlier batch could not move: left where they are.
    skip: list[str] = []


async def migrate_batch(
    index: BlobIndex,
    source: ContentBackend,
    target: ContentBackend,
    *,
    limit: int,
    heartbeat: Callable[[str], None],
    skip: Collection[str] = (),
) -> MigrationBatch:
    """Move up to ``limit`` of ``source``'s rows, least recently touched first, to
    ``target``. A row whose object is gone or not its sha256, whose copy the target
    refused, or that moved on meanwhile (its swap missed) is ``failed`` and stays."""
    rows = await asyncio.to_thread(index.oldest, source.backend, limit=limit, skip=list(skip))
    moved, failed = 0, []
    for row in rows:
        heartbeat(row.key)
        if await _move(index, source, target, row):
            moved += 1
        else:
            failed.append(row.key)
    remaining = await asyncio.to_thread(index.count, source.backend, skip=[*skip, *failed])
    return MigrationBatch(moved=moved, remaining=remaining, failed=failed)


async def _move(
    index: BlobIndex, source: ContentBackend, target: ContentBackend, row: BlobStat
) -> bool:
    ref = row.ref
    with await asyncio.to_thread(lambda: tempfile.TemporaryFile("w+b")) as spool:
        digest = hashlib.sha256()
        try:
            async with aclosing(source.download(ref.backend_id)) as chunks:
                async for chunk in chunks:
                    digest.update(chunk)
                    await asyncio.to_thread(spool.write, chunk)
        except BlobMissingError:
            logger.warning("a blob to move is missing", extra={"key": row.key})
            return False
        if digest.hexdigest() != ref.sha256:
            logger.warning("a blob to move is not its sha256", extra={"key": row.key})
            return False
        spool.seek(0)
        try:
            backend_id = await target.upload(
                ref.kind, spool, name=row.key, scope=BlobScope(slug=row.slug)
            )
        except Exception:
            logger.warning("the target refused a blob", extra={"key": row.key}, exc_info=True)
            return False
    moved = BlobRef(
        sha256=ref.sha256,
        kind=ref.kind,
        backend=target.backend,
        backend_id=backend_id,
        size=ref.size,
    )
    return await asyncio.to_thread(
        index.swap, row.key, moved, expected=ref, slug=row.slug, meta=row.meta
    )


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
                skip=params.skip,
                heartbeat=activity.heartbeat,
            )

    return store_migrate_batch
