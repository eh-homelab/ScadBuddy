"""`OutputStore` reads an output's bytes from the blob store only (#7256, #7257,
outputs-in-Postgres spec 2026-11-23 §4.4): a row that names a blob is read there, and a
lost blob raises `OutputBytesLostError` (plan D9); no file is read in its place."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import io
import logging
import threading
import zipfile
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import Connection
from psycopg.errors import DeadlockDetected, UniqueViolation
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

import scadbuddy.store.index as index_module
from scadbuddy.api.outputs import _listed
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.output_bytes import _PREFETCH, OutputBytes
from scadbuddy.library.output_rows import (
    OUTPUT_HOLDER,
    ByteWrite,
    OutputRows,
    lock_thumbnail,
    output_blob_key,
    output_file_key,
)
from scadbuddy.library.outputs import (
    FILES_DIR,
    MODEL_NAME,
    PREVIEW_NAME,
    THUMBNAIL_NAME,
    OutputBytesLostError,
    OutputMeta,
    OutputNotFoundError,
    OutputStore,
    OutputStoreUnusableError,
)
from scadbuddy.render.jobs import Job
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import ReuseLostError, StoreFullError
from scadbuddy.store.index import (
    CLAIM_LOCK_SLOTS,
    BlobIndex,
    Pool,
    StageLockBusyError,
    StageLocks,
    object_lock_name,
)
from scadbuddy.store.local import LocalContentBackend
from tests.support.arrange import finished_job
from tests.support.output_blobs import put_folder_at_keys
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


def _dir(store: OutputStore, output_id: str) -> Path:
    """The folder a test put the output's bytes from (`copied`): no save writes one on the
    share since #7257 (task B4), and no reader reads it."""
    return store.paths.root.parent / "copied" / output_id


@pytest.fixture
async def copied(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> tuple[OutputStore, OutputMeta]:
    """A saved output with a thumbnail and one file, each named in its row as a blob."""
    paths, job, _ = await finished_job(tmp_path / "data")
    store = OutputStore(paths, rows=OutputRows(projection.pool), bytes=OutputBytes(content))
    meta = await store.save(job, name="first")
    directory = _dir(store, meta.id)
    (directory / FILES_DIR).mkdir(parents=True)
    assert store.bytes is not None
    row = store.rows.get(meta.id)
    assert row is not None and row.blobs.model is not None and row.blobs.preview is not None
    (directory / MODEL_NAME).write_bytes(await store.bytes.read(row.blobs.model))
    (directory / PREVIEW_NAME).write_bytes(await store.bytes.read(row.blobs.preview))
    (directory / THUMBNAIL_NAME).write_bytes(b"thumbnail png")
    (directory / FILES_DIR / "a.stl").write_bytes(b"a stl")
    blobs = await put_folder_at_keys(store.bytes, directory, meta.id, meta.slug)
    store.rows.write_byte_copy([ByteWrite(meta.id, blobs, "stamp", [])])
    return store, meta


async def _read(store: OutputStore, output_id: str, role: str = "model") -> bytes | None:
    stream = await store.open_role(output_id, role)  # type: ignore[arg-type]
    if stream is None:
        return None
    async with stream:
        return b"".join([chunk async for chunk in stream.chunks])


async def test_open_role_reads_blob_not_file(copied: tuple[OutputStore, OutputMeta]) -> None:
    store, meta = copied
    model = _dir(store, meta.id) / MODEL_NAME
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


async def test_open_role_of_a_lost_blob_raises(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, meta = copied
    key = output_blob_key(meta.id, "model")
    ref = await content.stat(key)
    assert ref is not None
    await content.backend.remove(ref.ref.backend_id)
    with caplog.at_level(logging.ERROR, logger="scadbuddy.library.outputs"):
        with pytest.raises(OutputBytesLostError) as raised:
            await _read(store, meta.id)
        assert (raised.value.role, raised.value.key) == ("model", key)
        with pytest.raises(OutputBytesLostError):
            await store.model_3mf(meta.id)
        with pytest.raises(OutputBytesLostError):
            async with store.local_role(meta.id, "model"):
                pass
    assert "blob" in caplog.text


async def test_open_file_of_a_lost_blob_raises(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    found = await content.stat(output_file_key(meta.id, "a.stl"))
    assert found is not None
    await content.backend.remove(found.ref.backend_id)
    with pytest.raises(OutputBytesLostError) as raised:
        await store.open_file(meta.id, "a.stl")
    assert raised.value.role == "files/a.stl"


async def test_open_role_of_a_corrupt_blob_raises(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    await flip_first_byte(content, output_blob_key(meta.id, "model"))
    with pytest.raises(OutputBytesLostError):
        await _read(store, meta.id)


async def test_local_role_of_a_corrupt_blob_raises(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    await flip_first_byte(content, output_blob_key(meta.id, "model"))
    with pytest.raises(OutputBytesLostError):
        async with store.local_role(meta.id, "model"):
            pass
    assert list(content.spool.iterdir()) == []


async def test_model_3mf_of_a_corrupt_blob_raises(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    await flip_first_byte(content, output_blob_key(meta.id, "model"))
    with pytest.raises(OutputBytesLostError):
        await store.model_3mf(meta.id)


async def test_open_role_of_a_corrupt_blob_of_exactly_prefetch_raises(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    assert store.bytes is not None
    key = output_blob_key(meta.id, "model")
    await store.bytes.put(key, b"x" * _PREFETCH, slug=meta.slug)
    await flip_first_byte(content, key)
    with pytest.raises(OutputBytesLostError):
        await _read(store, meta.id)


def _unreachable(after: list[bytes], error: Exception | None = None) -> Any:
    """A `ContentStore.get` that gives ``after`` and then loses the connection, or
    raises ``error``."""

    def get(ref: Any) -> AsyncGenerator[bytes]:
        async def chunks() -> AsyncGenerator[bytes]:
            for chunk in after:
                yield chunk
            raise error or EndpointConnectionError(endpoint_url="http://s3.invalid")

        return chunks()

    return get


async def _none_lost(store: OutputStore, meta: OutputMeta, error: type[Exception]) -> None:
    """Every reader of the output's bytes raises ``error`` itself, never
    `OutputBytesLostError`: a store that does not answer now has not lost the blob (#7257
    B3 review)."""
    with pytest.raises(error):
        await _read(store, meta.id)
    with pytest.raises(error):
        await store.model_3mf(meta.id)
    with pytest.raises(error):
        async with store.local_role(meta.id, "model"):
            pass
    with pytest.raises(error):
        await store.open_file(meta.id, "a.stl")


async def test_transport_error_in_prefetch_is_not_a_lost_blob(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = copied
    monkeypatch.setattr(content, "get", _unreachable([b"part"]))
    with caplog.at_level(logging.ERROR, logger="scadbuddy.library.outputs"):
        await _none_lost(store, meta, EndpointConnectionError)
    assert "lost" not in caplog.text


def _s3_error(status: int, code: str) -> ClientError:
    response: Any = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return ClientError(response, "GetObject")


@pytest.mark.parametrize(("status", "code"), [(503, "SlowDown"), (500, "InternalError")])
async def test_s3_server_error_in_prefetch_is_not_a_lost_blob(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
) -> None:
    store, meta = copied
    monkeypatch.setattr(content, "get", _unreachable([], _s3_error(status, code)))
    with caplog.at_level(logging.ERROR, logger="scadbuddy.library.outputs"):
        await _none_lost(store, meta, ClientError)
    assert "lost" not in caplog.text


async def test_s3_client_error_is_not_a_lost_blob(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 4xx (a rotated key's 403) is a fault to see, not a blob to read around.
    store, meta = copied
    monkeypatch.setattr(content, "get", _unreachable([], _s3_error(403, "AccessDenied")))
    with pytest.raises(ClientError):
        await store.open_role(meta.id, "model")


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
    data = (_dir(store, meta.id) / MODEL_NAME).read_bytes()
    async with store.local_role(meta.id, "model") as path:
        assert path is not None and path.parent == content.spool
        assert path.suffix == ".3mf"
        assert path.read_bytes() == data
    assert not path.exists()


async def test_local_role_gives_none_when_the_row_names_no_blob(
    copied: tuple[OutputStore, OutputMeta],
) -> None:
    store, meta = copied
    row = store.rows.get(meta.id)
    assert row is not None
    blobs = dataclasses.replace(row.blobs, model=None)
    store.rows.write_byte_copy([ByteWrite(meta.id, blobs, "stamp", [])])
    # The folder still holds the 3MF; no reader reads it.
    assert (_dir(store, meta.id) / MODEL_NAME).is_file()
    async with store.local_role(meta.id, "model") as path:
        assert path is None
    assert await store.open_role(meta.id, "model") is None
    assert await store.model_3mf(meta.id) is None


async def test_files_from_output_files_rows(copied: tuple[OutputStore, OutputMeta]) -> None:
    store, meta = copied
    directory = _dir(store, meta.id) / FILES_DIR
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

    def no_read(*args: object) -> None:
        raise AssertionError("a path name reached the rows")

    monkeypatch.setattr(store.rows, "file", no_read)
    for name in ("../meta.json", "sub/a.stl", ".hidden", ""):
        with pytest.raises(OutputNotFoundError):
            await store.open_file(meta.id, name)


async def test_thumbnail_version_is_blob_sha(
    copied: tuple[OutputStore, OutputMeta],
) -> None:
    store, meta = copied
    assert store.thumbnail_version(meta.id) == hashlib.sha256(b"thumbnail png").hexdigest()[:16]
    with store.rows._pool.connection() as conn:
        conn.execute("UPDATE outputs SET thumbnail_blob = NULL WHERE output_id = %s", (meta.id,))
    # The file stays; no reader reads it.
    assert (_dir(store, meta.id) / THUMBNAIL_NAME).is_file()
    assert store.thumbnail_version(meta.id) is None


async def test_model_3mf_from_blob(
    copied: tuple[OutputStore, OutputMeta], content: ContentStore
) -> None:
    store, meta = copied
    data = (_dir(store, meta.id) / MODEL_NAME).read_bytes()
    (_dir(store, meta.id) / MODEL_NAME).write_bytes(b"the file moved on")
    assert await store.model_3mf(meta.id) == data
    assert await store.model_3mf("0" * 32) is None


async def test_store_without_bytes_refuses_reads(copied: tuple[OutputStore, OutputMeta]) -> None:
    """An app on the bambuddy store does not start (#7257); a store built with no blob
    store reads no file in their place."""
    store, meta = copied
    store.bytes = None
    with pytest.raises(OutputStoreUnusableError):
        await _read(store, meta.id)
    with pytest.raises(OutputStoreUnusableError):
        await store.model_3mf(meta.id)
    with pytest.raises(OutputStoreUnusableError):
        async with store.local_role(meta.id, "model"):
            pass
    with pytest.raises(OutputStoreUnusableError):
        store.thumbnail_version(meta.id)


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


def _is_3mf(data: bytes) -> bool:
    """Whether ``data`` is a 3MF the save staged: a zip with a model part."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return any(name.endswith(".model") for name in archive.namelist())


def _store(paths: DataPaths, pool: Any, content: ContentStore | None) -> OutputStore:
    return OutputStore(
        paths, rows=OutputRows(pool), bytes=None if content is None else OutputBytes(content)
    )


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
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is not None


async def test_a_save_writes_its_index_rows_in_the_rows_transaction(
    tmp_path: Path, projection: JobProjection, pool: Pool
) -> None:
    """#7477: a save commits once, however many files it has. Its puts upload the
    objects and write no index row; the output's keys are indexed in the row's own
    transaction, and nothing is left to delete after it."""
    paths, job, files = await _with_files(tmp_path)
    index_pool = _Counted(pool)
    content = local_content(tmp_path / "remote", index_pool)  # type: ignore[arg-type]
    content.spool = tmp_path / "spool"
    rows_pool = _Counted(projection.pool)
    store = _store(paths, rows_pool, content)
    try:
        meta = await store.save(job, name="first", files_dir=files)
    finally:
        await content.aclose()
    assert (index_pool.transactions, index_pool.writes_outside) == (0, [])
    assert (rows_pool.transactions, rows_pool.writes_outside) == (1, [])
    assert store.rows is not None
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is not None
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key FROM store_blobs WHERE starts_with(key, %s)", (f"output-{meta.id}-",)
        ).fetchall()
    assert {str(item["key"]) for item in found} == _holds(pool, meta.id)


async def test_a_save_whose_reused_object_was_freed_puts_its_own_copies(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
    spans: InMemorySpanExporter,
) -> None:
    """#7477's rule for a lost reuse: the sweep freed an object the save reused after
    its put found it. The row's transaction rolls back whole, the save uploads its own
    copy of every file it reused, and writes the row once more, reusing nothing. The
    re-uploads are one span with their count (#7756 review)."""
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    first = await store.save(job, name="first", files_dir=files)
    assert store.rows is not None
    insert = store.rows.insert
    reused: list[list[bool]] = []

    def freed_first(row: Any, conn: Any = None) -> Any:
        reused.append([item.reused for item in row.blobs.staged])
        if len(reused) == 1:
            # What the sweep does to the first output's objects meanwhile.
            with pool.connection() as own:
                gone = own.execute(
                    "DELETE FROM store_blobs WHERE starts_with(key, %s) RETURNING backend_id",
                    (f"output-{first.id}-",),
                ).fetchall()
            for item in gone:
                Path(content.backend.root, item["backend_id"]).unlink(missing_ok=True)  # type: ignore[attr-defined]
        return insert(row, conn)

    monkeypatch.setattr(store.rows, "insert", freed_first)
    spans.clear()
    meta = await store.save(job, name="second", files_dir=files)
    assert len(reused) == 2
    assert any(reused[0]) and not any(reused[1])
    [copied] = [s for s in spans.get_finished_spans() if s.name == "outputs.own_copies"]
    assert copied.attributes is not None
    assert copied.attributes["scadbuddy.reused_files"] == sum(reused[0])
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is not None
    assert store.bytes is not None
    assert row.blobs.model is not None and row.blobs.preview is not None
    assert _is_3mf(await store.bytes.read(row.blobs.model))
    assert (await store.bytes.read(row.blobs.preview)).startswith(b"glTF")
    key = output_file_key(meta.id, "a.stl")
    assert await store.bytes.read(key) == b"a stl"


async def test_a_save_runs_its_rows_transaction_again_after_a_deadlock(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7477's rule for a deadlock: Postgres picked the row's transaction as a victim.
    The whole transaction runs again, as `BlobIndex.retrying` runs an index write."""
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    put_in = BlobIndex.put_in
    calls = 0

    def deadlocks_once(self: BlobIndex, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DeadlockDetected("deadlock detected")
        return put_in(self, *args, **kwargs)

    monkeypatch.setattr(BlobIndex, "put_in", deadlocks_once)
    meta = await store.save(job, name="first", files_dir=files)
    monkeypatch.undo()
    assert store.rows is not None
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is not None
    keys = {
        row.blobs.model,
        row.blobs.preview,
        *(item.blob_key for item in store.rows.files(meta.id)),
    }
    assert _holds(pool, meta.id) == keys
    assert store.bytes is not None and row.blobs.model is not None
    assert _is_3mf(await store.bytes.read(row.blobs.model))


async def test_a_save_that_deadlocks_every_time_removes_what_it_uploaded(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7477: the last attempt's deadlock fails the save; it removes its folder and every
    object it uploaded, which no row names."""
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    assert store.bytes is not None
    put_folder = store.bytes.put_folder
    refs: list[Any] = []

    async def kept(*args: Any, **kwargs: Any) -> Any:
        got = await put_folder(*args, **kwargs)
        refs.extend(item.ref for item in got.staged)
        return got

    def deadlocks(self: BlobIndex, *args: Any, **kwargs: Any) -> Any:
        raise DeadlockDetected("deadlock detected")

    monkeypatch.setattr(store.bytes, "put_folder", kept)
    monkeypatch.setattr(BlobIndex, "put_in", deadlocks)
    output_id = "9" * 32
    with pytest.raises(DeadlockDetected):
        await store.save(job, name="first", files_dir=files, output_id=output_id)
    monkeypatch.undo()
    assert not paths.output_dir(job.slug, output_id).exists()
    assert store.rows is not None and store.rows.get(output_id) is None
    assert _holds(pool, output_id) == set()
    assert refs
    for ref in refs:
        assert not await content.backend.exists(ref.backend_id)
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key FROM store_blobs WHERE starts_with(key, %s)", (f"output-{output_id}-",)
        ).fetchall()
    assert found == []


async def test_a_failed_save_keeps_an_object_another_save_has_staged(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7477: two saves upload the same bytes, so the same objects. One fails while the
    other is between its uploads and its commit: the failed one leaves every object the
    other holds (its stage lock), and the other's row reads them back."""
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    assert store.rows is not None
    insert = store.rows.insert
    kept_id, failed_id = "7" * 32, "8" * 32
    staged = threading.Event()
    go_on = threading.Event()

    def held_or_failed(row: Any, conn: Any = None) -> Any:
        if row.output_id == failed_id:
            raise RuntimeError("the insert failed")
        staged.set()
        assert go_on.wait(30)
        return insert(row, conn)

    monkeypatch.setattr(store.rows, "insert", held_or_failed)
    kept = asyncio.create_task(store.save(job, name="kept", files_dir=files, output_id=kept_id))
    try:
        assert await asyncio.to_thread(staged.wait, 30)
        with pytest.raises(RuntimeError, match="the insert failed"):
            await store.save(job, name="failed", files_dir=files, output_id=failed_id)
    finally:
        go_on.set()
    meta = await kept
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is not None and row.blobs.model is not None
    assert store.bytes is not None
    assert _is_3mf(await store.bytes.read(row.blobs.model))
    assert await store.bytes.read(output_file_key(meta.id, "b.stl")) == b"b stl"
    assert store.rows.get(failed_id) is None


async def test_save_row_names_every_blob_and_holds_it(
    tmp_path: Path, projection: JobProjection, content: ContentStore, pool: Pool
) -> None:
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    meta = await store.save(job, name="first", files_dir=files)
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is not None
    assert row.blobs.model == output_blob_key(meta.id, "model")
    assert row.blobs.preview == output_blob_key(meta.id, "preview")
    # No cover was drawn for this save, so no thumbnail.
    assert row.blobs.thumbnail is None
    listed = store.rows.files(meta.id)
    assert [(item.name, item.blob_key) for item in listed] == [
        ("a.stl", output_file_key(meta.id, "a.stl")),
        ("b.stl", output_file_key(meta.id, "b.stl")),
    ]
    keys = {row.blobs.model, row.blobs.preview, *(item.blob_key for item in listed)}
    assert _holds(pool, meta.id) == keys
    assert store.bytes is not None and row.blobs.model is not None
    assert _is_3mf(await store.bytes.read(row.blobs.model))
    assert row.blobs.preview is not None
    assert (await store.bytes.read(row.blobs.preview)).startswith(b"glTF")
    assert await store.bytes.read(output_file_key(meta.id, "b.stl")) == b"b stl"


async def test_save_without_a_blob_store_is_refused(
    tmp_path: Path, projection: JobProjection, pool: Pool
) -> None:
    """#7257: the outputs are kept only as blobs, so a save with no blob store writes
    nothing: no folder and no row."""
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, None)
    output_id = "a" * 32
    with pytest.raises(OutputStoreUnusableError):
        await store.save(job, name="first", files_dir=files, output_id=output_id)
    assert not paths.output_dir(job.slug, output_id).exists()
    assert store.rows.get(output_id) is None
    assert _holds(pool, output_id) == set()


async def test_save_put_failure_removes_folder_and_row(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    stage = content.stage
    refs: list[Any] = []

    async def second_fails(*args: Any, **kwargs: Any) -> Any:
        if refs:
            raise OSError("the store went away")
        ref, reused = await stage(*args, **kwargs)
        refs.append(ref)
        return ref, reused

    monkeypatch.setattr(content, "stage", second_fails)
    output_id = "f" * 32
    with pytest.raises(OSError, match="the store went away"):
        await store.save(job, name="first", files_dir=files, output_id=output_id)
    assert not paths.output_dir(job.slug, output_id).exists()
    assert store.rows.get(output_id) is None
    assert _holds(pool, output_id) == set()
    # The first put's object is removed; no index row ever named it.
    assert len(refs) == 1
    assert not await content.backend.exists(refs[0].backend_id)
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key FROM store_blobs WHERE starts_with(key, %s)", (f"output-{output_id}-",)
        ).fetchall()
    assert found == []


async def test_a_save_whose_stage_lock_stays_claimed_fails_and_frees_its_slot(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    pg_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7756 review: a removal's claim that never ends (a hung remove) fails the save
    that waits for one of its objects' locks, once `STAGE_LOCK_TRIES` tries of
    `STAGE_LOCK_TIMEOUT_MS` ran out: no row, no folder, nothing it uploaded, and its
    stage-lock slot free for the next save."""
    monkeypatch.setattr(index_module, "STAGE_LOCK_TIMEOUT_MS", 50)
    monkeypatch.setattr(index_module, "STAGE_LOCK_TRIES", 2)
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    upload = content.backend.upload
    uploaded: list[str] = []

    async def recorded(*args: Any, **kwargs: Any) -> str:
        backend_id = await upload(*args, **kwargs)
        uploaded.append(backend_id)
        return backend_id

    monkeypatch.setattr(content.backend, "upload", recorded)
    busy = hashlib.sha256(b"b stl").hexdigest()
    claimed = psycopg.connect(pg_conninfo, autocommit=True)
    output_id = "d" * 32
    try:
        claimed.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (object_lock_name(content.name, busy),),
        )
        with pytest.raises(StageLockBusyError):
            await store.save(job, name="first", files_dir=files, output_id=output_id)
    finally:
        claimed.close()
    assert uploaded
    for backend_id in uploaded:
        assert not await content.backend.exists(backend_id)
    assert not paths.output_dir(job.slug, output_id).exists()
    assert store.rows is not None and store.rows.get(output_id) is None
    assert _holds(pool, output_id) == set()
    async with asyncio.timeout(30):
        meta = await store.save(job, name="second", files_dir=files)
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is not None


async def test_a_save_that_loses_on_the_id_keeps_the_outputs_bytes(
    tmp_path: Path, projection: JobProjection, content: ContentStore, pool: Pool
) -> None:
    """A second save of an id an output of another model holds (a quote's uuid5 id,
    `orders/quote.py`) fails on the row, and the output keeps its own bytes: its keys
    take the save's objects only in the row's transaction (#7478 review)."""
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    output_id = "c" * 32
    await store.save(job, name="first", files_dir=files, output_id=output_id)
    model = await store.model_3mf(output_id)
    keys = _holds(pool, output_id)
    before = {key: await content.stat(key) for key in keys}
    other = job.model_copy(update={"slug": "other"})
    with pytest.raises(UniqueViolation):
        await store.save(other, name="second", files_dir=files, output_id=output_id)
    assert not paths.output_dir("other", output_id).exists()
    assert store.get(output_id).slug == job.slug
    assert await store.model_3mf(output_id) == model
    assert _holds(pool, output_id) == keys
    assert {key: await content.stat(key) for key in keys} == before
    # The losing save's own blobs went with it; only the output's keys are left.
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key FROM store_blobs WHERE starts_with(key, %s)", (f"output-{output_id}-",)
        ).fetchall()
    assert {str(row["key"]) for row in found} == keys


async def test_put_thumbnail_puts_blob_then_row(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = copied
    seen: list[bytes | None] = []
    put = content.put

    async def blob_first(*args: Any, **kwargs: Any) -> Any:
        # The row names the old blob while the new one is put.
        seen.append(await _read(store, meta.id, "thumbnail"))
        return await put(*args, **kwargs)

    monkeypatch.setattr(content, "put", blob_first)
    await store.put_thumbnail(meta.id, b"new png")
    assert seen == [b"thumbnail png"]
    assert not (store.paths.output_dir(meta.slug, meta.id) / THUMBNAIL_NAME).exists()
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
    before = store.rows.get(meta.id)
    version = store.thumbnail_version(meta.id)

    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(content, "put", refused)
    with pytest.raises(type(error)):
        await store.put_thumbnail(meta.id, b"new png")
    monkeypatch.undo()
    assert await _read(store, meta.id, "thumbnail") == b"thumbnail png"
    after = store.rows.get(meta.id)
    assert after is not None and before is not None
    assert (after.blobs, after.bytes_at) == (before.blobs, before.bytes_at)
    assert store.thumbnail_version(meta.id) == version


async def test_put_thumbnail_calls_at_once_keep_the_last_step(
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
    assert await _read(store, meta.id, "thumbnail") == b"first"


async def test_put_thumbnail_steps_out_of_order_keep_the_last_step(
    copied: tuple[OutputStore, OutputMeta],
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first put lands first and its step runs last: that step's image is the one the
    # row names.
    store, meta = copied
    put = content.put
    first_landed = asyncio.Event()
    second_done = asyncio.Event()

    async def first_waits(kind: Any, data: Any, **kwargs: Any) -> Any:
        ref = await put(kind, data, **kwargs)
        if data == b"first":
            first_landed.set()
            await second_done.wait()
        return ref

    monkeypatch.setattr(content, "put", first_waits)
    first = asyncio.create_task(store.put_thumbnail(meta.id, b"first"))
    await first_landed.wait()
    await store.put_thumbnail(meta.id, b"second")
    second_done.set()
    await first
    assert await _read(store, meta.id, "thumbnail") == b"first"


async def test_put_thumbnail_without_blob_store_is_refused(
    copied: tuple[OutputStore, OutputMeta],
) -> None:
    store, meta = copied
    store.bytes = None
    with pytest.raises(OutputStoreUnusableError):
        await store.put_thumbnail(meta.id, b"new png")


async def test_put_drawn_thumbnail_keeps_upload(
    tmp_path: Path, projection: JobProjection, content: ContentStore, pool: Pool
) -> None:
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    meta = await store.save(job, name="first", files_dir=files)
    # No thumbnail yet: the draw is kept.
    assert await store.put_drawn_thumbnail(meta.id, b"drawn") is True
    assert await _read(store, meta.id, "thumbnail") == b"drawn"
    key = output_blob_key(meta.id, "thumbnail")
    assert key in _holds(pool, meta.id)
    # An upload lands; a later draw keeps it.
    await store.put_thumbnail(meta.id, b"uploaded")
    assert await store.put_drawn_thumbnail(meta.id, b"drawn again") is False
    assert await _read(store, meta.id, "thumbnail") == b"uploaded"


async def test_put_drawn_thumbnail_failed_put_names_no_blob(
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

    monkeypatch.setattr(content, "put", refused)
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.outputs"):
        assert await store.put_drawn_thumbnail(meta.id, b"drawn") is False
    assert "thumbnail" in caplog.text
    monkeypatch.undo()
    # No file is written: none would be read (#7257). The row names no blob, so a
    # reader finds no thumbnail.
    assert not (paths.output_dir(meta.slug, meta.id) / THUMBNAIL_NAME).exists()
    row = store.rows.get(meta.id)
    assert row is not None and row.blobs.thumbnail is None
    assert await _read(store, meta.id, "thumbnail") is None
    assert output_blob_key(meta.id, "thumbnail") not in _holds(pool, meta.id)


async def test_put_drawn_thumbnail_waits_for_the_thumbnail_lock(
    copied: tuple[OutputStore, OutputMeta], pool: Pool
) -> None:
    """#7256 review A-M1: a drawn thumbnail's step takes the output's thumbnail lock, as
    every writer of the key does."""
    store, meta = copied
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM store_blobs WHERE key = %s", (output_blob_key(meta.id, "thumbnail"),)
        )
        conn.execute("UPDATE outputs SET thumbnail_blob = NULL WHERE output_id = %s", (meta.id,))
    with pool.connection() as held, held.transaction():
        lock_thumbnail(held, meta.id)
        draw = asyncio.create_task(store.put_drawn_thumbnail(meta.id, b"drawn"))
        await asyncio.sleep(0.5)
        # The step waits for the lock: no key yet.
        assert not draw.done()
        assert await _read(store, meta.id, "thumbnail") is None
    assert await draw is True
    assert await _read(store, meta.id, "thumbnail") == b"drawn"


async def test_put_thumbnail_retries_a_deadlock(
    copied: tuple[OutputStore, OutputMeta], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7256 review A-M2: a step Postgres picks as a deadlock's victim runs again, as
    `BlobIndex.retrying` runs an index write again, so the upload does not answer 500."""
    store, meta = copied
    real_put_in = BlobIndex.put_in
    key = output_blob_key(meta.id, "thumbnail")
    calls: list[str] = []

    def deadlocks_once(self: BlobIndex, conn: Any, put: str, *args: Any, **kwargs: Any) -> Any:
        # Only the step's put of the thumbnail key; the staged put has its own retry.
        if put == key:
            calls.append(put)
            if len(calls) == 1:
                raise DeadlockDetected("deadlock detected")
        return real_put_in(self, conn, put, *args, **kwargs)

    monkeypatch.setattr(BlobIndex, "put_in", deadlocks_once)
    await store.put_thumbnail(meta.id, b"new png")
    monkeypatch.undo()
    assert len(calls) == 2
    assert await _read(store, meta.id, "thumbnail") == b"new png"


async def test_save_commit_failure_after_puts_leaves_no_row(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)

    def refused(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the insert failed")

    monkeypatch.setattr(store.rows, "insert", refused)
    output_id = "e" * 32
    with pytest.raises(RuntimeError, match="the insert failed"):
        await store.save(job, name="first", files_dir=files, output_id=output_id)
    assert not paths.output_dir(job.slug, output_id).exists()
    assert store.rows.get(output_id) is None
    assert _holds(pool, output_id) == set()
    # The failed save removed the objects it uploaded; no index row named them.
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key FROM store_blobs WHERE starts_with(key, %s)", (f"output-{output_id}-",)
        ).fetchall()
    assert found == []


async def test_a_listing_reads_its_versions_in_one_query(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7256 review B-M6: a listing's thumbnail and preview versions come from one query
    of the rows and the index, not one index read per output."""
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    metas = [await store.save(job, name=f"n{n}", files_dir=files) for n in range(3)]
    for meta in metas:
        await store.put_thumbnail(meta.id, f"png {meta.id}".encode())
    expected = {
        meta.id: (store.role_version(meta.id, "thumbnail"), store.role_version(meta.id, "preview"))
        for meta in metas
    }
    assert store.bytes is not None
    refs: list[str] = []
    real_ref = store.bytes.ref
    batched: list[int] = []
    real_shas = store.rows.blob_shas

    def ref(key: str) -> Any:
        refs.append(key)
        return real_ref(key)

    def blob_shas(output_ids: Any) -> Any:
        batched.append(len(output_ids))
        return real_shas(output_ids)

    monkeypatch.setattr(store.bytes, "ref", ref)
    monkeypatch.setattr(store.rows, "blob_shas", blob_shas)
    listed = _listed(store, metas, {meta.id: [] for meta in metas}, set())
    assert batched == [3]
    assert refs == []
    assert {item.id: (item.thumbnail_version, item.preview_version) for item in listed} == expected


async def test_a_save_past_the_stage_slots_waits_and_the_index_pool_stays_usable(
    tmp_path: Path, projection: JobProjection, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7477 review I2: a save's stage hold takes a connection of the small lock pool, not
    of the index's (the API's shared pool), and a save waits for a slot before its first
    upload. With every slot held by a save between its uploads and its commit, one more
    save waits, uploads nothing, and the index's pool still serves; then all finish."""
    paths, job, files = await _with_files(tmp_path)
    slots = 2
    with contextlib.ExitStack() as stack:
        index_pool: Pool = stack.enter_context(
            ConnectionPool(
                pg_conninfo,
                min_size=0,
                max_size=slots,
                timeout=5,
                connection_class=Connection[DictRow],
                kwargs={"autocommit": True, "row_factory": dict_row},
            )
        )
        lock_pool: Pool = stack.enter_context(
            ConnectionPool(
                pg_conninfo,
                min_size=0,
                max_size=slots + CLAIM_LOCK_SLOTS,
                timeout=1,
                connection_class=Connection[DictRow],
                kwargs={"autocommit": True, "row_factory": dict_row},
            )
        )
        content = ContentStore(
            LocalContentBackend(tmp_path / "remote"),
            BlobIndex(index_pool, stage_locks=StageLocks(lock_pool, slots)),
        )
        content.spool = tmp_path / "spool"
        store = _store(paths, projection.pool, content)
        assert store.rows is not None
        insert = store.rows.insert
        upload = content.backend.upload
        uploaded: set[str] = set()
        entered: list[str] = []
        all_in = threading.Event()
        go_on = threading.Event()

        async def recorded(*args: Any, name: str, **kwargs: Any) -> str:
            uploaded.add(name.split("-")[1])
            return await upload(*args, name=name, **kwargs)

        def held(row: Any, conn: Any = None) -> Any:
            entered.append(row.output_id)
            if len(entered) == slots:
                all_in.set()
            assert go_on.wait(30)
            return insert(row, conn)

        monkeypatch.setattr(content.backend, "upload", recorded)
        monkeypatch.setattr(store.rows, "insert", held)
        ids = [str(n) * 32 for n in range(1, slots + 2)]
        saves = [
            asyncio.create_task(store.save(job, name=i, files_dir=files, output_id=i)) for i in ids
        ]
        try:
            assert await asyncio.to_thread(all_in.wait, 15)
            await asyncio.sleep(1.5)
            # The extra save waits for a slot, longer than the lock pool would let a
            # connection request wait: it uploaded nothing, and it holds no thread.
            assert uploaded == set(entered)
            with index_pool.connection(timeout=2) as conn:
                assert conn.execute("SELECT 1 AS one").fetchone() == {"one": 1}
        finally:
            go_on.set()
        try:
            metas = await asyncio.wait_for(asyncio.gather(*saves), 30)
            assert {meta.id for meta in metas} == set(ids)
            for meta in metas:
                row = store.rows.get(meta.id)
                assert row is not None and row.bytes_at is not None
        finally:
            await content.aclose()


async def test_a_save_cancelled_during_its_commit_holds_its_objects_until_the_commit_ends(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7477 review I3: a cancel cannot stop the commit's thread, which may still commit
    rows naming the save's objects. The save keeps its stage hold until that thread ends,
    so no release removes those objects meanwhile; then the cancel stands."""
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    assert store.rows is not None
    insert = store.rows.insert
    staged: list[Any] = []
    entered = threading.Event()
    go_on = threading.Event()

    def blocked(row: Any, conn: Any = None) -> Any:
        staged.extend(item.ref for item in row.blobs.staged)
        entered.set()
        assert go_on.wait(30)
        return insert(row, conn)

    monkeypatch.setattr(store.rows, "insert", blocked)
    output_id = "c" * 32
    task = asyncio.create_task(
        store.save(job, name="cancelled", files_dir=files, output_id=output_id)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 30)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.5)
        # The save waits for its commit's thread, still holding its objects.
        assert not done
        async with content.claim_object(staged[0].backend_id) as free:
            assert not free
    finally:
        go_on.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = store.rows.get(output_id)
    assert row is not None and row.blobs.model is not None
    assert store.bytes is not None
    assert _is_3mf(await store.bytes.read(row.blobs.model))


async def test_a_save_cancelled_after_a_lost_reuse_leaves_nothing_staged(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7477 review M4, #7257: a cancel while the save puts its own copies after a lost
    reuse leaves no row, no folder on the share and nothing staged on the spool."""
    paths, job, files = await _with_files(tmp_path)
    store = _store(paths, projection.pool, content)
    assert store.rows is not None and store.bytes is not None
    entered = asyncio.Event()

    def lost(row: Any, conn: Any = None) -> Any:
        raise ReuseLostError("freed meanwhile")

    async def copying(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store.rows, "insert", lost)
    monkeypatch.setattr(store.bytes, "own_copies", copying)
    output_id = "d" * 32
    task = asyncio.create_task(store.save(job, name="lost", files_dir=files, output_id=output_id))
    await asyncio.wait_for(entered.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.rows.get(output_id) is None
    assert not paths.output_dir(job.slug, output_id).exists()
    assert [p for p in store.bytes.spool.iterdir() if p.is_dir()] == []
