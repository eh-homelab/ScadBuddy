"""`OutputBytes` (#7256, outputs-in-Postgres spec 2026-11-23 §4.3): an output's bytes as
blobs of kind ``output``, and the temporary files for readers that need a path."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest

from scadbuddy.library import output_bytes
from scadbuddy.library.output_bytes import OutputBytes, usable
from scadbuddy.library.output_rows import OutputFileBlob, output_blob_key, output_file_key
from scadbuddy.store.content import BlobMissingError, BlobRef, ContentStore
from scadbuddy.store.index import Pool
from tests.support.store import local_content, store_pool

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
async def blobs(content: ContentStore) -> OutputBytes:
    return OutputBytes(content, asyncio.get_running_loop())


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
    # Not a file name an output may hold: left out.
    (files / ".hidden").write_bytes(b"x")
    return root


def _spooled(content: ContentStore) -> list[Path]:
    return list(content.spool.iterdir()) if content.spool.exists() else []


async def test_put_folder_puts_every_role_and_file(
    blobs: OutputBytes, content: ContentStore, tmp_path: Path
) -> None:
    got = await blobs.put_folder(_folder(tmp_path / "o1"), "o1", "cube")
    assert got.model == output_blob_key("o1", "model")
    assert got.preview == output_blob_key("o1", "preview")
    assert got.thumbnail == output_blob_key("o1", "thumbnail")
    assert got.files == (
        OutputFileBlob(name="a.stl", blob_key=output_file_key("o1", 0), size=1),
        OutputFileBlob(name="b.stl", blob_key=output_file_key("o1", 1), size=2),
    )
    assert await blobs.read(output_blob_key("o1", "model")) == b"3mf"
    assert await blobs.read(output_file_key("o1", 1)) == b"bb"
    ref = blobs.ref(output_blob_key("o1", "preview"))
    assert ref is not None and ref.kind == "output"
    assert ref.sha256 == hashlib.sha256(b"glb").hexdigest()
    stat = await content.stat(output_blob_key("o1", "preview"))
    assert stat is not None and stat.slug == "cube"
    # No thumbnail: no thumbnail blob.
    bare = await blobs.put_folder(_folder(tmp_path / "o2", thumbnail=False), "o2", "cube")
    assert bare.thumbnail is None
    assert blobs.ref(output_blob_key("o2", "thumbnail")) is None


async def test_put_folder_without_preview_raises(
    blobs: OutputBytes, content: ContentStore, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        await blobs.put_folder(_folder(tmp_path / "o1", preview=False), "o1", "cube")
    # The check comes before any put.
    assert blobs.ref(output_blob_key("o1", "model")) is None


async def test_put_if_absent_keeps_first(blobs: OutputBytes) -> None:
    key = output_blob_key("o1", "thumbnail")
    assert await blobs.put_if_absent(key, b"first", slug="cube") is True
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


async def test_blocking_local_file_from_thread(blobs: OutputBytes, content: ContentStore) -> None:
    key = output_blob_key("o1", "model")
    await blobs.put(key, b"3mf bytes", slug="cube")

    def reader() -> tuple[bytes, Path]:
        with blobs.blocking_local_file(key, ".3mf") as path:
            return path.read_bytes(), path

    data, path = await asyncio.to_thread(reader)
    assert data == b"3mf bytes"
    assert not path.exists()
    assert _spooled(content) == []


async def test_blocking_local_file_refuses_loop_thread(blobs: OutputBytes) -> None:
    key = output_blob_key("o1", "model")
    await blobs.put(key, b"3mf bytes", slug="cube")
    # On the loop's own thread the wait would block the loop that must run the
    # download: it raises at once and never waits.
    with pytest.raises(RuntimeError, match="loop"), blobs.blocking_local_file(key, ".3mf"):
        pass


async def test_blocking_local_file_times_out(
    blobs: OutputBytes, content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = output_blob_key("o1", "model")
    await blobs.put(key, b"3mf bytes", slug="cube")

    async def stalling(ref: BlobRef) -> AsyncGenerator[bytes]:
        await asyncio.Event().wait()
        yield b""

    monkeypatch.setattr(content, "get", stalling)
    monkeypatch.setattr(output_bytes, "BLOCKING_READ_TIMEOUT", 0.2)

    def reader() -> None:
        with blobs.blocking_local_file(key, ".3mf"):
            pass

    with pytest.raises(TimeoutError):
        await asyncio.to_thread(reader)
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
