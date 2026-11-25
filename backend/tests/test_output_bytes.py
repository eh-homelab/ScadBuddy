"""`OutputBytes` (#7256, outputs-in-Postgres spec 2026-11-23 §4.3): an output's bytes as
blobs of kind ``output``, and the temporary files for readers that need a path."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import threading
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import AsyncGeneratorType
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

import scadbuddy.store.index as index_module
from scadbuddy.library.output_bytes import _PREFETCH, OutputBytes, OutputFileNameError, usable
from scadbuddy.library.output_rows import (
    OutputBlobs,
    OutputFileBlob,
    output_blob_key,
    output_file_key,
)
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.content import (
    BlobCorruptError,
    BlobMissingError,
    BlobRef,
    BlobScope,
    ContentStore,
)
from scadbuddy.store.index import (
    STAGE_LOCK_POOL_SIZE,
    STAGE_LOCK_SLOTS,
    BlobIndex,
    Pool,
    StageLockBusyError,
    StageLocks,
    object_lock_name,
)
from scadbuddy.store.local import LocalContentBackend
from scadbuddy.store.s3 import S3ContentBackend
from tests.support.store import flip_first_byte, local_content, moto_s3, record_reads, store_pool

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


@pytest.fixture
async def content(tmp_path: Path, pool: Pool) -> AsyncGenerator[ContentStore]:
    store = local_content(tmp_path / "remote", pool)
    store.spool = tmp_path / "spool"
    try:
        yield store
    finally:
        await store.aclose()


@pytest.fixture
def s3_settings() -> Iterator[S3Settings]:
    with moto_s3() as settings:
        yield settings


@pytest.fixture
async def blobs(content: ContentStore) -> OutputBytes:
    return OutputBytes(content)


def _folder(root: Path, *, preview: bool = True, thumbnail: bool = True) -> Path:
    root.mkdir(parents=True)
    (root / "model.3mf").write_bytes(b"3mf")
    if preview:
        (root / "preview.glb").write_bytes(b"glb")
    if thumbnail:
        (root / "thumbnail.png").write_bytes(b"png")
    files = root / "files"
    files.mkdir()
    (files / "b.stl").write_bytes(b"bb")
    (files / "a.stl").write_bytes(b"a")
    return root


def _staged(got: OutputBlobs) -> dict[str, BlobRef]:
    """Each of the output's keys and the object its bytes went to."""
    return {item.key: item.ref for item in got.staged}


def _index_keys(pool: Pool) -> list[str]:
    with pool.connection() as conn:
        found = conn.execute("SELECT key FROM store_blobs").fetchall()
    return [str(item["key"]) for item in found]


def _spooled(content: ContentStore) -> list[Path]:
    return list(content.spool.iterdir()) if content.spool.exists() else []


async def test_put_folder_puts_every_role_and_file(
    blobs: OutputBytes, content: ContentStore, pool: Pool, tmp_path: Path
) -> None:
    got = await blobs.put_folder(_folder(tmp_path / "o1"), "o1", "cube")
    assert got.model == output_blob_key("o1", "model")
    assert got.preview == output_blob_key("o1", "preview")
    assert got.thumbnail == output_blob_key("o1", "thumbnail")
    assert got.files == (
        OutputFileBlob(name="a.stl", blob_key=output_file_key("o1", "a.stl"), size=1),
        OutputFileBlob(name="b.stl", blob_key=output_file_key("o1", "b.stl"), size=2),
    )
    staged = _staged(got)
    assert list(staged) == got.keys()
    # The objects are uploaded and no index row is written (#7477): the output's keys
    # are indexed only in the row's transaction (`OutputRows.insert`).
    assert _index_keys(pool) == []
    assert not any(item.reused for item in got.staged)
    assert await content.read(staged[output_blob_key("o1", "model")]) == b"3mf"
    assert await content.read(staged[output_file_key("o1", "b.stl")]) == b"bb"
    ref = staged[output_blob_key("o1", "preview")]
    assert ref.kind == "output"
    assert ref.sha256 == hashlib.sha256(b"glb").hexdigest()
    # No thumbnail: no thumbnail blob.
    bare = await blobs.put_folder(_folder(tmp_path / "o2", thumbnail=False), "o2", "cube")
    assert bare.thumbnail is None
    assert output_blob_key("o2", "thumbnail") not in _staged(bare)


async def test_put_folder_reuses_an_object_a_row_names(
    blobs: OutputBytes, content: ContentStore, tmp_path: Path
) -> None:
    """An object the index names already is reused, not uploaded again; the row's
    transaction holds it (`BlobIndex.put_in` ``reuse``)."""
    first = await blobs.put_folder(_folder(tmp_path / "o1"), "o1", "cube")
    model = output_blob_key("o1", "model")
    await asyncio.to_thread(content.index.put, model, _staged(first)[model], slug="cube", meta={})
    again = await blobs.put_folder(_folder(tmp_path / "o2"), "o2", "cube")
    reused = {item.key: item.reused for item in again.staged}
    assert reused[output_blob_key("o2", "model")]
    assert not reused[output_blob_key("o2", "preview")]
    assert _staged(again)[output_blob_key("o2", "model")] == _staged(first)[model]


async def test_a_failed_put_folder_removes_what_it_uploaded(
    blobs: OutputBytes, content: ContentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = content.stage
    refs: list[BlobRef] = []

    async def third_fails(*args: Any, **kwargs: Any) -> tuple[BlobRef, bool]:
        if len(refs) == 2:
            raise OSError("the store went away")
        ref, reused = await stage(*args, **kwargs)
        refs.append(ref)
        return ref, reused

    monkeypatch.setattr(content, "stage", third_fails)
    with pytest.raises(OSError, match="the store went away"):
        await blobs.put_folder(_folder(tmp_path / "o1"), "o1", "cube")
    for ref in refs:
        assert not await content.backend.exists(ref.backend_id)


async def test_put_folder_without_preview_raises(
    blobs: OutputBytes, content: ContentStore, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        await blobs.put_folder(_folder(tmp_path / "o1", preview=False), "o1", "cube")
    # The check comes before any put.
    assert blobs.ref(output_blob_key("o1", "model")) is None


async def test_put_folder_refuses_a_name_an_output_may_not_hold(
    blobs: OutputBytes, tmp_path: Path
) -> None:
    root = _folder(tmp_path / "o1")
    (root / "files" / ".hidden").write_bytes(b"x")
    (root / "files" / "sub").mkdir()
    (root / "files" / "link.stl").symlink_to(root / "files" / "a.stl")
    with pytest.raises(OutputFileNameError) as raised:
        await blobs.put_folder(root, "o1", "cube")
    assert raised.value.names == (".hidden", "link.stl", "sub")
    assert ".hidden" in str(raised.value) and "sub" in str(raised.value)
    # The check comes before any put.
    assert blobs.ref(output_blob_key("o1", "model")) is None


async def test_put_if_absent_keeps_first(blobs: OutputBytes) -> None:
    key = output_blob_key("o1", "thumbnail")
    assert await blobs.put_if_absent(key, b"first", slug="cube") is True
    assert await blobs.put_if_absent(key, b"second", slug="cube") is False
    assert await blobs.read(key) == b"first"


async def test_put_if_absent_uploads_nothing_when_key_exists(
    blobs: OutputBytes, content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = output_blob_key("o1", "thumbnail")
    await blobs.put(key, b"first", slug="cube")

    async def no_store(*args: object, **kwargs: object) -> None:
        raise AssertionError("put_if_absent hashed or uploaded for a key that exists")

    monkeypatch.setattr(content, "_store", no_store)
    assert await blobs.put_if_absent(key, b"second", slug="cube") is False
    assert await blobs.read(key) == b"first"


async def test_replace_refuses_moved_key(blobs: OutputBytes) -> None:
    key = output_blob_key("o1", "thumbnail")
    old = await blobs.put(key, b"old", slug="cube")
    moved = await blobs.put(key, b"moved", slug="cube")
    assert await blobs.replace(key, b"copy", slug="cube", expected=old.sha256) is None
    assert await blobs.read(key) == b"moved"
    landed = await blobs.replace(key, b"new", slug="cube", expected=moved.sha256)
    assert landed is not None and landed.sha256 == hashlib.sha256(b"new").hexdigest()
    assert await blobs.read(key) == b"new"


async def test_stream_raises_missing_before_first_chunk(
    blobs: OutputBytes, content: ContentStore
) -> None:
    key = output_blob_key("o1", "model")
    ref = await blobs.put(key, b"3mf bytes", slug="cube")
    stream = await blobs.stream(key)
    assert (stream.size, stream.sha256) == (ref.size, ref.sha256)
    assert b"".join([chunk async for chunk in stream.chunks]) == b"3mf bytes"
    # The object lost behind the index's back: the call itself raises.
    await content.backend.remove(ref.backend_id)
    with pytest.raises(BlobMissingError):
        await blobs.stream(key)
    with pytest.raises(BlobMissingError):
        await blobs.stream(output_blob_key("gone", "model"))


async def test_stream_raises_corrupt_small_blob_before_first_chunk(
    blobs: OutputBytes, content: ContentStore
) -> None:
    key = output_blob_key("o1", "model")
    await blobs.put(key, b"3mf bytes", slug="cube")
    await flip_first_byte(content, key)
    with pytest.raises(BlobCorruptError):
        await blobs.stream(key)


async def test_stream_raises_corrupt_blob_of_exactly_prefetch(
    blobs: OutputBytes, content: ContentStore
) -> None:
    key = output_blob_key("o1", "model")
    await blobs.put(key, b"x" * _PREFETCH, slug="cube")
    await flip_first_byte(content, key)
    # A blob of exactly the prefetch is checked whole before `stream` returns.
    with pytest.raises(BlobCorruptError):
        await blobs.stream(key)


async def test_put_folder_on_s3_reads_its_files_off_the_loop(
    s3_settings: S3Settings, pool: Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = S3ContentBackend(s3_settings, multipart_threshold=5 << 20, part_size=5 << 20)
    content = ContentStore(backend, BlobIndex(pool))
    content.spool = tmp_path / "spool"
    folder = _folder(tmp_path / "o1")
    big = os.urandom(11 << 20)
    (folder / "model.3mf").write_bytes(big)
    parts: list[str] = []
    multipart = backend._multipart

    async def counted(client: Any, key: str, data: Any, kind: str) -> None:
        parts.append(key)
        await multipart(client, key, data, kind)

    monkeypatch.setattr(backend, "_multipart", counted)
    reads = record_reads(monkeypatch, folder)
    blobs = OutputBytes(content)
    try:
        staged = _staged(await blobs.put_folder(folder, "o1", "cube"))
        monkeypatch.undo()
        # The model is past the threshold, so it went up in parts; the rest whole.
        assert len(parts) == 1
        assert await content.read(staged[output_blob_key("o1", "model")]) == big
        assert await content.read(staged[output_blob_key("o1", "preview")]) == b"glb"
        assert await content.read(staged[output_file_key("o1", "b.stl")]) == b"bb"
    finally:
        await content.aclose()
    assert reads
    # No read of an output's file ran on the loop's thread.
    assert not any(reads)


async def test_stream_holds_last_chunk_when_blob_corrupt(
    blobs: OutputBytes, content: ContentStore
) -> None:
    key = output_blob_key("o1", "model")
    size = 3 * _PREFETCH + 5
    await blobs.put(key, b"x" * size, slug="cube")
    await flip_first_byte(content, key)
    stream = await blobs.stream(key)
    assert stream.size == size
    got = 0
    async with stream:
        with pytest.raises(BlobCorruptError):
            async for chunk in stream.chunks:
                got += len(chunk)
    # The last chunk waited for the check: the body ends short, never clean.
    assert 0 < got < size


async def test_stream_gives_large_blob_whole(blobs: OutputBytes) -> None:
    key = output_blob_key("o1", "model")
    data = bytes(range(256)) * (_PREFETCH // 128 + 3)
    await blobs.put(key, data, slug="cube")
    async with await blobs.stream(key) as stream:
        assert b"".join([chunk async for chunk in stream.chunks]) == data


async def test_stream_aclose_closes_source_unread(blobs: OutputBytes) -> None:
    key = output_blob_key("o1", "model")
    # Past the prefetch, so the backend's read is still open when `stream` returns.
    await blobs.put(key, b"x" * (_PREFETCH + 1), slug="cube")
    stream = await blobs.stream(key)
    source = stream._source
    assert isinstance(source, AsyncGeneratorType) and source.ag_frame is not None
    # A response that never starts its body still closes the backend's read.
    await stream.aclose()
    assert source.ag_frame is None
    async with await blobs.stream(key) as held:
        inner = held._source
        assert isinstance(inner, AsyncGeneratorType)
    assert inner.ag_frame is None


async def test_local_file_holds_blob_bytes_then_is_removed(
    blobs: OutputBytes, content: ContentStore
) -> None:
    key = output_blob_key("o1", "model")
    await blobs.put(key, b"3mf bytes", slug="cube")
    async with blobs.local_file(key, ".3mf") as path:
        assert path.parent == content.spool
        assert path.suffix == ".3mf"
        assert path.read_bytes() == b"3mf bytes"
    assert not path.exists()
    assert _spooled(content) == []


async def test_local_file_removed_after_error(blobs: OutputBytes, content: ContentStore) -> None:
    key = output_blob_key("o1", "model")
    await blobs.put(key, b"3mf bytes", slug="cube")
    with pytest.raises(ValueError, match="reader failed"):
        async with blobs.local_file(key, ".3mf") as path:
            assert path.is_file()
            raise ValueError("reader failed")
    assert not path.exists()
    # A blob that is gone leaves no temporary file either.
    with pytest.raises(BlobMissingError):
        async with blobs.local_file(output_blob_key("gone", "model"), ".3mf"):
            pass
    assert _spooled(content) == []


async def test_local_file_cancelled_during_download_leaves_no_file(
    blobs: OutputBytes, content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = output_blob_key("o1", "model")
    await blobs.put(key, b"3mf bytes", slug="cube")
    reached = threading.Event()
    gate = threading.Event()
    opened = threading.Event()
    reopened: list[Path] = []
    original_open = Path.open

    def gated_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        # A reopen of the temporary path: held until the reader was cancelled.
        if self.parent == content.spool and "w" in mode:
            reopened.append(self)
            reached.set()
            gate.wait(5)
            try:
                return original_open(self, mode, *args, **kwargs)
            finally:
                opened.set()
        return original_open(self, mode, *args, **kwargs)

    async def stalling(ref: BlobRef) -> AsyncGenerator[bytes]:
        reached.set()
        await asyncio.Event().wait()
        yield b""

    monkeypatch.setattr(Path, "open", gated_open)
    monkeypatch.setattr(content, "get", stalling)

    async def reader() -> None:
        async with blobs.local_file(key, ".3mf"):
            pass

    task = asyncio.create_task(reader())
    assert await asyncio.to_thread(reached.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    if reopened:
        await asyncio.to_thread(opened.wait, 5)
    assert _spooled(content) == []


async def test_local_file_cancelled_while_temporary_runs_leaves_no_file(
    blobs: OutputBytes, content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = output_blob_key("o1", "model")
    await blobs.put(key, b"3mf bytes", slug="cube")
    reached = threading.Event()
    gate = threading.Event()
    made = threading.Event()
    original = blobs._temporary

    def gated(suffix: str) -> tuple[Any, Path]:
        reached.set()
        gate.wait(5)
        try:
            return original(suffix)
        finally:
            made.set()

    monkeypatch.setattr(blobs, "_temporary", gated)

    async def reader() -> None:
        async with blobs.local_file(key, ".3mf"):
            pass

    task = asyncio.create_task(reader())
    assert await asyncio.to_thread(reached.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    assert await asyncio.to_thread(made.wait, 5)
    for _ in range(100):
        if not _spooled(content):
            break
        await asyncio.sleep(0.01)
    assert _spooled(content) == []


async def test_verify_checks_the_blob(blobs: OutputBytes, content: ContentStore) -> None:
    key = output_blob_key("o1", "model")
    ref = await blobs.put(key, b"3mf bytes", slug="cube")
    assert await blobs.verify(key) is True
    assert await blobs.verify(output_blob_key("gone", "model")) is False
    await content.backend.remove(ref.backend_id)
    assert await blobs.verify(key) is False


async def test_usable_refuses_bambuddy_and_none(
    content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert usable(None) is False
    assert usable(content) is True
    monkeypatch.setattr(content.backend, "backend", "bambuddy")
    assert usable(content) is False


async def test_the_sweep_removes_an_unindexed_object_only_past_the_grace_and_unheld(
    s3_settings: S3Settings, pool: Pool, tmp_path: Path
) -> None:
    """#7477: a save that died between its upload and its commit leaves an object no row
    names. The unrecorded sweep removes it once it is older than the grace, and never
    while a live save holds it (`ContentStore.claim_object`)."""
    backend = S3ContentBackend(s3_settings)
    content = ContentStore(backend, BlobIndex(pool))
    content.spool = tmp_path / "spool"
    try:
        async with content.stage_hold() as hold:
            assert hold is not None
            ref, reused = await content.stage(
                "output", b"staged", name="k", scope=BlobScope(slug="cube"), hold=hold
            )
            assert not reused

            async def sweep(cutoff: datetime) -> int:
                return await backend.sweep_unrecorded(
                    cutoff=cutoff,
                    recorded=partial(content.index.shares_backend_id, backend.backend),
                    claim=content.claim_object,
                )

            past_grace = datetime.now(UTC) + timedelta(minutes=1)
            # A live save holds it: even past the grace it stays.
            assert await sweep(past_grace) == 0
            assert await backend.exists(ref.backend_id)
            # The save died: its connection, and so its lock, went.
            await asyncio.to_thread(hold.close)
            # Within the grace it stays.
            assert await sweep(datetime.now(UTC) - timedelta(hours=1)) == 0
            assert await backend.exists(ref.backend_id)
            # Past the grace it goes.
            assert await sweep(past_grace) == 1
            assert not await backend.exists(ref.backend_id)
    finally:
        await content.aclose()


async def test_release_unindexed_leaves_an_object_another_stager_holds(
    content: ContentStore,
) -> None:
    """#7477: a failed save's release never removes an object another stager holds."""
    async with content.stage_hold() as other:
        assert other is not None
        ref, _ = await content.stage(
            "output", b"shared", name="k", scope=BlobScope(slug="cube"), hold=other
        )
        mine, _ = await content.stage("output", b"shared", name="k", scope=BlobScope(slug="cube"))
        assert mine.backend_id == ref.backend_id
        await content.release_unindexed(mine, False)
        assert await content.backend.exists(ref.backend_id)
    await content.release_unindexed(ref, False)
    assert not await content.backend.exists(ref.backend_id)


@pytest.mark.parametrize("kind", ["local", "s3"])
async def test_the_unrecorded_sweep_claims_only_objects_no_row_names(
    kind: str, s3_settings: S3Settings, pool: Pool, tmp_path: Path
) -> None:
    """#7477 review M3: an object a row names costs one query (`recorded`); only one no
    row names is claimed (a try-lock, a check under it, an unlock). On `local` too, whose
    outputs have a content store of their own (#7257): a dead save's object goes."""
    backend: LocalContentBackend | S3ContentBackend = (
        LocalContentBackend(tmp_path / "remote")
        if kind == "local"
        else S3ContentBackend(s3_settings)
    )
    content = ContentStore(backend, BlobIndex(pool))
    content.spool = tmp_path / "spool"
    try:
        kept = await content.put("output", b"kept", name="k", scope=BlobScope(slug="cube"))
        loose, _ = await content.stage("output", b"loose", name="l", scope=BlobScope(slug="cube"))
        claimed: list[str] = []

        def claim(backend_id: str) -> Any:
            claimed.append(backend_id)
            return content.claim_object(backend_id)

        swept = await backend.sweep_unrecorded(
            cutoff=datetime.now(UTC) + timedelta(minutes=1),
            recorded=partial(content.index.shares_backend_id, backend.backend),
            claim=claim,
        )
        assert swept == 1
        assert claimed == [loose.backend_id]
        assert await backend.exists(kept.backend_id)
        assert not await backend.exists(loose.backend_id)
    finally:
        await content.aclose()


async def test_the_local_unrecorded_sweep_keeps_a_young_or_held_object(
    pool: Pool, tmp_path: Path
) -> None:
    """#7477 on `local`: an object no row names stays within the grace, and past it
    while a live save holds it (`StageHold`)."""
    backend = LocalContentBackend(tmp_path / "remote")
    content = ContentStore(backend, BlobIndex(pool))
    content.spool = tmp_path / "spool"
    try:

        async def sweep(cutoff: datetime) -> int:
            return await backend.sweep_unrecorded(
                cutoff=cutoff,
                recorded=partial(content.index.shares_backend_id, backend.backend),
                claim=content.claim_object,
            )

        async with content.stage_hold() as hold:
            ref, _ = await content.stage(
                "output", b"staged", name="k", scope=BlobScope(slug="cube"), hold=hold
            )
            assert await sweep(datetime.now(UTC) + timedelta(minutes=1)) == 0
        assert await sweep(datetime.now(UTC) - timedelta(hours=1)) == 0
        assert await backend.exists(ref.backend_id)
        assert await sweep(datetime.now(UTC) + timedelta(minutes=1)) == 1
        assert not await backend.exists(ref.backend_id)
    finally:
        await content.aclose()


def _lock_free(conninfo: str, name: str) -> bool:
    """Whether no session holds the advisory lock ``name``: a fresh connection takes it
    exclusively, then drops it as it closes."""
    with psycopg.connect(conninfo, autocommit=True) as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (name,)
        ).fetchone()
    return bool(row and row[0])


def _failing_unlocks(monkeypatch: pytest.MonkeyPatch) -> None:
    execute = psycopg.Connection.execute

    def failing(self: psycopg.Connection[Any], query: Any, *args: Any, **kwargs: Any) -> Any:
        if "pg_advisory_unlock" in str(query):
            raise psycopg.OperationalError("the unlock failed")
        return execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", failing)


async def test_a_stage_hold_whose_unlock_fails_drops_its_connection(
    pool: Pool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7477 review M2: a connection whose locks could not be dropped is closed, never
    given back to the pool with them, where they would stall removals for its next user."""
    sha = "a" * 64
    name = object_lock_name("local", sha)
    async with BlobIndex(pool).stage_hold() as hold:
        await asyncio.to_thread(hold.take, "local", sha)
        assert not _lock_free(pg_conninfo, name)
        _failing_unlocks(monkeypatch)
        await asyncio.to_thread(hold.close)
        monkeypatch.undo()
    assert _lock_free(pg_conninfo, name)


def test_a_claim_whose_unlock_fails_drops_its_connection(
    pool: Pool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7477 review M2: the same for a removal's exclusive lock (`claim_unnamed`)."""
    sha = "b" * 64
    name = object_lock_name("local", sha)
    with BlobIndex(pool).claim_unnamed("local", f"output/{sha}", sha) as free:
        assert free
        _failing_unlocks(monkeypatch)
    monkeypatch.undo()
    assert _lock_free(pg_conninfo, name)


async def test_a_claim_takes_its_lock_connection_from_the_stage_lock_pool(
    pg_conninfo: str,
) -> None:
    """#7756 review: a release keeps its exclusive lock through the backend's remove, a
    round trip, so its connection is the stage-lock pool's reserved one, never the API's
    shared pool's. With the shared pool spent and every save slot holding its own lock
    connection, a claim still gets one, and frees it at its end."""
    kwargs = {"autocommit": True, "row_factory": dict_row}
    with (
        ConnectionPool(
            pg_conninfo,
            min_size=0,
            max_size=1,
            timeout=1,
            connection_class=Connection[DictRow],
            kwargs=kwargs,
        ) as shared,
        ConnectionPool(
            pg_conninfo,
            min_size=0,
            max_size=STAGE_LOCK_POOL_SIZE,
            timeout=1,
            connection_class=Connection[DictRow],
            kwargs=kwargs,
        ) as locks,
    ):
        index = BlobIndex(shared, stage_locks=StageLocks(locks, STAGE_LOCK_SLOTS))
        held = shared.getconn()
        try:
            async with contextlib.AsyncExitStack() as stack:
                for n in range(STAGE_LOCK_SLOTS):
                    hold = await stack.enter_async_context(index.stage_hold())
                    await asyncio.to_thread(hold.take, "local", str(n) * 64)
                sha = "e" * 64
                for _ in range(2):
                    with index.claim_unnamed("local", f"output/{sha}", sha) as free:
                        assert free
        finally:
            shared.putconn(held)


async def test_the_local_unrecorded_sweep_keeps_an_object_written_again_before_its_claim(
    pool: Pool, tmp_path: Path
) -> None:
    """#7756 review: the sweep reads an object's mtime before its claim, and a plain put
    (no stage hold) may write the same bytes again in between, which gives the file a
    fresh mtime. The sweep reads the mtime again under the claim and keeps it."""
    backend = LocalContentBackend(tmp_path / "remote")
    content = ContentStore(backend, BlobIndex(pool))
    content.spool = tmp_path / "spool"
    try:
        ref, _ = await content.stage("output", b"orphan", name="o", scope=BlobScope(slug="c"))
        path = backend.root / ref.backend_id
        old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
        os.utime(path, (old, old))

        def claim(backend_id: str) -> Any:
            # What a plain put of the same bytes does meanwhile: a fresh file.
            os.utime(path)
            return content.claim_object(backend_id)

        swept = await backend.sweep_unrecorded(
            cutoff=datetime.now(UTC) - timedelta(hours=1),
            recorded=partial(content.index.shares_backend_id, backend.backend),
            claim=claim,
        )
        assert swept == 0
        assert await backend.exists(ref.backend_id)
    finally:
        await content.aclose()


@pytest.fixture
def lock_pool(pg_conninfo: str) -> Iterator[Pool]:
    with ConnectionPool(
        pg_conninfo,
        min_size=0,
        max_size=1,
        timeout=2,
        connection_class=Connection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
    ) as opened:
        yield opened


def _claimed(conninfo: str, name: str) -> psycopg.Connection[Any]:
    """A session that holds the lock ``name`` exclusively, as a removal's claim does."""
    conn = psycopg.connect(conninfo, autocommit=True)
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (name,))
    return conn


async def test_a_stage_hold_gives_up_on_a_claim_that_never_ends(
    lock_pool: Pool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7756 review: a removal's claim lasts as long as its backend's remove; one that
    hangs must not pin a save's slot. `take` waits `STAGE_LOCK_TIMEOUT_MS` a try, at most
    `STAGE_LOCK_TRIES` tries, then raises `StageLockBusyError`. The hold still closes,
    its slot is free, and its pooled connection keeps no lock and no timeout."""
    monkeypatch.setattr(index_module, "STAGE_LOCK_TIMEOUT_MS", 50)
    monkeypatch.setattr(index_module, "STAGE_LOCK_TRIES", 2)
    locks = StageLocks(lock_pool, 1)
    busy, free = "a" * 64, "b" * 64
    claimed = _claimed(pg_conninfo, object_lock_name("local", busy))
    try:
        async with locks.hold() as hold:
            await asyncio.to_thread(hold.take, "local", free)
            with pytest.raises(StageLockBusyError):
                await asyncio.to_thread(hold.take, "local", busy)
    finally:
        claimed.close()
    assert _lock_free(pg_conninfo, object_lock_name("local", free))
    async with asyncio.timeout(5), locks.hold() as hold:
        await asyncio.to_thread(hold.take, "local", busy)
    with lock_pool.connection() as conn:
        assert conn.execute("SHOW lock_timeout").fetchone() == {"lock_timeout": "0"}


async def test_a_stage_hold_takes_the_lock_once_a_claim_ends_within_its_tries(
    lock_pool: Pool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7756 review: a claim that ends while `take` still has tries left costs the save
    a wait, not its failure."""
    monkeypatch.setattr(index_module, "STAGE_LOCK_TIMEOUT_MS", 200)
    monkeypatch.setattr(index_module, "STAGE_LOCK_TRIES", 10)
    sha = "c" * 64
    name = object_lock_name("local", sha)
    claimed = _claimed(pg_conninfo, name)
    timer = threading.Timer(0.3, claimed.close)
    timer.start()
    try:
        async with StageLocks(lock_pool, 1).hold() as hold:
            await asyncio.to_thread(hold.take, "local", sha)
            assert not _lock_free(pg_conninfo, name)
    finally:
        timer.cancel()
        claimed.close()
    assert _lock_free(pg_conninfo, name)
