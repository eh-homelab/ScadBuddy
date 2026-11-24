"""The byte copy of the saved outputs' files into the blob store (#7256, outputs-in-
Postgres spec 2026-11-23 §4.5 phase 2, §6.3-§6.5): it uploads and verifies each byte
role, changes nothing on a second run, records what it cannot copy, never undoes a live
thumbnail, leaves no hold for a deleted output, refuses past the store's cap and on the
`local` and `bambuddy` stores, runs on one replica at a time and stops when told."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.library import output_byte_copy
from scadbuddy.library.output_byte_copy import (
    BYTES_LOCK,
    ByteCopyResult,
    bytes_stamp,
    copy_output_bytes,
)
from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import (
    OUTPUT_HOLDER,
    OutputRows,
    output_blob_key,
    output_file_key,
    plate_cover_key,
)
from scadbuddy.library.outputs import (
    ARRANGED_NAME,
    BACKFILL_NAME,
    FILES_DIR,
    MODEL_NAME,
    PREVIEW_NAME,
    THUMBNAIL_NAME,
    OutputMeta,
    OutputStore,
)
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import Pool
from tests.support.arrange import finished_job
from tests.support.store import local_content, store_pool

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    jobs = JobProjection(pg_conninfo, pool_size=3)
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


class Tree:
    """Five outputs saved as files with rows and no blobs, as phase 1 left them: one
    plain, one arranged, one with ``files/``, one with a backfill and one with no
    ``preview.glb``."""

    def __init__(self, store: OutputStore, rows: OutputRows, metas: dict[str, OutputMeta]):
        self.store = store
        self.rows = rows
        self.metas = metas

    def dir(self, name: str) -> Path:
        return self.store.directory(self.metas[name].id)

    def id(self, name: str) -> str:
        return self.metas[name].id

    def byte_files(self) -> list[Path]:
        """Every byte file of every output: what a first copy puts and reads back."""
        found: list[Path] = []
        for name in self.metas:
            directory = self.dir(name)
            found += [
                directory / file
                for file in (MODEL_NAME, PREVIEW_NAME, THUMBNAIL_NAME)
                if (directory / file).is_file()
            ]
            if (directory / FILES_DIR).is_dir():
                found += sorted((directory / FILES_DIR).iterdir())
        return found


@pytest.fixture
async def tree(tmp_path: Path, projection: JobProjection) -> Tree:
    paths, job, _ = await finished_job(tmp_path / "data")
    store = OutputStore(paths)
    rows = OutputRows(projection.pool)
    store.rows = rows
    plain = store.create(job, name="plain")
    (store.directory(plain.id) / THUMBNAIL_NAME).write_bytes(b"plain thumbnail")
    arranged = store.create(job, name="arranged", arranged_from=[plain.id])
    with_files = store.create(job, name="with files")
    (store.directory(with_files.id) / FILES_DIR).mkdir()
    (store.directory(with_files.id) / FILES_DIR / "a.stl").write_bytes(b"a stl")
    (store.directory(with_files.id) / FILES_DIR / "b.step").write_bytes(b"b step")
    backfill = store.create(job, name="backfill")
    (store.directory(backfill.id) / BACKFILL_NAME).write_text('{"job_id": "j9"}')
    no_preview = store.create(job, name="no preview")
    (store.directory(no_preview.id) / PREVIEW_NAME).unlink()
    metas = {
        "plain": plain,
        "arranged": arranged,
        "files": with_files,
        "backfill": backfill,
        "no_preview": no_preview,
    }
    for meta in metas.values():
        row = rows.get(meta.id)
        assert row is not None and row.bytes_at is None
    assert (store.directory(arranged.id) / ARRANGED_NAME).is_file()
    return Tree(store, rows, metas)


async def _run(
    tree: Tree,
    content: ContentStore | None,
    projection: JobProjection,
    stop: threading.Event | None = None,
) -> ByteCopyResult | None:
    return await copy_output_bytes(tree.store, tree.rows, content, projection.pool, stop)


async def _blob(content: ContentStore, key: str) -> bytes:
    found = await content.stat(key)
    assert found is not None, key
    return await content.read(found.ref)


def _holds(pool: Pool, output_id: str) -> set[str]:
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s"
            " AND starts_with(key, 'output-')",
            (OUTPUT_HOLDER, output_id),
        ).fetchall()
    return {str(item["key"]) for item in found}


def _output_blobs(pool: Pool) -> int:
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT count(*) AS n FROM store_blobs WHERE kind = 'output'"
        ).fetchone()
    assert found is not None
    return int(found["n"])


def _touch_later(path: Path) -> None:
    later = time.time() + 60
    os.utime(path, (later, later))


async def test_byte_copy_uploads_and_verifies_every_role(
    tree: Tree, content: ContentStore, projection: JobProjection, pool: Pool
) -> None:
    result = await _run(tree, content, projection)
    assert result is not None
    assert result.refused is None and not result.incomplete and result.failed == []
    assert result.copied == 5
    # Every blob put is read back: each model, preview, thumbnail and file.
    assert result.verified == len(tree.byte_files())
    for name in ("plain", "arranged", "files", "backfill"):
        row = tree.rows.get(tree.id(name))
        assert row is not None and row.bytes_at is not None
        output_id = tree.id(name)
        assert row.blobs.model == output_blob_key(output_id, "model")
        assert row.blobs.preview == output_blob_key(output_id, "preview")
        assert await _blob(content, row.blobs.model) == (tree.dir(name) / MODEL_NAME).read_bytes()
        assert (
            await _blob(content, row.blobs.preview) == (tree.dir(name) / PREVIEW_NAME).read_bytes()
        )
        assert set(row.blobs.keys()) <= _holds(pool, output_id)
    plain = tree.rows.get(tree.id("plain"))
    assert plain is not None and plain.blobs.thumbnail == output_blob_key(
        tree.id("plain"), "thumbnail"
    )
    assert await _blob(content, plain.blobs.thumbnail) == b"plain thumbnail"
    files = tree.rows.files(tree.id("files"))
    assert [(item.name, item.blob_key) for item in files] == [
        ("a.stl", output_file_key(tree.id("files"), 0)),
        ("b.step", output_file_key(tree.id("files"), 1)),
    ]
    assert await _blob(content, files[1].blob_key) == b"b step"
    last = tree.rows.last_byte_run()
    assert last is not None and last["finished_at"] is not None
    assert last["copied"] == 5 and last["refused"] is None
    assert tree.rows.pending_bytes() == 0


async def test_byte_copy_second_run_changes_nothing(
    tree: Tree, content: ContentStore, projection: JobProjection, pool: Pool
) -> None:
    await _run(tree, content, projection)
    before = {name: tree.rows.get(tree.id(name)) for name in tree.metas}
    blobs = _output_blobs(pool)
    uploads: list[str] = []
    upload = content.backend.upload

    async def counted(*args: Any, **kwargs: Any) -> str:
        uploads.append(kwargs.get("name", ""))
        return await upload(*args, **kwargs)

    content.backend.upload = counted  # type: ignore[method-assign]
    result = await _run(tree, content, projection)
    assert result is not None
    assert (result.copied, result.verified, result.skipped) == (0, 0, 5)
    assert uploads == []
    assert {name: tree.rows.get(tree.id(name)) for name in tree.metas} == before
    assert _output_blobs(pool) == blobs


async def test_byte_copy_corrupt_upload_fails_verification(
    tree: Tree,
    content: ContentStore,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = output_blob_key(tree.id("plain"), "preview")
    verify = OutputBytes.verify

    async def failing(self: OutputBytes, key: str) -> bool:
        return False if key == bad else await verify(self, key)

    monkeypatch.setattr(OutputBytes, "verify", failing)
    result = await _run(tree, content, projection)
    assert result is not None
    assert result.failed == [
        {"output_id": tree.id("plain"), "role": "preview", "error": "the blob is not whole"}
    ]
    row = tree.rows.get(tree.id("plain"))
    assert row is not None and row.bytes_at is None
    others = tree.rows.get(tree.id("arranged"))
    assert others is not None and others.bytes_at is not None
    last = tree.rows.last_byte_run()
    assert last is not None and last["finished_at"] is None
    assert last["failed"] == result.failed
    # Once it verifies, the next run copies it.
    monkeypatch.setattr(OutputBytes, "verify", verify)
    again = await _run(tree, content, projection)
    assert again is not None and again.failed == [] and again.copied == 1
    row = tree.rows.get(tree.id("plain"))
    assert row is not None and row.bytes_at is not None


async def test_byte_copy_marks_missing_preview_damaged(
    tree: Tree, content: ContentStore, projection: JobProjection
) -> None:
    result = await _run(tree, content, projection)
    assert result is not None
    output_id = tree.id("no_preview")
    assert result.damaged == [
        {"output_id": output_id, "role": "preview", "error": f"no {PREVIEW_NAME}"}
    ]
    row = tree.rows.get(output_id)
    assert row is not None and row.bytes_at is not None
    assert row.blobs.model == output_blob_key(output_id, "model") and row.blobs.preview is None
    with projection.pool.connection() as conn:
        found = conn.execute(
            "SELECT damaged FROM outputs WHERE output_id = %s", (output_id,)
        ).fetchone()
    assert found is not None and found["damaged"] == [
        {"role": "preview", "error": f"no {PREVIEW_NAME}"}
    ]
    # A damaged output does not hold the run back.
    last = tree.rows.last_byte_run()
    assert last is not None and last["finished_at"] is not None
    assert last["damaged"] == result.damaged


async def test_byte_copy_marks_bad_file_name_damaged(
    tree: Tree, content: ContentStore, projection: JobProjection, pool: Pool
) -> None:
    output_id = tree.id("files")
    (tree.dir("files") / FILES_DIR / "bad name!").write_bytes(b"x")
    (tree.dir("files") / FILES_DIR / "sub").mkdir()
    result = await _run(tree, content, projection)
    assert result is not None
    damaged = [item for item in result.damaged if item["output_id"] == output_id]
    assert len(damaged) == 1
    assert damaged[0]["role"] == "files" and damaged[0]["names"] == ["bad name!", "sub"]
    row = tree.rows.get(output_id)
    assert row is not None and row.bytes_at is None and row.blobs.model is None
    assert tree.rows.files(output_id) == []
    assert _holds(pool, output_id) == set()
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT damaged FROM outputs WHERE output_id = %s", (output_id,)
        ).fetchone()
        uploaded = conn.execute(
            "SELECT count(*) AS n FROM store_blobs WHERE starts_with(key, %s)",
            (f"output-{output_id}-",),
        ).fetchone()
    assert found is not None and found["damaged"][0]["names"] == ["bad name!", "sub"]
    assert uploaded is not None and uploaded["n"] == 0


async def test_byte_copy_takes_newer_thumbnail_file(
    tree: Tree, content: ContentStore, projection: JobProjection, pool: Pool
) -> None:
    await _run(tree, content, projection)
    output_id = tree.id("plain")
    key = output_blob_key(output_id, "thumbnail")
    old = await content.stat(key)
    assert old is not None
    # An old pod writes the file alone, after the copy took the old one.
    path = tree.dir("plain") / THUMBNAIL_NAME
    path.write_bytes(b"newer thumbnail from an old pod")
    _touch_later(path)
    result = await _run(tree, content, projection)
    assert result is not None and result.failed == [] and result.copied == 1
    assert await _blob(content, key) == b"newer thumbnail from an old pod"
    row = tree.rows.get(output_id)
    assert row is not None and row.blobs.thumbnail == key
    assert key in _holds(pool, output_id)
    # The old object is named now only by its staged key, which nothing holds: the sweep
    # removes it, as it does after a thumbnail upload.
    with pool.connection() as conn:
        naming = conn.execute(
            "SELECT b.key, count(r.key) AS held FROM store_blobs b"
            " LEFT JOIN blob_refs r ON r.key = b.key WHERE b.backend_id = %s GROUP BY b.key",
            (old.ref.backend_id,),
        ).fetchall()
    assert [(row["key"], row["held"]) for row in naming] == [
        (f"output-{output_id}-thumbnail-{old.ref.sha256}", 0)
    ]


@pytest.mark.parametrize("copied_before", [True, False], ids=["copied", "not-copied"])
async def test_byte_copy_does_not_undo_live_thumbnail(
    tree: Tree,
    content: ContentStore,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
    copied_before: bool,
) -> None:
    """On a row copied before, the upload moves ``bytes_at`` and the copy's write of the
    row is skipped; on a row not copied yet, ``bytes_at`` stays NULL, and the thumbnail
    lock and compare-and-swap keep the upload's key."""
    if copied_before:
        await _run(tree, content, projection)
    output_id = tree.id("plain")
    tree.store.bytes = OutputBytes(content)
    path = tree.dir("plain") / THUMBNAIL_NAME
    path.write_bytes(b"old pod thumbnail")
    _touch_later(path)
    put_file = OutputBytes.put_file

    async def racing(self: OutputBytes, key: str, source: Path, *, slug: str) -> Any:
        staged = await put_file(self, key, source, slug=slug)
        # A new pod's upload lands after the copy read the index and put the old pod's
        # file, before the batch names it.
        if key.startswith(f"output-{output_id}-thumbnail-"):
            await tree.store.put_thumbnail(output_id, b"live upload")
        return staged

    monkeypatch.setattr(OutputBytes, "put_file", racing)
    result = await _run(tree, content, projection)
    assert result is not None and result.failed == []
    key = output_blob_key(output_id, "thumbnail")
    assert await _blob(content, key) == b"live upload"
    assert path.read_bytes() == b"live upload"
    row = tree.rows.get(output_id)
    assert row is not None and row.blobs.thumbnail == key


async def test_byte_copy_keeps_thumbnail_key_of_a_put_on_a_row_not_copied(
    tree: Tree, content: ContentStore, projection: JobProjection, pool: Pool
) -> None:
    output_id = tree.id("arranged")
    tree.store.bytes = OutputBytes(content)
    await tree.store.put_thumbnail(output_id, b"put before the copy")
    row = tree.rows.get(output_id)
    key = output_blob_key(output_id, "thumbnail")
    assert row is not None and row.bytes_at is None and row.blobs.thumbnail == key
    # The file goes; the key a put wrote stays.
    (tree.dir("arranged") / THUMBNAIL_NAME).unlink()
    result = await _run(tree, content, projection)
    assert result is not None and result.failed == []
    row = tree.rows.get(output_id)
    assert row is not None and row.bytes_at is not None and row.blobs.thumbnail == key
    assert key in _holds(pool, output_id)


async def test_byte_copy_drops_derived_rows_when_the_model_moves(
    tree: Tree, content: ContentStore, projection: JobProjection, pool: Pool
) -> None:
    await _run(tree, content, projection)
    output_id = tree.id("plain")
    tree.rows.write_derived(output_id, "plate_names", 0, "1", {"1": "old"})
    cover = plate_cover_key(output_id)
    tree.store.bytes = OutputBytes(content)
    await tree.store.bytes.put(cover, b"old cover", slug=tree.metas["plain"].slug)
    tree.rows.keep_plate_cover(output_id, "1", cover)
    # A copy that keeps the model keeps what was derived from it, and its cover's hold.
    path = tree.dir("plain") / THUMBNAIL_NAME
    path.write_bytes(b"another thumbnail")
    _touch_later(path)
    await _run(tree, content, projection)
    assert tree.rows.derived(output_id, "plate_names", 0) is not None
    assert cover in _holds(pool, output_id)
    # A model whose bytes differ from the blob's: what was derived goes with the old blob.
    model = tree.dir("plain") / MODEL_NAME
    model.write_bytes(model.read_bytes() + b"\0")
    _touch_later(model)
    result = await _run(tree, content, projection)
    assert result is not None and result.copied == 1
    assert tree.rows.derived(output_id, "plate_names", 0) is None
    assert tree.rows.derived(output_id, "plate_cover", 0) is None
    assert cover not in _holds(pool, output_id)
    assert await _blob(content, output_blob_key(output_id, "model")) == model.read_bytes()


async def test_byte_copy_leaves_no_hold_for_deleted_output(
    tree: Tree,
    content: ContentStore,
    projection: JobProjection,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_id = tree.id("files")
    replace_file = OutputBytes.replace_file

    async def deleting(self: OutputBytes, key: str, source: Path, **kwargs: Any) -> Any:
        landed = await replace_file(self, key, source, **kwargs)
        if key == output_file_key(output_id, 1):
            tree.rows.delete(output_id)  # a live delete while its bytes go up
        return landed

    monkeypatch.setattr(OutputBytes, "replace_file", deleting)
    result = await _run(tree, content, projection)
    assert result is not None
    assert tree.rows.get(output_id) is None
    assert _holds(pool, output_id) == set()
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT count(*) AS n FROM blob_refs WHERE holder_id = %s", (output_id,)
        ).fetchone()
    assert found is not None and found["n"] == 0
    # Its blobs are indexed and held by nothing: the sweep removes them.
    assert await content.stat(output_blob_key(output_id, "model")) is not None


async def test_byte_copy_refuses_past_cap(
    tree: Tree, projection: JobProjection, pool: Pool, tmp_path: Path
) -> None:
    capped = local_content(tmp_path / "capped", pool, max_total_bytes=1000)
    try:
        result = await _run(tree, capped, projection)
    finally:
        await capped.aclose()
    assert result is not None and result.refused is not None
    assert "SCADBUDDY_STORE_MAX_TOTAL_BYTES (1000)" in result.refused
    last = tree.rows.last_byte_run()
    assert last is not None and last["refused"] == result.refused
    assert last["finished_at"] is None
    needed = int(last["needed_bytes"])
    assert needed > 1000 and str(needed) in result.refused
    assert f"short by {needed - 1000} bytes" in result.refused
    assert last["needed_count"] == len(tree.byte_files())
    assert needed == sum(path.stat().st_size for path in tree.byte_files())
    assert _output_blobs(pool) == 0
    assert tree.rows.pending_bytes() == 5
    capped = local_content(tmp_path / "counted", pool, max_count=3)
    try:
        result = await _run(tree, capped, projection)
    finally:
        await capped.aclose()
    assert result is not None and result.refused is not None
    assert "SCADBUDDY_STORE_MAX_COUNT (3)" in result.refused
    assert f"short by {len(tree.byte_files()) - 3} blobs" in result.refused
    assert _output_blobs(pool) == 0


async def test_byte_copy_refuses_on_bambuddy_and_local(
    tree: Tree, content: ContentStore, projection: JobProjection, pool: Pool
) -> None:
    result = await _run(tree, None, projection)
    assert result is not None
    assert result.refused == "the local store has no blob store; output bytes stay on the share"
    content.backend.backend = "bambuddy"
    result = await _run(tree, content, projection)
    assert result is not None
    assert result.refused == "outputs do not go into Bambuddy's library"
    assert _output_blobs(pool) == 0
    last = tree.rows.last_byte_run()
    assert last is not None and last["refused"] == result.refused and last["finished_at"] is None


async def test_byte_copy_runs_once_under_lock(
    tree: Tree, content: ContentStore, projection: JobProjection, pool: Pool
) -> None:
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (BYTES_LOCK,))
        try:
            assert await _run(tree, content, projection) is None
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (BYTES_LOCK,))
    assert tree.rows.last_byte_run() is None
    assert await _run(tree, content, projection) is not None


async def test_byte_copy_stops_unfinished_on_stop(
    tree: Tree, content: ContentStore, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()
    verify = OutputBytes.verify

    async def stopping(self: OutputBytes, key: str) -> bool:
        stop.set()  # during the first output: the run ends after it
        return await verify(self, key)

    monkeypatch.setattr(OutputBytes, "verify", stopping)
    result = await _run(tree, content, projection, stop)
    assert result is not None and result.copied == 1
    assert tree.rows.pending_bytes() == 4
    last = tree.rows.last_byte_run()
    assert last is not None and last["finished_at"] is None and last["copied"] == 1


async def test_byte_copy_incomplete_on_a_failed_stat(
    tree: Tree, content: ContentStore, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tree.dir("arranged")
    stamp = output_byte_copy.bytes_stamp

    def failing(directory: Path) -> str:
        if directory == broken:
            raise OSError(116, "Stale file handle")
        return stamp(directory)

    monkeypatch.setattr(output_byte_copy, "bytes_stamp", failing)
    result = await _run(tree, content, projection)
    assert result is not None and result.incomplete and result.copied == 4
    last = tree.rows.last_byte_run()
    assert last is not None and last["finished_at"] is None and last["incomplete"] is True


def test_bytes_stamp_names_every_byte_file(tmp_path: Path) -> None:
    (tmp_path / MODEL_NAME).write_bytes(b"m")
    first = bytes_stamp(tmp_path)
    assert bytes_stamp(tmp_path) == first
    (tmp_path / FILES_DIR).mkdir()
    (tmp_path / FILES_DIR / "a.stl").write_bytes(b"a")
    second = bytes_stamp(tmp_path)
    assert second != first
    (tmp_path / FILES_DIR / "a.stl").write_bytes(b"ab")
    assert bytes_stamp(tmp_path) != second
    (tmp_path / "meta.json").write_text("{}")
    third = bytes_stamp(tmp_path)
    (tmp_path / "meta.json").write_text('{"a": 1}')
    assert bytes_stamp(tmp_path) == third  # not a byte file
