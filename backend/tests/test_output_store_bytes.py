"""`OutputStore` reads an output's bytes from the blob store first (#7256, outputs-in-
Postgres spec 2026-11-23 §4.4): a row that names a blob is read there, and the NFS file
is the fallback when the row names none or the blob is lost."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import threading
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from botocore.exceptions import EndpointConnectionError

import scadbuddy.library.outputs as outputs_module
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.output_bytes import _PREFETCH, OutputBytes
from scadbuddy.library.output_rows import (
    OUTPUT_HOLDER,
    ByteWrite,
    OutputRows,
    output_blob_key,
    output_file_key,
)
from scadbuddy.library.outputs import (
    FILES_DIR,
    MODEL_NAME,
    PREVIEW_NAME,
    THUMBNAIL_NAME,
    OutputMeta,
    OutputNotFoundError,
    OutputStore,
    thumbnail_file_version,
)
from scadbuddy.library.outputs import _open_regular as open_regular
from scadbuddy.render.jobs import Job
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import StoreFullError
from scadbuddy.store.index import Pool
from tests.support.arrange import finished_job
from tests.support.store import flip_first_byte, local_content, store_pool

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
    model = store.directory(meta.id) / MODEL_NAME
    # The file's own bytes, so a read of the blob cannot pass as one of the file.
    model.write_bytes(b"file bytes")
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.outputs"):
        assert await _read(store, meta.id) == b"file bytes"
        assert await store.model_3mf(meta.id) == b"file bytes"
        async with store.local_role(meta.id, "model") as path:
            assert path == model
            assert path.read_bytes() == b"file bytes"
    assert "blob" in caplog.text


async def test_open_file_falls_back_when_blob_missing(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    found = await content.stat(output_file_key(meta.id, 0))
    assert found is not None
    await content.backend.remove(found.ref.backend_id)
    (store.directory(meta.id) / FILES_DIR / "a.stl").write_bytes(b"file stl")
    async with await store.open_file(meta.id, "a.stl") as stream:
        assert b"".join([chunk async for chunk in stream.chunks]) == b"file stl"


async def test_open_role_falls_back_when_blob_corrupt(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    await flip_first_byte(content, output_blob_key(meta.id, "model"))
    (store.directory(meta.id) / MODEL_NAME).write_bytes(b"file bytes")
    assert await _read(store, meta.id) == b"file bytes"


async def test_local_role_falls_back_when_blob_corrupt(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    await flip_first_byte(content, output_blob_key(meta.id, "model"))
    model = store.directory(meta.id) / MODEL_NAME
    model.write_bytes(b"file bytes")
    async with store.local_role(meta.id, "model") as path:
        assert path == model
    assert list(content.spool.iterdir()) == []


async def test_model_3mf_falls_back_when_blob_corrupt(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    await flip_first_byte(content, output_blob_key(meta.id, "model"))
    (store.directory(meta.id) / MODEL_NAME).write_bytes(b"file bytes")
    assert await store.model_3mf(meta.id) == b"file bytes"


async def test_open_role_reads_file_while_bytes_at_null(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    store, meta = await _saved(tmp_path, projection, content)
    assert store.bytes is not None and store.rows is not None
    key = output_blob_key(meta.id, "model")
    await store.bytes.put(key, b"blob bytes", slug=meta.slug)
    # A live writer names a blob on a row the byte copy has not copied yet.
    assert store.rows.set_blob(meta.id, "model", key)
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is None
    (store.directory(meta.id) / MODEL_NAME).write_bytes(b"newer file")
    assert await _read(store, meta.id) == b"newer file"
    assert await store.model_3mf(meta.id) == b"newer file"


async def test_file_stream_closes_its_file_unread(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = await _saved(tmp_path, projection, content)
    files: list[BinaryIO] = []

    def recorded(path: Path) -> tuple[BinaryIO, int] | None:
        opened = open_regular(path)
        if opened is not None:
            files.append(opened[0])
        return opened

    monkeypatch.setattr(outputs_module, "_open_regular", recorded)
    stream = await store.open_role(meta.id, "model")
    assert stream is not None and stream.sha256 is None
    assert stream.size == (store.directory(meta.id) / MODEL_NAME).stat().st_size
    assert len(files) == 1 and not files[0].closed
    await stream.aclose()
    assert files[0].closed


async def test_file_stream_cancelled_during_open_closes_its_file(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = await _saved(tmp_path, projection, content)
    started = threading.Event()
    release = threading.Event()
    files: list[BinaryIO] = []

    def slow(path: Path) -> tuple[BinaryIO, int] | None:
        opened = open_regular(path)
        assert opened is not None
        files.append(opened[0])
        started.set()
        release.wait(10)
        return opened

    monkeypatch.setattr(outputs_module, "_open_regular", slow)
    task = asyncio.create_task(store.open_role(meta.id, "model"))
    await asyncio.to_thread(started.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(files) == 1 and not files[0].closed
    release.set()
    for _ in range(200):
        if files[0].closed:
            break
        await asyncio.sleep(0.01)
    # The descriptor the thread opened is closed once the thread returns.
    assert files[0].closed


async def test_open_role_falls_back_when_blob_of_exactly_prefetch_corrupt(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    assert store.bytes is not None
    key = output_blob_key(meta.id, "model")
    await store.bytes.put(key, b"x" * _PREFETCH, slug=meta.slug)
    await flip_first_byte(content, key)
    (store.directory(meta.id) / MODEL_NAME).write_bytes(b"file bytes")
    assert await _read(store, meta.id) == b"file bytes"


def _unreachable(after: list[bytes]) -> Any:
    """A `ContentStore.get` that gives ``after`` and then loses the connection."""

    def get(ref: Any) -> AsyncGenerator[bytes]:
        async def chunks() -> AsyncGenerator[bytes]:
            for chunk in after:
                yield chunk
            raise EndpointConnectionError(endpoint_url="http://s3.invalid")

        return chunks()

    return get


async def test_transport_error_in_prefetch_falls_back_to_file(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = copied
    model = store.directory(meta.id) / MODEL_NAME
    model.write_bytes(b"file bytes")
    monkeypatch.setattr(content, "get", _unreachable([b"part"]))
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.outputs"):
        assert await _read(store, meta.id) == b"file bytes"
        assert await store.model_3mf(meta.id) == b"file bytes"
        async with store.local_role(meta.id, "model") as path:
            assert path == model
        async with await store.open_file(meta.id, "a.stl") as stream:
            assert b"".join([chunk async for chunk in stream.chunks]) == b"a stl"
    assert "blob" in caplog.text


async def test_transport_error_past_prefetch_ends_stream_short(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = copied
    monkeypatch.setattr(content, "get", _unreachable([b"x" * _PREFETCH, b"y", b"z"]))
    stream = await store.open_role(meta.id, "model")
    assert stream is not None and stream.sha256 is not None
    got = 0
    async with stream:
        with pytest.raises(EndpointConnectionError):
            async for chunk in stream.chunks:
                got += len(chunk)
    assert got < _PREFETCH + 2


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


class _Counted:
    """A pool whose connections count their transactions and note any write made
    outside one."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self.transactions = 0
        self.writes_outside: list[str] = []

    @contextlib.contextmanager
    def connection(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        with self._pool.connection(*args, **kwargs) as conn:
            yield _CountedConnection(conn, self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


class _CountedConnection:
    def __init__(self, conn: Any, counted: _Counted) -> None:
        self._conn = conn
        self._counted = counted
        self._depth = 0

    @contextlib.contextmanager
    def transaction(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        if self._depth == 0:
            self._counted.transactions += 1
        self._depth += 1
        try:
            with self._conn.transaction(*args, **kwargs) as tx:
                yield tx
        finally:
            self._depth -= 1

    def note(self, query: Any) -> None:
        words = str(query).split()
        if words and words[0].upper() in {"INSERT", "UPDATE", "DELETE"} and self._depth == 0:
            self._counted.writes_outside.append(str(query))

    def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        self.note(query)
        return self._conn.execute(query, *args, **kwargs)

    @contextlib.contextmanager
    def cursor(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        with self._conn.cursor(*args, **kwargs) as cursor:
            yield _CountedCursor(cursor, self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _CountedCursor:
    def __init__(self, cursor: Any, conn: _CountedConnection) -> None:
        self._cursor = cursor
        self._conn = conn

    def executemany(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        self._conn.note(query)
        return self._cursor.executemany(query, *args, **kwargs)

    def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        self._conn.note(query)
        return self._cursor.execute(query, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


def _holds(pool: Pool, output_id: str) -> set[str]:
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (OUTPUT_HOLDER, output_id),
        ).fetchall()
    return {str(row["key"]) for row in found}


async def _with_files(tmp_path: Path) -> tuple[DataPaths, Job, Path]:
    """A finished job whose output has one extra file, and the folder that holds it."""
    paths, job, written = await finished_job(tmp_path / "data")
    files = tmp_path / "extra" / "files"
    files.mkdir(parents=True)
    (files / "a.stl").write_bytes(b"a stl")
    (files / "b.stl").write_bytes(b"b stl")
    job = job.model_copy(
        update={"outputs": [written.model_copy(update={"files": ["a.stl", "b.stl"]})]}
    )
    return paths, job, files


def _store(paths: DataPaths, pool: Any, content: ContentStore | None) -> OutputStore:
    store = OutputStore(paths)
    store.rows = OutputRows(pool)
    if content is not None:
        store.bytes = OutputBytes(content)
    return store


async def test_save_puts_bytes_and_row_in_one_transaction(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    paths, job, files = await _with_files(tmp_path)
    counted = _Counted(projection.pool)
    store = _store(paths, counted, content)
    meta = await store.save(job, name="first", files_dir=files)
    # One transaction writes the row, its `output_files` rows and the holds.
    assert counted.transactions == 1
    assert counted.writes_outside == []
    row = store.rows.get(meta.id) if store.rows is not None else None
    assert row is not None and row.bytes_at is not None


async def test_save_row_names_every_blob_and_holds_it(
    tmp_path: Path, projection: JobProjection, content: ContentStore, pool: Pool
) -> None:
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    meta = await store.save(job, name="first", files_dir=files)
    assert store.rows is not None
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is not None
    assert row.blobs.model == output_blob_key(meta.id, "model")
    assert row.blobs.preview == output_blob_key(meta.id, "preview")
    # No cover was drawn for this save, so no thumbnail.
    assert row.blobs.thumbnail is None
    listed = store.rows.files(meta.id)
    assert [(item.name, item.blob_key) for item in listed] == [
        ("a.stl", output_file_key(meta.id, 0)),
        ("b.stl", output_file_key(meta.id, 1)),
    ]
    keys = {row.blobs.model, row.blobs.preview, *(item.blob_key for item in listed)}
    assert _holds(pool, meta.id) == keys
    directory = store.directory(meta.id)
    for key, name in (
        (row.blobs.model, MODEL_NAME),
        (row.blobs.preview, PREVIEW_NAME),
        (output_file_key(meta.id, 1), f"{FILES_DIR}/b.stl"),
    ):
        assert key is not None
        assert await store.bytes.read(key) == (directory / name).read_bytes()  # type: ignore[union-attr]


async def test_save_on_bambuddy_store_writes_no_blobs(
    tmp_path: Path, projection: JobProjection, pool: Pool
) -> None:
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, None)
    meta = await store.save(job, name="first", files_dir=files)
    assert store.rows is not None
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is None
    assert (row.blobs.model, row.blobs.preview, row.blobs.thumbnail) == (None, None, None)
    assert store.rows.files(meta.id) == []
    assert _holds(pool, meta.id) == set()
    assert (store.directory(meta.id) / MODEL_NAME).is_file()


async def test_save_put_failure_removes_folder_and_row(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    put = content.put
    calls = 0

    async def second_fails(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("the store went away")
        return await put(*args, **kwargs)

    monkeypatch.setattr(content, "put", second_fails)
    output_id = "f" * 32
    with pytest.raises(OSError, match="the store went away"):
        await store.save(job, name="first", files_dir=files, output_id=output_id)
    assert not paths.output_dir(job.slug, output_id).exists()
    assert store.rows is not None and store.rows.get(output_id) is None
    assert _holds(pool, output_id) == set()


async def test_put_thumbnail_puts_blob_then_file(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = copied
    thumbnail = store.directory(meta.id) / THUMBNAIL_NAME
    seen: list[bytes] = []
    put = content.put

    async def blob_first(*args: Any, **kwargs: Any) -> Any:
        # The file holds the old bytes while the blob is put.
        seen.append(thumbnail.read_bytes())
        return await put(*args, **kwargs)

    monkeypatch.setattr(content, "put", blob_first)
    await store.put_thumbnail(meta.id, b"new png")
    assert seen == [b"thumbnail png"]
    assert thumbnail.read_bytes() == b"new png"
    assert await _read(store, meta.id, "thumbnail") == b"new png"
    key = output_blob_key(meta.id, "thumbnail")
    assert key in _holds(pool, meta.id)
    assert store.thumbnail_version(meta.id) == hashlib.sha256(b"new png").hexdigest()[:16]


@pytest.mark.parametrize("error", [StoreFullError("no room"), OSError("the store went away")])
async def test_put_thumbnail_failed_put_changes_nothing(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    store, meta = copied
    assert store.rows is not None
    before = store.rows.get(meta.id)
    version = store.thumbnail_version(meta.id)

    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(content, "put", refused)
    with pytest.raises(type(error)):
        await store.put_thumbnail(meta.id, b"new png")
    monkeypatch.undo()
    thumbnail = store.directory(meta.id) / THUMBNAIL_NAME
    assert thumbnail.read_bytes() == b"thumbnail png"
    assert await _read(store, meta.id, "thumbnail") == b"thumbnail png"
    after = store.rows.get(meta.id)
    assert after is not None and before is not None
    assert (after.blobs, after.bytes_at) == (before.blobs, before.bytes_at)
    assert store.thumbnail_version(meta.id) == version


async def test_put_thumbnail_calls_at_once_leave_file_and_blob_alike(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = copied
    put = content.put
    first_put = asyncio.Event()
    second_done = asyncio.Event()

    async def out_of_order(kind: Any, data: Any, **kwargs: Any) -> Any:
        if data == b"first":
            first_put.set()
            # The first call's put lands after the second call is done.
            await second_done.wait()
        return await put(kind, data, **kwargs)

    monkeypatch.setattr(content, "put", out_of_order)
    first = asyncio.create_task(store.put_thumbnail(meta.id, b"first"))
    await first_put.wait()
    await store.put_thumbnail(meta.id, b"second")
    second_done.set()
    await first
    thumbnail = store.directory(meta.id) / THUMBNAIL_NAME
    assert await _read(store, meta.id, "thumbnail") == b"first"
    assert thumbnail.read_bytes() == b"first"


async def test_put_thumbnail_step_of_an_older_put_keeps_the_newer_file(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = copied
    put = content.put
    first_landed = asyncio.Event()
    second_done = asyncio.Event()

    async def first_waits(kind: Any, data: Any, **kwargs: Any) -> Any:
        ref = await put(kind, data, **kwargs)
        if data == b"first":
            first_landed.set()
            # The first call's put landed first; its file step runs after the second's.
            await second_done.wait()
        return ref

    monkeypatch.setattr(content, "put", first_waits)
    first = asyncio.create_task(store.put_thumbnail(meta.id, b"first"))
    await first_landed.wait()
    await store.put_thumbnail(meta.id, b"second")
    second_done.set()
    await first
    thumbnail = store.directory(meta.id) / THUMBNAIL_NAME
    assert await _read(store, meta.id, "thumbnail") == b"second"
    assert thumbnail.read_bytes() == b"second"


async def test_put_thumbnail_without_blob_store_writes_file(
    tmp_path: Path, projection: JobProjection
) -> None:
    store, meta = await _saved(tmp_path, projection, None)
    await store.put_thumbnail(meta.id, b"new png")
    assert (store.directory(meta.id) / THUMBNAIL_NAME).read_bytes() == b"new png"
    assert store.rows is not None
    row = store.rows.get(meta.id)
    assert row is not None and row.blobs.thumbnail is None


async def test_put_drawn_thumbnail_keeps_upload(
    tmp_path: Path, projection: JobProjection, content: ContentStore, pool: Pool
) -> None:
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    meta = await store.save(job, name="first", files_dir=files)
    # No thumbnail yet: the draw is kept, file and blob.
    assert await store.put_drawn_thumbnail(meta.id, b"drawn") is True
    assert await _read(store, meta.id, "thumbnail") == b"drawn"
    key = output_blob_key(meta.id, "thumbnail")
    assert key in _holds(pool, meta.id)
    # An upload lands; a later draw keeps it.
    await store.put_thumbnail(meta.id, b"uploaded")
    assert await store.put_drawn_thumbnail(meta.id, b"drawn again") is False
    assert await _read(store, meta.id, "thumbnail") == b"uploaded"
    assert (store.directory(meta.id) / THUMBNAIL_NAME).read_bytes() == b"uploaded"


async def test_put_drawn_thumbnail_failed_put_keeps_the_drawn_file(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    meta = await store.save(job, name="first", files_dir=files)

    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise StoreFullError("no room")

    monkeypatch.setattr(content, "replace", refused)
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.outputs"):
        assert await store.put_drawn_thumbnail(meta.id, b"drawn") is True
    assert "thumbnail" in caplog.text
    monkeypatch.undo()
    assert (store.directory(meta.id) / THUMBNAIL_NAME).read_bytes() == b"drawn"
    # The row names no blob, so readers take the drawn file.
    assert store.rows is not None
    row = store.rows.get(meta.id)
    assert row is not None and row.blobs.thumbnail is None
    assert await _read(store, meta.id, "thumbnail") == b"drawn"
    assert output_blob_key(meta.id, "thumbnail") not in _holds(pool, meta.id)


async def test_save_commit_failure_after_puts_discards_folder_and_row(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    assert store.rows is not None

    def refused(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the insert failed")

    monkeypatch.setattr(store.rows, "insert", refused)
    output_id = "e" * 32
    with pytest.raises(RuntimeError, match="the insert failed"):
        await store.save(job, name="first", files_dir=files, output_id=output_id)
    assert not paths.output_dir(job.slug, output_id).exists()
    assert store.rows.get(output_id) is None
    assert _holds(pool, output_id) == set()
    # The blobs were put, and nothing holds them: the content sweep takes them.
    keys = [
        output_blob_key(output_id, "model"),
        output_blob_key(output_id, "preview"),
        output_file_key(output_id, 0),
        output_file_key(output_id, 1),
    ]
    for key in keys:
        assert await content.stat(key) is not None
    with pool.connection() as conn:
        held = conn.execute("SELECT key FROM blob_refs WHERE key = ANY(%s)", (keys,)).fetchall()
    assert held == []
