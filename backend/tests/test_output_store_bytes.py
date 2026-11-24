"""`OutputStore` reads an output's bytes from the blob store first (#7256, outputs-in-
Postgres spec 2026-11-23 §4.4): a row that names a blob is read there, and the NFS file
is the fallback when the row names none or the blob is lost."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest

from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import ByteWrite, OutputRows, output_blob_key
from scadbuddy.library.outputs import (
    FILES_DIR,
    MODEL_NAME,
    THUMBNAIL_NAME,
    OutputMeta,
    OutputNotFoundError,
    OutputStore,
    thumbnail_file_version,
)
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import Pool
from tests.support.arrange import finished_job
from tests.support.store import local_content, store_pool

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    jobs = JobProjection(pg_conninfo, pool_size=2)
    jobs.open()
    try:
        yield jobs
    finally:
        jobs.close()


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


async def _saved(
    tmp_path: Path, projection: JobProjection, content: ContentStore | None
) -> tuple[OutputStore, OutputMeta]:
    paths, job, _ = await finished_job(tmp_path / "data")
    store = OutputStore(paths)
    store.rows = OutputRows(projection.pool)
    if content is not None:
        store.bytes = OutputBytes(content)
    meta = store.create(job, name="first")
    directory = store.directory(meta.id)
    (directory / THUMBNAIL_NAME).write_bytes(b"thumbnail png")
    (directory / FILES_DIR).mkdir(exist_ok=True)
    (directory / FILES_DIR / "a.stl").write_bytes(b"a stl")
    return store, meta


async def _copy_bytes(store: OutputStore, meta: OutputMeta) -> None:
    """What the byte copy does: put the folder, then name the blobs in the row."""
    assert store.bytes is not None and store.rows is not None
    blobs = await store.bytes.put_folder(store.directory(meta.id), meta.id, meta.slug)
    store.rows.write_bytes([ByteWrite(meta.id, blobs, "stamp", None)])


@pytest.fixture
async def copied(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> tuple[OutputStore, OutputMeta]:
    store, meta = await _saved(tmp_path, projection, content)
    await _copy_bytes(store, meta)
    return store, meta


async def _read(store: OutputStore, output_id: str, role: str = "model") -> bytes | None:
    stream = await store.open_role(output_id, role)  # type: ignore[arg-type]
    if stream is None:
        return None
    async with stream:
        return b"".join([chunk async for chunk in stream.chunks])


async def test_open_role_reads_blob_not_file(copied: tuple[OutputStore, OutputMeta]) -> None:
    store, meta = copied
    model = store.directory(meta.id) / MODEL_NAME
    data = model.read_bytes()
    model.unlink()
    stream = await store.open_role(meta.id, "model")
    assert stream is not None
    assert stream.sha256 == hashlib.sha256(data).hexdigest()
    assert stream.size == len(data)
    await stream.aclose()
    assert await _read(store, meta.id) == data
    assert await store.model_3mf(meta.id) == data
    assert store.has_role(meta.id, "model")
    assert store.role_size(meta.id, "model") == len(data)


async def test_open_role_falls_back_to_file_without_key(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    store, meta = await _saved(tmp_path, projection, content)
    data = (store.directory(meta.id) / MODEL_NAME).read_bytes()
    stream = await store.open_role(meta.id, "model")
    assert stream is not None and stream.sha256 is None and stream.size == len(data)
    await stream.aclose()
    assert await _read(store, meta.id) == data
    assert await _read(store, meta.id, "thumbnail") == b"thumbnail png"
    (store.directory(meta.id) / THUMBNAIL_NAME).unlink()
    # Neither a blob nor a file.
    assert await store.open_role(meta.id, "thumbnail") is None
    assert not store.has_role(meta.id, "thumbnail")
    assert store.role_size(meta.id, "thumbnail") is None
    assert await store.open_role("0" * 32, "model") is None


async def test_open_role_falls_back_when_blob_missing(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, meta = copied
    ref = await content.stat(output_blob_key(meta.id, "model"))
    assert ref is not None
    await content.backend.remove(ref.ref.backend_id)
    data = (store.directory(meta.id) / MODEL_NAME).read_bytes()
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.outputs"):
        assert await _read(store, meta.id) == data
        assert await store.model_3mf(meta.id) == data
        async with store.local_role(meta.id, "model") as path:
            assert path == store.directory(meta.id) / MODEL_NAME
    assert "blob" in caplog.text


async def test_local_role_gives_blob_temp_file(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    data = (store.directory(meta.id) / MODEL_NAME).read_bytes()
    async with store.local_role(meta.id, "model") as path:
        assert path is not None and path.parent == content.spool
        assert path.suffix == ".3mf"
        assert path.read_bytes() == data
    assert not path.exists()


async def test_local_role_gives_nfs_path_without_blob(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    store, meta = await _saved(tmp_path, projection, content)
    model = store.directory(meta.id) / MODEL_NAME
    async with store.local_role(meta.id, "model") as path:
        assert path == model
    # Never copied: the NFS file stays.
    assert model.is_file()
    model.unlink()
    async with store.local_role(meta.id, "model") as path:
        assert path is None


async def test_files_from_output_files_rows(copied: tuple[OutputStore, OutputMeta]) -> None:
    store, meta = copied
    directory = store.directory(meta.id) / FILES_DIR
    (directory / "a.stl").unlink()
    (directory / "late.stl").write_bytes(b"not in the rows")
    assert store.files(meta.id) == ["a.stl"]
    stream = await store.open_file(meta.id, "a.stl")
    async with stream:
        assert b"".join([chunk async for chunk in stream.chunks]) == b"a stl"
    with pytest.raises(OutputNotFoundError):
        await store.open_file(meta.id, "missing.stl")


async def test_open_file_refuses_path_names(
    copied: tuple[OutputStore, OutputMeta], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta = copied
    assert store.rows is not None

    def no_read(*args: object) -> None:
        raise AssertionError("a path name reached the rows")

    monkeypatch.setattr(store.rows, "file", no_read)
    for name in ("../meta.json", "sub/a.stl", ".hidden", ""):
        with pytest.raises(OutputNotFoundError):
            await store.open_file(meta.id, name)


async def test_thumbnail_version_is_blob_sha(
    copied: tuple[OutputStore, OutputMeta],
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
) -> None:
    store, meta = copied
    assert store.thumbnail_version(meta.id) == hashlib.sha256(b"thumbnail png").hexdigest()[:16]
    # No blob: the file's identity, as before.
    other, saved = await _saved(tmp_path / "other", projection, content)
    thumbnail = other.directory(saved.id) / THUMBNAIL_NAME
    assert other.thumbnail_version(saved.id) == thumbnail_file_version(thumbnail)
    thumbnail.unlink()
    assert other.thumbnail_version(saved.id) is None


async def test_model_3mf_from_blob(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    data = (store.directory(meta.id) / MODEL_NAME).read_bytes()
    (store.directory(meta.id) / MODEL_NAME).write_bytes(b"the file moved on")
    assert await store.model_3mf(meta.id) == data
    assert await store.model_3mf("0" * 32) is None


async def test_store_without_bytes_reads_files(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    store, meta = await _saved(tmp_path, projection, content)
    await _copy_bytes(store, meta)
    store.bytes = None
    model = store.directory(meta.id) / MODEL_NAME
    model.write_bytes(b"file bytes")
    assert await _read(store, meta.id) == b"file bytes"
    assert await store.model_3mf(meta.id) == b"file bytes"
    async with store.local_role(meta.id, "model") as path:
        assert path == model
    (store.directory(meta.id) / FILES_DIR / "late.stl").write_bytes(b"late")
    assert store.files(meta.id) == ["a.stl", "late.stl"]
    thumbnail = store.directory(meta.id) / THUMBNAIL_NAME
    assert store.thumbnail_version(meta.id) == thumbnail_file_version(thumbnail)
