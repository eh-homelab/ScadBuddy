"""`OutputBytes` (#7256, outputs-in-Postgres spec 2026-11-23 §4.3): an output's bytes as
blobs of kind ``output``, and the temporary files for readers that need a path."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from types import AsyncGeneratorType
from typing import Any

import pytest

from scadbuddy.library.output_bytes import _PREFETCH, OutputBytes, OutputFileNameError, usable
from scadbuddy.library.output_rows import (
    OutputBlobs,
    OutputFileBlob,
    output_blob_key,
    output_file_key,
)
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.content import BlobCorruptError, BlobMissingError, BlobRef, ContentStore
from scadbuddy.store.index import BlobIndex, Pool
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
