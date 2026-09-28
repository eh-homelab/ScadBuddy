"""The byte store under the blob store (spec 2026-09-27 §6.2).

Phase 1's `BlobStore` is directory-shaped: a piece renders into `dir_for(key)`. This is
§6.2's byte-stream interface, named `ContentStore` because `BlobStore` is taken:
`put/get/stat/delete/list` over a backend, plus the Postgres index that makes a fetch
by id, the caps and the usage O(1). `store/cache.py` puts the directory shape back on
top for processes that share no volume. `put` takes bytes, not a stream: every caller
holds the blob in memory already (a packed directory, a sanitised upload).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from scadbuddy.store.content_models import (
    SWEPT_KINDS,
    BlobCorruptError,
    BlobKind,
    BlobMissingError,
    BlobRef,
    BlobScope,
    BlobStat,
    RefusedDeleteError,
    StoreFullError,
    StoreUsage,
)
from scadbuddy.store.index import BlobIndex
from scadbuddy.store.refs import BlobRefs

if TYPE_CHECKING:
    from scadbuddy.core.metrics import Metrics

logger = logging.getLogger(__name__)


class ContentBackend(Protocol):
    backend: str

    async def upload(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> str: ...
    def download(self, backend_id: str) -> AsyncIterator[bytes]: ...
    async def exists(self, backend_id: str) -> bool: ...
    async def remove(self, backend_id: str) -> None: ...


def template_title(model_dir: Path, fallback: str) -> str:
    """A template's display name from its `model.json`, for its Bambuddy folder."""
    try:
        name = json.loads((model_dir / "model.json").read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError, AttributeError):
        return fallback
    return name.strip() if isinstance(name, str) and name.strip() else fallback


class ContentStore:
    def __init__(
        self,
        backend: ContentBackend,
        index: BlobIndex,
        *,
        max_total_bytes: int = 0,
        max_count: int = 0,
        metrics: Metrics | None = None,
    ) -> None:
        self.backend = backend
        self.index = index
        self.max_total_bytes = max_total_bytes
        self.max_count = max_count
        self.metrics = metrics

    @property
    def name(self) -> str:
        return self.backend.backend

    def _count(self, op: str, outcome: str) -> None:
        if self.metrics is not None:
            self.metrics.store_ops.labels(op, outcome).inc()

    def usage(self) -> StoreUsage:
        count, total, by_kind = self.index.usage(self.name)
        return StoreUsage(
            backend=self.name,
            count=count,
            bytes=total,
            max_count=self.max_count,
            max_total_bytes=self.max_total_bytes,
            by_kind=by_kind,
        )

    def _require_room(self, size: int) -> None:
        usage = self.usage()
        if self.max_count and usage.count + 1 > self.max_count:
            raise StoreFullError(
                f"the store holds {usage.count} blobs, the most SCADBUDDY_STORE_MAX_COUNT"
                f" ({self.max_count}) allows; unreferenced blobs go after the sweep's grace"
            )
        if self.max_total_bytes and usage.bytes + size > self.max_total_bytes:
            raise StoreFullError(
                f"storing {size} bytes would take the store to {usage.bytes + size} bytes,"
                f" past SCADBUDDY_STORE_MAX_TOTAL_BYTES ({self.max_total_bytes})"
            )

    async def _store(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> BlobRef:
        sha = hashlib.sha256(data).hexdigest()
        existing = await asyncio.to_thread(self.index.by_sha, sha, kind, self.name)
        if existing is not None and await self.backend.exists(existing.backend_id):
            return existing  # a re-put: never refused, never uploaded twice
        try:
            await asyncio.to_thread(self._require_room, len(data))
        except StoreFullError:
            self._count("put", "full")
            raise
        backend_id = await self.backend.upload(kind, data, name=name, scope=scope)
        self._count("put", "ok")
        return BlobRef(
            sha256=sha, kind=kind, backend=self.name, backend_id=backend_id, size=len(data)
        )

    async def _release(self, ref: BlobRef) -> None:
        """Remove the object unless an index row still names it."""
        if not await asyncio.to_thread(self.index.shares_backend_id, ref.backend, ref.backend_id):
            await self.backend.remove(ref.backend_id)
            self._count("delete", "ok")

    async def put(
        self,
        kind: BlobKind,
        data: bytes,
        *,
        name: str,
        scope: BlobScope,
        key: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> BlobRef:
        ref = await self._store(kind, data, name=name, scope=scope)
        key = key or f"{kind}-{ref.sha256}"
        previous = await asyncio.to_thread(self.index.get, key)
        await asyncio.to_thread(self.index.put, key, ref, slug=scope.slug, meta=meta or {})
        if previous is not None and previous.ref.backend_id != ref.backend_id:
            await self._release(previous.ref)
        return ref

    async def replace(
        self,
        key: str,
        kind: BlobKind,
        data: bytes,
        *,
        name: str,
        scope: BlobScope,
        expected: str | None,
        meta: dict[str, Any] | None = None,
    ) -> BlobRef | None:
        """`put` under ``key`` only if the key still names ``expected``; None if it
        moved on, and this call's own upload is removed again."""
        ref = await self._store(kind, data, name=name, scope=scope)
        previous = await asyncio.to_thread(self.index.get, key)
        landed = await asyncio.to_thread(
            self.index.swap, key, ref, expected=expected, slug=scope.slug, meta=meta or {}
        )
        if not landed:
            await self._release(ref)
            return None
        if previous is not None and previous.ref.backend_id != ref.backend_id:
            await self._release(previous.ref)
        return ref

    async def get(self, ref: BlobRef) -> AsyncIterator[bytes]:
        digest = hashlib.sha256()
        try:
            async for chunk in self.backend.download(ref.backend_id):
                digest.update(chunk)
                yield chunk
        except BlobMissingError:
            self._count("get", "missing")
            raise
        if digest.hexdigest() != ref.sha256:
            self._count("get", "corrupt")
            raise BlobCorruptError(f"{ref.backend}:{ref.backend_id} is not sha256 {ref.sha256}")
        self._count("get", "ok")

    async def read(self, ref: BlobRef) -> bytes:
        return b"".join([chunk async for chunk in self.get(ref)])

    async def stat(self, key: str) -> BlobStat | None:
        stat = await asyncio.to_thread(self.index.get, key)
        if stat is None:
            return None
        if not await self.backend.exists(stat.ref.backend_id):
            await self.forget(key)
            return None
        return stat

    async def forget(self, key: str) -> None:
        await asyncio.to_thread(self.index.delete, key)

    async def delete(self, key: str) -> None:
        stat = await asyncio.to_thread(self.index.delete, key)
        if stat is not None:
            await self._release(stat.ref)

    async def delete_if_stale(self, key: str, cutoff: datetime) -> bool:
        stat = await asyncio.to_thread(self.index.delete_if_stale, key, cutoff)
        if stat is None:
            return False
        await self._release(stat.ref)
        return True

    async def list(self, scope: BlobScope) -> AsyncIterator[BlobStat]:
        for stat in await asyncio.to_thread(self.index.stats, None, scope.slug):
            yield stat


async def sweep_content(
    content: ContentStore, refs: BlobRefs, *, grace: float, now: float | None = None
) -> list[str]:
    """Remove every swept-kind blob nothing references and nothing touched for ``grace``."""
    cutoff = datetime.fromtimestamp((time.time() if now is None else now) - grace, UTC)
    kept = await asyncio.to_thread(refs.referenced)
    removed: list[str] = []
    for stat in await asyncio.to_thread(content.index.stats, SWEPT_KINDS):
        if stat.key in kept or stat.touched_at > cutoff:
            continue
        try:
            if await content.delete_if_stale(stat.key, cutoff):
                removed.append(stat.key)
        except RefusedDeleteError:
            # The index row is gone; the object is outside a Work/ folder and stays.
            # One refused key must not stop the rest of the sweep.
            logger.exception("the backend refused to delete a swept blob", extra={"key": stat.key})
    return removed


__all__ = [
    "SWEPT_KINDS",
    "BlobCorruptError",
    "BlobKind",
    "BlobMissingError",
    "BlobRef",
    "BlobScope",
    "BlobStat",
    "ContentBackend",
    "ContentStore",
    "RefusedDeleteError",
    "StoreFullError",
    "StoreUsage",
    "sweep_content",
    "template_title",
]
