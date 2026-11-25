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
import io
import logging
import threading
import time
import weakref
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from scadbuddy.core.paths import result_path
from scadbuddy.core.tracing import span
from scadbuddy.render.job_models import JobResult
from scadbuddy.store import BlobStore, PieceStateLostError
from scadbuddy.store.archive import (
    clear_marker,
    read_marker,
    staged_pack,
    unpack_dir,
    write_marker,
)
from scadbuddy.store.content import (
    BlobCorruptError,
    BlobMissingError,
    BlobRef,
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


def _member(archive: bytes, name: str) -> bytes | None:
    """The file ``name`` of a packed directory (`archive.pack_dir`); None when absent."""
    with zipfile.ZipFile(io.BytesIO(archive)) as packed:
        try:
            return packed.read(name)
        except KeyError:
            return None


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
        #: Held by the one `cached_bytes` walk that runs at a time (#6354), and the last
        #: walk's (monotonic time, total).
        self._walking = threading.Lock()
        self._walked: tuple[float, int] | None = None

    # --- phase 1's BlobStore, over the local cache ---------------------------

    @property
    def root(self) -> Path:
        return self.local.root

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

    async def claim(self, key: str) -> bool:
        # Touched before the row is read: a sweep whose `delete_if_stale` comes after
        # the read finds it fresh, and one that came before leaves no row to find. A
        # row on another backend (mid `/store/migrate`) is not this store's blob, as
        # for `_row`. A caller that then publishes from nothing (`orders/attachments.py`
        # `keep`) loses the swap against that row and fails as stale, as a fetch would
        # have: it succeeds again once the migration has flipped `store_backend`.
        # `stat` asks the backend whether the object is there (a HEAD, never a
        # download) and forgets a row whose object was lost, so that is written again too.
        await self.content.touch(key)
        return await self.content.stat(key) is not None

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
        None when nothing (of this backend) is stored. A span says hit or miss (#7318)."""
        with span("blob_cache.fetch", attributes={"scadbuddy.blob_key": key}) as current:
            stat = await asyncio.to_thread(self._row, key)
            if stat is None:
                current.set_attribute("scadbuddy.blob_found", False)
                return None
            directory = self.local.dir_for(key)
            hit = await asyncio.to_thread(read_marker, directory) == stat.ref.sha256
            current.set_attribute("scadbuddy.cache.hit", hit)
            if hit:
                self._cache("hit")
            else:
                self._cache("miss")
                data = await self._read(key, stat.ref)
                if data is None:
                    return None
                await asyncio.to_thread(unpack_dir, data, directory, sha256=stat.ref.sha256)
                await self._trim()
            # Claimed: the sweep's `delete_if_stale` now skips it (see `sweep_content`).
            await self.content.touch(key)
            return stat.ref.sha256

    async def _read(self, key: str, ref: BlobRef) -> bytes | None:
        """The stored blob's bytes; None, and its row forgotten, when it is gone or
        altered in the backend (the next render stores it again)."""
        try:
            return await self.content.read(ref)
        except (BlobMissingError, BlobCorruptError) as error:
            logger.warning(
                "a stored piece is gone or altered; it will be rendered again",
                extra={"key": key, "error": repr(error)},
            )
            await self.content.forget(key)
            return None

    async def read_file(self, key: str, name: str) -> bytes | None:
        """One file of the blob ``key``, whole (#7318). A hit reads the cached copy; a
        miss reads the stored blob in memory, as `fetch` does, but unpacks nothing onto
        the cache, so a small file read now and then (a preset document, a plugin zip)
        costs the cache no directory and no write."""
        with span("blob_cache.read_file", attributes={"scadbuddy.blob_key": key}) as current:
            stat = await asyncio.to_thread(self._row, key)
            if stat is None:
                current.set_attribute("scadbuddy.blob_found", False)
                return None
            directory = self.local.root / key  # not dir_for: a read makes no directory
            sha = stat.ref.sha256
            if await asyncio.to_thread(read_marker, directory) == sha:
                try:
                    data = await asyncio.to_thread((directory / name).read_bytes)
                except OSError:
                    # An eviction clears the marker before it removes the files: with the
                    # marker still there, the blob has no such file. Else read the store.
                    if await asyncio.to_thread(read_marker, directory) == sha:
                        current.set_attribute("scadbuddy.cache.hit", True)
                        return None
                else:
                    current.set_attribute("scadbuddy.cache.hit", True)
                    self._cache("hit")
                    return data
            current.set_attribute("scadbuddy.cache.hit", False)
            self._cache("miss")
            archive = await self._read(key, stat.ref)
            if archive is None:
                return None
            return await asyncio.to_thread(_member, archive, name)

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
        async with staged_pack(directory) as data:
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

    def cached_bytes(self, *, max_age: float = 0) -> int | None:
        """The bytes in the cache: a walk of every file in it. A walk at most `max_age`
        seconds old answers instead, and a call while another walks answers the last
        total, or None before the first (#6354). In the API the cache was on the shared
        `/data` NFS volume (its default, unless `SCADBUDDY_BLOB_CACHE_DIR` moves it,
        #7318), where one walk took 13 s (19,340 files, 2026-11-15): the
        `/metrics` scrape, every 30 s from each of two Prometheus replicas with a 10 s
        timeout, started one each time, and on a slow node they piled up until the
        default executor had no thread for `/healthz` and liveness killed the pod."""
        walked = self._walked
        if walked is not None and max_age and time.monotonic() - walked[0] < max_age:
            return walked[1]
        if not self._walking.acquire(blocking=False):
            return walked[1] if walked is not None else None
        try:
            cached = self.local.keys()  # a list of blob keys, not a dict view
            total = sum(_size(self.local.root / key) for key in cached)
            self._walked = (time.monotonic(), total)
            return total
        finally:
            self._walking.release()

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


def result_file(blobs: BlobStore, data_root: Path, name: str) -> Path:
    """Where the file a `JobResult` names is in this process: ``blobs/<key>/<file>`` under
    ``blobs``' root, wherever its cache is (#7318); anything else under ``data_root``."""
    return result_path(name, blobs_root=blobs.root, data_root=data_root)


async def materialize_result(blobs: BlobStore, result: JobResult | None) -> None:
    """Pull a finished job's piece into this process's cache before its files are read
    (`JobResult` paths are `blobs/<key>/<file>`; `result_file` finds them).

    The routes that read them (preview, plates, view, colours, a save, a quote) need
    a path or read several files of one piece, request after request, so the piece is
    unpacked onto the cache once rather than read from the store each time (#7318)."""
    if result is None:
        return
    for rel in {result.model_3mf, result.preview_glb}:
        parts = Path(rel).parts
        if len(parts) >= 3 and parts[0] == "blobs":
            await blobs.fetch(parts[1])
