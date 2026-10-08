"""A process's local cache in front of a remote `ContentStore`, in phase 1's
directory shape (spec 2026-09-27 §6.2).

Each render stage runs in `dir_for(key)` exactly as on the shared volume; `publish`
packs the directory into one blob when a stage ends, `fetch` unpacks it on whichever
worker the next stage lands on. A directory whose marker matches the index is a hit and
costs one index read. The marker means "exactly the published bytes": a stage about to
write removes it (`checkout`) and only an unpack or a successful publish writes it, so a
directory a stage changed but never published is a miss, never an answer. Sticky
scheduling would make every fetch a hit; nothing depends on it. Publishing is a
compare-and-swap on the sha the stage checked out, so an attempt Temporal has already
retried elsewhere cannot overwrite its successor.

The per-key lock covers `fetch` and `checkout`, not a stage's use of the directory.
While a stage writes, the directory has no marker, so a `fetch` of the same key in the
same process is a miss and swaps the directory out from under the stage: a key must
not be fetched while a stage of that key is writing it. One `RenderPiece` per key
keeps that true, except for a retry overlapping a zombie attempt on the same worker,
where the zombie loses and the retry works on fresh state.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import weakref
from pathlib import Path
from typing import TYPE_CHECKING

from scadbuddy.render.job_models import JobResult
from scadbuddy.store import BlobStore, PieceStateLostError
from scadbuddy.store.archive import clear_marker, pack_dir, read_marker, unpack_dir, write_marker
from scadbuddy.store.content import (
    BlobCorruptError,
    BlobMissingError,
    BlobScope,
    BlobStat,
    ContentStore,
)
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
        #: One per key being fetched: a second miss on it waits, then finds a hit.
        self._fetching: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        #: Held by the one eviction pass that runs at a time, whatever started it (a
        #: write's trim or the periodic pass): another finds it held and skips.
        self._evicting = threading.Lock()

    # --- phase 1's BlobStore, over the local cache ---------------------------

    def dir_for(self, key: str) -> Path:
        return self.local.dir_for(key)

    def exists(self, key: str) -> bool:
        """Synchronous: it reads the index. Call it from a thread (as the sweep does),
        never from the event loop."""
        return self.local.exists(key) or self._row(key) is not None

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

    def _row(self, key: str) -> BlobStat | None:
        """The index row, when it is this store's backend's (as `ContentStore.stat`)."""
        stat = self.content.index.get(key)
        return stat if stat is not None and stat.ref.backend == self.content.name else None

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._fetching.get(key)
        if lock is None:
            lock = self._fetching[key] = asyncio.Lock()
        return lock

    async def fetch(self, key: str) -> bool:
        async with self._lock(key):
            return await self._fetch(key) is not None

    async def checkout(self, key: str) -> str | None:
        async with self._lock(key):
            sha = await self._fetch(key)
            if sha is None:
                raise PieceStateLostError(key)
            await asyncio.to_thread(clear_marker, self.local.dir_for(key))
            return sha

    async def checkout_fresh(self, key: str) -> str | None:
        async with self._lock(key):
            await asyncio.to_thread(clear_marker, self.local.dir_for(key))
            return await self.indexed_sha(key)

    async def _fetch(self, key: str) -> str | None:
        """Under the key's lock: make the directory hold the stored blob; its sha, or
        None when nothing (of this backend) is stored."""
        stat = await asyncio.to_thread(self._row, key)
        if stat is None:
            return None
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
                return None
            await asyncio.to_thread(unpack_dir, data, directory, sha256=stat.ref.sha256)
            await self._trim()
        # Claimed: the sweep's `delete_if_stale` now skips it (see `sweep_content`).
        await self.content.touch(key)
        return stat.ref.sha256

    async def publish(self, key: str, *, scope: BlobScope) -> None:
        directory = self.local.dir_for(key)
        await self._publish(key, scope, expected=read_marker(directory))

    async def indexed_sha(self, key: str) -> str | None:
        stat = await asyncio.to_thread(self._row, key)
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
        await self._trim()

    async def _trim(self) -> None:
        """After a write grew the cache: evict down to `max_bytes` now, in a thread and
        one pass at a time, so the cap holds between the periodic passes (#689), which
        stay as a backstop."""
        try:
            removed = await asyncio.to_thread(self.evict)
            if removed:
                logger.info("evicted cached pieces", extra={"count": len(removed)})
        except Exception:
            logger.exception("could not evict the piece cache")

    def cached_bytes(self) -> int:
        cached = self.local.keys()  # a list of blob keys, not a dict view
        return sum(_size(self.local.root / key) for key in cached)

    def evict(self, *, now: float | None = None) -> list[str]:
        """Least recently used first, down to `max_bytes`: any directory not touched
        within `min_age`. That includes one never published (a render that crashed) and a
        dot-named `unpack_dir` staging directory a crash left; one in use is recent, as
        rendering and unpacking both touch it. One pass at a time: a call while another
        runs removes nothing."""
        if not self._evicting.acquire(blocking=False):
            return []
        try:
            return self._evict(now)
        finally:
            self._evicting.release()

    def _evict(self, now: float | None) -> list[str]:
        cutoff = (time.time() if now is None else now) - self.min_age
        entries = []
        cached = self.local.keys()  # a list of blob keys, not a dict view
        for key in cached:
            directory = self.local.root / key  # not dir_for: that would touch it
            try:
                mtime = directory.stat().st_mtime
            except FileNotFoundError:
                continue
            entries.append((mtime, key, _size(directory)))
        total = sum(size for _, _, size in entries)
        removed: list[str] = []
        for mtime, key, size in sorted(entries):
            if total <= self.max_bytes:
                break
            if mtime > cutoff:
                continue
            try:  # re-read: a `dir_for` or `unpack_dir` since the scan touches it first
                if (self.local.root / key).stat().st_mtime > cutoff:
                    continue
            except FileNotFoundError:
                total -= size  # gone already
                continue
            # #1271: the marker goes first, so a removal cut short is never a hit.
            clear_marker(self.local.root / key)
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
