"""Copying recorded blobs to another backend, then switching them (spec 2026-10-16 §4.3)."""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import AsyncGenerator, Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store import migrate
from scadbuddy.store.content import BlobMissingError, BlobScope, ContentBackend, ContentStore
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


async def _batch(
    index: BlobIndex,
    source: ContentBackend,
    target: ContentBackend,
    *,
    limit: int = 10,
    started: datetime | None = None,
    heartbeat: Callable[[str], None] = lambda _: None,
) -> MigrationBatch:
    """One batch; ``started`` defaults to now, a new run's."""
    return await migrate_batch(
        index,
        source,
        target,
        limit=limit,
        started=started or index.now(),
        heartbeat=heartbeat,
    )


def _copy(pool: Pool, key: str) -> tuple[str | None, str | None, str | None]:
    """``key``'s recorded copy: its backend, its id there, and why it failed."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT migrated_backend, migrated_backend_id, migrated_error FROM store_blobs"
            " WHERE key = %s",
            (key,),
        ).fetchone()
    assert row is not None
    return row["migrated_backend"], row["migrated_backend_id"], row["migrated_error"]


async def test_a_batch_copies_the_bytes_and_leaves_the_row_on_its_backend(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    src = local_content(tmp_path / "a", pool)
    ref = await src.put("piece", b"zip", name="p.zip", scope=SCOPE, meta={"x": 1})
    key = f"piece-{ref.sha256}"
    index = BlobIndex(pool)
    try:
        result = await _batch(index, src.backend, target)
        assert result == MigrationBatch(moved=1, remaining=0, failed=[])
        # Still the source's, which the running processes read until the switch.
        stat = index.get(key)
        assert stat is not None and stat.ref == ref
        assert await src.read(ref) == b"zip"
        backend, backend_id, error = _copy(pool, key)
        assert (backend, error) == ("s3", None) and backend_id is not None
        assert await _bytes(target, backend_id) == b"zip"
        assert index.migration_counts("local", "s3") == (0, 0, 1)
        assert await _batch(index, src.backend, target) == MigrationBatch(
            moved=0, remaining=0, failed=[]
        )
    finally:
        await target.aclose()


async def test_the_switch_points_copied_rows_at_their_copy_on_that_backend_only(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    src = local_content(tmp_path / "a", pool)
    copied = await src.put("piece", b"copied", name="c", scope=SCOPE, meta={"x": 1})
    left = await src.put("piece", b"left", name="l", scope=SCOPE)
    index = BlobIndex(pool)
    try:
        # The least recently touched first: only `copied`.
        assert (await _batch(index, src.backend, target, limit=1)).remaining == 1
        _, copy_id, _ = _copy(pool, f"piece-{copied.sha256}")
        assert index.switch_migrated("bambuddy") == 0
        assert index.get(f"piece-{copied.sha256}").ref == copied  # type: ignore[union-attr]
        assert index.switch_migrated("s3") == 1
        stat = index.get(f"piece-{copied.sha256}")
        assert stat is not None
        assert (stat.ref.backend, stat.ref.backend_id, stat.ref.sha256) == (
            "s3",
            copy_id,
            copied.sha256,
        )
        assert (stat.slug, stat.meta) == ("demo", {"x": 1})
        assert _copy(pool, f"piece-{copied.sha256}") == (None, None, None)
        assert index.get(f"piece-{left.sha256}").ref == left  # type: ignore[union-attr]
        store = s3_content(target.settings, pool)
        assert await store.read(stat.ref) == b"copied"
        await store.aclose()
        assert isinstance(store.backend, S3ContentBackend)
        await store.backend.aclose()
    finally:
        await target.aclose()


async def test_a_put_of_other_bytes_drops_the_stale_copy_and_the_same_bytes_keep_it(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    src = local_content(tmp_path / "a", pool)
    await src.put("piece", b"one", name="o", scope=SCOPE, key="k")
    index = BlobIndex(pool)
    try:
        await _batch(index, src.backend, target)
        await src.put("piece", b"one", name="o", scope=SCOPE, key="k")
        assert _copy(pool, "k")[0] == "s3"
        await src.put("piece", b"two", name="t", scope=SCOPE, key="k")
        assert _copy(pool, "k") == (None, None, None)
        assert index.migration_counts("local", "s3") == (1, 0, 0)
        stat = index.get("k")
        assert stat is not None
        assert await src.replace(
            "k", "piece", b"three", name="t", scope=SCOPE, expected=stat.ref.sha256
        )
        await _batch(index, src.backend, target)
        assert _copy(pool, "k")[0] == "s3"
        stat = index.get("k")
        assert stat is not None
        assert await src.replace(
            "k", "piece", b"four", name="f", scope=SCOPE, expected=stat.ref.sha256
        )
        assert _copy(pool, "k") == (None, None, None)
    finally:
        await target.aclose()


async def test_a_batch_takes_the_oldest_rows_up_to_its_limit(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    src = local_content(tmp_path / "a", pool)
    refs = [await src.put("asset", data, name="a", scope=SCOPE) for data in (b"1", b"2", b"3")]
    keys = [f"asset-{ref.sha256}" for ref in refs]
    index = BlobIndex(pool)
    try:
        first = await _batch(index, src.backend, target, limit=2)
        assert first == MigrationBatch(moved=2, remaining=1, failed=[])
        assert [_copy(pool, key)[0] for key in keys] == ["s3", "s3", None]
        rest = await _batch(index, src.backend, target, limit=5)
        assert rest == MigrationBatch(moved=1, remaining=0, failed=[])
    finally:
        await target.aclose()


async def test_a_missing_source_object_is_recorded_on_its_row_and_retried_by_a_new_run(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    src = local_content(tmp_path / "a", pool)
    gone = await src.put("piece", b"gone", name="g", scope=SCOPE)
    kept = await src.put("piece", b"kept", name="k", scope=SCOPE)
    assert isinstance(src.backend, LocalContentBackend)
    (src.backend.root / gone.backend_id).unlink()
    key = f"piece-{gone.sha256}"
    index = BlobIndex(pool)
    try:
        started = index.now()
        result = await _batch(index, src.backend, target, started=started)
        assert result == MigrationBatch(moved=1, remaining=0, failed=[key])
        backend, _, error = _copy(pool, key)
        assert backend is None and error is not None and error.startswith("BlobMissingError")
        assert _copy(pool, f"piece-{kept.sha256}")[0] == "s3"
        assert index.migration_counts("local", "s3") == (0, 1, 1)
        assert index.migration_failures("local", "s3", limit=10) == [(key, error)]
        # The same run does not try it again.
        assert await _batch(index, src.backend, target, started=started) == MigrationBatch(
            moved=0, remaining=0, failed=[]
        )
        # A new run does, and a copy clears the error.
        (src.backend.root / gone.backend_id).write_bytes(b"gone")
        assert (await _batch(index, src.backend, target)).moved == 1
        assert _copy(pool, key)[0::2] == ("s3", None)
        assert index.migration_failures("local", "s3", limit=10) == []
    finally:
        await target.aclose()


async def test_a_source_object_that_is_not_its_sha_is_recorded(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    src = local_content(tmp_path / "a", pool)
    ref = await src.put("piece", b"good", name="g", scope=SCOPE)
    await overwrite_object(src, ref.backend_id, b"bad!")
    key = f"piece-{ref.sha256}"
    try:
        result = await _batch(BlobIndex(pool), src.backend, target)
        assert result == MigrationBatch(moved=0, remaining=0, failed=[key])
        error = _copy(pool, key)[2]
        assert error is not None and error.startswith("sha256 mismatch")
    finally:
        await target.aclose()


class _Unreadable(LocalContentBackend):
    """Fails one object's read part way, as a broken connection or a corrupt file would."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.broken = ""

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        if backend_id == self.broken:
            yield b"part"
            raise ConnectionResetError("the source hung up")
        async for chunk in super().download(backend_id):
            yield chunk


async def test_any_source_read_error_is_recorded_and_the_batch_goes_on(
    pool: Pool, tmp_path: Path, target: S3ContentBackend
) -> None:
    backend = _Unreadable(tmp_path / "a")
    index = BlobIndex(pool)
    src = ContentStore(backend, index)
    broken = await src.put("piece", b"broken", name="b", scope=SCOPE)
    fine = await src.put("piece", b"fine", name="f", scope=SCOPE)
    backend.broken = broken.backend_id
    try:
        result = await _batch(index, backend, target)
        assert result == MigrationBatch(moved=1, remaining=0, failed=[f"piece-{broken.sha256}"])
        assert _copy(pool, f"piece-{broken.sha256}")[2] == (
            "ConnectionResetError: the source hung up"
        )
        assert _copy(pool, f"piece-{fine.sha256}")[0] == "s3"
    finally:
        await target.aclose()


async def test_a_target_failure_is_recorded_and_its_error_is_capped(
    pool: Pool, tmp_path: Path, target: S3ContentBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = local_content(tmp_path / "a", pool)
    ref = await src.put("piece", b"zip", name="p", scope=SCOPE)

    async def refuse(*_: Any, **__: Any) -> str:
        raise OSError("refused " * 100)

    monkeypatch.setattr(target, "upload", refuse)
    try:
        result = await _batch(BlobIndex(pool), src.backend, target)
        assert result == MigrationBatch(moved=0, remaining=0, failed=[f"piece-{ref.sha256}"])
        error = _copy(pool, f"piece-{ref.sha256}")[2]
        assert error is not None and error.startswith("OSError: refused")
        assert len(error) == migrate.ERROR_CHARS
    finally:
        await target.aclose()


class _Chunked(LocalContentBackend):
    """Downloads in three chunks."""

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        data = b"".join([chunk async for chunk in super().download(backend_id)])
        for part in (data[:2], data[2:4], data[4:]):
            yield part


@pytest.mark.parametrize(("step", "least"), [(0.0, 1), (2.0, 5)], ids=["fast", "slow"])
async def test_a_copy_heartbeats_per_chunk_at_most_once_a_second(
    pool: Pool,
    tmp_path: Path,
    target: S3ContentBackend,
    monkeypatch: pytest.MonkeyPatch,
    step: float,
    least: int,
) -> None:
    backend = _Chunked(tmp_path / "a")
    index = BlobIndex(pool)
    ref = await ContentStore(backend, index).put("piece", b"sixbyt", name="s", scope=SCOPE)
    ticks = itertools.count()
    monkeypatch.setattr(migrate, "monotonic", lambda: next(ticks) * step)
    beats: list[str] = []
    try:
        await _batch(index, backend, target, heartbeat=beats.append)
        assert set(beats) == {f"piece-{ref.sha256}"}
        # The row, each of its three chunks, and at least one of the upload's reads.
        assert len(beats) == 1 if step == 0 else len(beats) >= least
    finally:
        await target.aclose()


class _RePutting(LocalContentBackend):
    """Re-puts ``key`` with other bytes while its download streams: the row moves on
    between the batch's read and its mark."""

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
        result = await _batch(index, backend, target)
        assert result == MigrationBatch(moved=0, remaining=1, failed=[])
        assert _copy(pool, "k") == (None, None, None)
        again = await _batch(index, backend, target)
        assert again == MigrationBatch(moved=1, remaining=0, failed=[])
        _, copy_id, _ = _copy(pool, "k")
        assert copy_id is not None
        assert await _bytes(target, copy_id) == b"newer"
        assert hashlib.sha256(b"newer").hexdigest() == index.get("k").ref.sha256  # type: ignore[union-attr]
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
