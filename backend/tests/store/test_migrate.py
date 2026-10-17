"""Moving recorded blobs from one backend to another (spec 2026-10-16 §4.3)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.content import BlobMissingError, BlobScope, ContentStore
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.local import LocalContentBackend
from scadbuddy.store.migrate import MigrationBatch, migrate_batch
from scadbuddy.store.s3 import S3ContentBackend
from tests.support.store import local_content, overwrite_object, s3_content

pytestmark = pytest.mark.requires_postgres
SCOPE = BlobScope(slug="demo", title="Demo")


@pytest.fixture
def target(s3: S3Settings) -> Iterator[S3ContentBackend]:
    yield S3ContentBackend(s3)


async def _bytes(backend: S3ContentBackend, backend_id: str) -> bytes:
    return b"".join([chunk async for chunk in backend.download(backend_id)])


async def test_migrate_batch_moves_rows_and_bytes(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    src = local_content(tmp_path / "a", pool)
    ref = await src.put("piece", b"zip", name="p.zip", scope=SCOPE, meta={"x": 1})
    beats: list[str] = []
    try:
        result = await migrate_batch(
            BlobIndex(pool), src.backend, target, limit=10, heartbeat=beats.append
        )
        assert result == MigrationBatch(moved=1, remaining=0, failed=[])
        assert beats == [f"piece-{ref.sha256}"]
        stat = BlobIndex(pool).get(f"piece-{ref.sha256}")
        assert stat is not None
        assert stat.ref.backend == "s3" and stat.ref.sha256 == ref.sha256
        assert (stat.slug, stat.meta) == ("demo", {"x": 1})
        assert await _bytes(target, stat.ref.backend_id) == b"zip"
    finally:
        await target.aclose()


async def test_a_batch_takes_the_oldest_rows_up_to_its_limit_and_skips(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    src = local_content(tmp_path / "a", pool)
    refs = [await src.put("asset", data, name="a", scope=SCOPE) for data in (b"1", b"2", b"3")]
    keys = [f"asset-{ref.sha256}" for ref in refs]
    index = BlobIndex(pool)
    try:
        first = await migrate_batch(
            index, src.backend, target, limit=1, skip=[keys[0]], heartbeat=lambda _: None
        )
        assert first == MigrationBatch(moved=1, remaining=1, failed=[])
        assert [index.get(key).ref.backend for key in keys] == ["local", "s3", "local"]  # type: ignore[union-attr]
        rest = await migrate_batch(index, src.backend, target, limit=5, heartbeat=lambda _: None)
        assert rest == MigrationBatch(moved=2, remaining=0, failed=[])
    finally:
        await target.aclose()


async def test_a_missing_source_object_is_recorded_and_skipped(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    src = local_content(tmp_path / "a", pool)
    gone = await src.put("piece", b"gone", name="g", scope=SCOPE)
    kept = await src.put("piece", b"kept", name="k", scope=SCOPE)
    assert isinstance(src.backend, LocalContentBackend)
    (src.backend.root / gone.backend_id).unlink()
    index = BlobIndex(pool)
    try:
        result = await migrate_batch(index, src.backend, target, limit=10, heartbeat=lambda _: None)
        assert result == MigrationBatch(moved=1, remaining=0, failed=[f"piece-{gone.sha256}"])
        assert index.get(f"piece-{gone.sha256}").ref == gone  # type: ignore[union-attr]
        assert index.get(f"piece-{kept.sha256}").ref.backend == "s3"  # type: ignore[union-attr]
    finally:
        await target.aclose()


async def test_a_source_object_that_is_not_its_sha_is_recorded(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    src = local_content(tmp_path / "a", pool)
    ref = await src.put("piece", b"good", name="g", scope=SCOPE)
    await overwrite_object(src, ref.backend_id, b"bad!")
    index = BlobIndex(pool)
    try:
        result = await migrate_batch(index, src.backend, target, limit=10, heartbeat=lambda _: None)
        assert result == MigrationBatch(moved=0, remaining=0, failed=[f"piece-{ref.sha256}"])
        assert index.get(f"piece-{ref.sha256}").ref == ref  # type: ignore[union-attr]
    finally:
        await target.aclose()


async def test_a_target_failure_is_recorded(
    pool: Pool, tmp_path: Path, target: S3ContentBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = local_content(tmp_path / "a", pool)
    ref = await src.put("piece", b"zip", name="p", scope=SCOPE)

    async def refuse(*_: Any, **__: Any) -> str:
        raise OSError("the bucket refused it")

    monkeypatch.setattr(target, "upload", refuse)
    try:
        result = await migrate_batch(
            BlobIndex(pool), src.backend, target, limit=10, heartbeat=lambda _: None
        )
        assert result == MigrationBatch(moved=0, remaining=0, failed=[f"piece-{ref.sha256}"])
    finally:
        await target.aclose()


class _RePutting(LocalContentBackend):
    """Re-puts ``key`` with other bytes while its download streams: the row moves on
    between the batch's read and its swap."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.store: ContentStore | None = None
        self.key = ""

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        async for chunk in super().download(backend_id):
            yield chunk
        if self.store is not None:
            store, self.store = self.store, None
            await store.put("piece", b"newer", name="n", scope=SCOPE, key=self.key)


async def test_a_row_touched_during_the_batch_is_left_for_the_next(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    backend = _RePutting(tmp_path / "a")
    index = BlobIndex(pool)
    src = ContentStore(backend, index)
    await src.put("piece", b"older", name="o", scope=SCOPE, key="k")
    backend.store, backend.key = src, "k"
    try:
        result = await migrate_batch(index, backend, target, limit=10, heartbeat=lambda _: None)
        assert result == MigrationBatch(moved=0, remaining=0, failed=["k"])
        stat = index.get("k")
        assert stat is not None
        newer = hashlib.sha256(b"newer").hexdigest()
        assert (stat.ref.backend, stat.ref.sha256) == ("local", newer)
        again = await migrate_batch(index, backend, target, limit=10, heartbeat=lambda _: None)
        assert again == MigrationBatch(moved=1, remaining=0, failed=[])
        moved = index.get("k")
        assert moved is not None and moved.ref.backend == "s3"
        assert await _bytes(target, moved.ref.backend_id) == b"newer"
    finally:
        await target.aclose()


async def test_a_row_left_on_the_old_backend_is_missing_not_an_error(
    pool: Pool, tmp_path: Path, s3: S3Settings
) -> None:
    old = local_content(tmp_path / "a", pool)
    ref = await old.put("piece", b"zip", name="p", scope=SCOPE)
    store = s3_content(s3, pool)
    # An object under the same id on S3: only the ref's backend tells them apart.
    await overwrite_object(store, ref.backend_id, b"zip")
    try:
        with pytest.raises(BlobMissingError):
            await store.read(ref)
    finally:
        await store.aclose()
        assert isinstance(store.backend, S3ContentBackend)
        await store.backend.aclose()
