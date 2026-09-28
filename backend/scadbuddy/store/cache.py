"""A process's local cache in front of a remote `ContentStore`, in phase 1's
directory shape (spec 2026-09-27 §6.2).

Each render stage runs in `dir_for(key)` exactly as on the shared volume; `publish`
packs the directory into one blob when a stage ends, `fetch` unpacks it on whichever
worker the next stage lands on. A directory whose marker matches the index is a hit and
costs one index read. Sticky scheduling would make every fetch a hit; nothing depends
on it. Publishing is a compare-and-swap on the sha the directory was fetched at, so an
attempt Temporal has already retried elsewhere cannot overwrite its successor.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from scadbuddy.render.job_models import JobResult
from scadbuddy.store import BlobStore
from scadbuddy.store.archive import pack_dir, read_marker, unpack_dir, write_marker
from scadbuddy.store.content import BlobCorruptError, BlobMissingError, BlobScope, ContentStore
from scadbuddy.store.local import LocalBlobStore

if TYPE_CHECKING:
    from scadbuddy.core.metrics import Metrics

logger = logging.getLogger(__name__)


class StaleBlobError(RuntimeError):
    """The key moved on since this process fetched it. The activity fails; Temporal
    retries it, and the retry fetches the newer state."""


def _size(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


class CachedBlobStore:
    def __init__(
        self,
        local: LocalBlobStore,
        content: ContentStore,
        *,
        max_bytes: int,
        min_age: float,
        metrics: Metrics | None = None,
    ) -> None:
        self.local = local
        self.content = content
        #: The cache's cap (SCADBUDDY_WORKER_CACHE_MAX_BYTES).
        self.max_bytes = max_bytes
        #: Never evict a directory touched this recently: an activity may be writing it.
        #: The wiring passes `Config.activity_timeout`, the longest an attempt can live.
        self.min_age = min_age
        self.metrics = metrics
        self.backend = content.name

    # --- phase 1's BlobStore, over the local cache ---------------------------

    def dir_for(self, key: str) -> Path:
        return self.local.dir_for(key)

    def exists(self, key: str) -> bool:
        return self.local.exists(key) or self.content.index.get(key) is not None

    def remove(self, key: str) -> None:
        self.local.remove(key)

    def keys(self) -> list[str]:
        return self.local.keys()

    def touched_at(self, key: str) -> float:
        return self.local.touched_at(key)

    # --- the remote half -----------------------------------------------------

    def _cache(self, result: str) -> None:
        if self.metrics is not None:
            self.metrics.worker_cache.labels(result).inc()

    async def fetch(self, key: str) -> bool:
        stat = await asyncio.to_thread(self.content.index.get, key)
        if stat is None:
            return False
        directory = self.local.dir_for(key)
        if read_marker(directory) == stat.ref.sha256:
            self._cache("hit")
        else:
            self._cache("miss")
            try:
                data = await self.content.read(stat.ref)
            except (BlobMissingError, BlobCorruptError) as error:
                logger.warning(
                    "a stored piece is gone or altered; it will be rendered again",
                    extra={"key": key, "error": repr(error)},
                )
                await self.content.forget(key)
                return False
            await asyncio.to_thread(unpack_dir, data, directory, sha256=stat.ref.sha256)
        # Claimed: the sweep's `delete_if_stale` now skips it (see `sweep_content`).
        await self.content.touch(key)
        return True

    async def publish(self, key: str, *, scope: BlobScope) -> None:
        directory = self.local.dir_for(key)
        await self._publish(key, scope, expected=read_marker(directory))

    async def indexed_sha(self, key: str) -> str | None:
        stat = await asyncio.to_thread(self.content.index.get, key)
        return stat.ref.sha256 if stat is not None else None

    async def publish_fresh(self, key: str, *, scope: BlobScope, expected: str | None) -> None:
        await self._publish(key, scope, expected=expected)

    async def _publish(self, key: str, scope: BlobScope, *, expected: str | None) -> None:
        directory = self.local.dir_for(key)
        data = await asyncio.to_thread(pack_dir, directory)
        ref = await self.content.replace(
            key, "piece", data, name=f"piece-{key}.zip", scope=scope, expected=expected
        )
        if ref is None:
            raise StaleBlobError(f"piece {key} was published by a later attempt")
        await asyncio.to_thread(write_marker, directory, ref.sha256)

    def cached_bytes(self) -> int:
        cached = self.local.keys()  # a list of blob keys, not a dict view
        return sum(_size(self.local.root / key) for key in cached)

    def evict(self, *, now: float | None = None) -> list[str]:
        """Least recently used first, down to `max_bytes`; only published directories
        (a marker) not touched within `min_age`."""
        cutoff = (time.time() if now is None else now) - self.min_age
        entries = []
        cached = self.local.keys()  # a list of blob keys, not a dict view
        for key in cached:
            if key.startswith("."):
                continue  # an `unpack_dir` staging directory, gone in a moment
            directory = self.local.root / key  # not dir_for: that would touch it
            try:
                mtime = directory.stat().st_mtime
            except FileNotFoundError:
                continue
            entries.append((mtime, key, _size(directory), read_marker(directory)))
        total = sum(size for _, _, size, _ in entries)
        removed: list[str] = []
        for mtime, key, size, marker in sorted(entries):
            if total <= self.max_bytes:
                break
            if marker is None or mtime > cutoff:
                continue
            self.local.remove(key)
            total -= size
            removed.append(key)
        return removed


async def materialize_result(blobs: BlobStore, result: JobResult | None) -> None:
    """Pull a finished job's piece into this process's cache before its files are read
    (`JobResult` paths are `blobs/<key>/<file>`, relative to the data dir)."""
    if result is None:
        return
    for rel in {result.model_3mf, result.preview_glb}:
        parts = Path(rel).parts
        if len(parts) >= 3 and parts[0] == "blobs":
            await blobs.fetch(parts[1])
