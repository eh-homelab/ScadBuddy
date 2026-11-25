"""An output's blobs are collected once nothing holds them (#7256, outputs-in-Postgres
spec 2026-11-23 §4.3): a delete releases every byte hold of the output (its roles, its
``files/``, its plate cover), and `sweep_content` removes the blobs after the grace. A
live output's blobs stay, and the hold reaper never releases a live output's holds."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncGenerator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import scadbuddy.library.outputs as outputs_module
from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import (
    OUTPUT_HOLDER,
    OutputRows,
    output_file_key,
    plate_cover_prefix,
)
from scadbuddy.library.outputs import (
    META_NAME,
    OutputMeta,
    OutputStore,
    reap_orphan_holds,
)
from scadbuddy.render.jobs import Job
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.content import ContentStore, sweep_content
from scadbuddy.store.index import Pool
from scadbuddy.store.refs import BlobRefs
from tests.support.arrange import finished_job
from tests.support.store import local_content, store_pool

pytestmark = pytest.mark.requires_postgres

COVER_PNG = b"\x89PNG\r\n\x1a\nplate cover"
DRAWN_PNG = b"\x89PNG\r\n\x1a\ndrawn thumbnail"


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


async def _with_files(tmp_path: Path) -> tuple[OutputStore, Job, Path]:
    """A store with rows and a blob store, and a finished job whose output has a file."""
    paths, job, written = await finished_job(tmp_path / "data")
    files = tmp_path / "extra" / "files"
    files.mkdir(parents=True)
    (files / "a.stl").write_bytes(b"a stl")
    job = job.model_copy(update={"outputs": [written.model_copy(update={"files": ["a.stl"]})]})
    store = OutputStore(paths)
    return store, job, files


def _wire(store: OutputStore, projection: JobProjection, content: ContentStore) -> None:
    store.rows = OutputRows(projection.pool)
    store.bytes = OutputBytes(content)


async def _saved(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[OutputStore, OutputMeta]:
    """A saved output with every kind of output blob: its roles and ``files/`` (the
    save), its plate cover (the first read of the model's fallback cover) and a drawn
    thumbnail (its staged key and its role key)."""
    store, job, files = await _with_files(tmp_path)
    _wire(store, projection, content)
    meta = await store.save(job, name="first", files_dir=files)
    with projection.pool.connection() as conn:
        conn.execute("UPDATE outputs SET has_plate_cover = true WHERE output_id = %s", (meta.id,))
    monkeypatch.setattr(outputs_module, "_read_plate_cover", lambda path: COVER_PNG)
    assert await store.read_plate_cover(meta.slug) == COVER_PNG
    assert await store.put_drawn_thumbnail(meta.id, DRAWN_PNG)
    return store, meta


def _keys(pool: Pool, output_id: str) -> dict[str, str]:
    """Each indexed ``output-<id>-*`` key, and the backend id of its object."""
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key, backend_id FROM store_blobs WHERE starts_with(key, %s)",
            (f"output-{output_id}-",),
        ).fetchall()
    return {str(row["key"]): str(row["backend_id"]) for row in found}


def _holds(pool: Pool, output_id: str) -> set[str]:
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (OUTPUT_HOLDER, output_id),
        ).fetchall()
    return {str(row["key"]) for row in found}


def _age_holds(pool: Pool, output_id: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE blob_refs SET created_at = now() - interval '1 day'"
            " WHERE holder_kind = %s AND holder_id = %s",
            (OUTPUT_HOLDER, output_id),
        )


async def test_deleted_output_blobs_go_after_grace(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = await _saved(tmp_path, projection, content, monkeypatch)
    keys = _keys(pool, meta.id)
    assert any(key.startswith(plate_cover_prefix(meta.id)) for key in keys)
    assert any(key.startswith(f"output-{meta.id}-thumbnail-") for key in keys)
    assert {f"output-{meta.id}-model", output_file_key(meta.id, "a.stl")} <= keys.keys()
    store.delete(meta.id)
    assert _holds(pool, meta.id) == set()
    with pool.connection() as conn:
        derived = conn.execute(
            "SELECT count(*) AS n FROM output_derived WHERE output_id = %s", (meta.id,)
        ).fetchone()
    assert derived is not None and derived["n"] == 0
    removed = await sweep_content(content, BlobRefs(pool), grace=0)
    assert set(keys) <= set(removed)
    assert _keys(pool, meta.id) == {}
    for backend_id in keys.values():
        assert not await content.backend.exists(backend_id)


async def test_live_output_blobs_survive_sweep(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = await _saved(tmp_path, projection, content, monkeypatch)
    held = _holds(pool, meta.id)
    assert any(key.startswith(plate_cover_prefix(meta.id)) for key in held)
    await sweep_content(content, BlobRefs(pool), grace=0)
    # Every held key stays, with its bytes; only the staged thumbnail key (held by
    # nothing) goes, and the role key that shares its object keeps the bytes.
    assert held <= _keys(pool, meta.id).keys()
    assert await store.read_plate_cover(meta.slug) == COVER_PNG
    for key in held:
        assert await store.bytes.read(key)  # type: ignore[union-attr]
    assert store.rows is not None
    row = store.rows.get(meta.id)
    assert row is not None and row.blobs.thumbnail is not None
    assert await store.bytes.read(row.blobs.thumbnail) == DRAWN_PNG  # type: ignore[union-attr]


async def test_model_delete_releases_byte_holds_keeps_part_holds(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = await _saved(tmp_path, projection, content, monkeypatch)
    refs = BlobRefs(pool)
    refs.add("piece-part", OUTPUT_HOLDER, meta.id)
    store.forget_indexed(meta.slug)
    # Ruling R4: the byte holds go with the rows; the Parts' hold stays.
    assert _holds(pool, meta.id) == {"piece-part"}
    assert store.rows is not None and store.rows.get(meta.id) is None


async def test_failed_save_blobs_are_swept(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, job, files = await _with_files(tmp_path)
    _wire(store, projection, content)

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise OSError("the commit failed")

    async def no_delete(key: str) -> None:
        raise OSError("the store went away")

    # The failed save deletes its own blobs at once; when that fails too, the sweep
    # removes them.
    monkeypatch.setattr(store, "_commit", failing)
    monkeypatch.setattr(content, "delete", no_delete)
    output_id = "e" * 32
    with pytest.raises(OSError, match="the commit failed"):
        await store.save(job, name="first", files_dir=files, output_id=output_id)
    monkeypatch.undo()
    keys = _keys(pool, output_id)
    assert any(key.startswith(f"output-{output_id}-model-save-") for key in keys)
    assert f"output-{output_id}-model" not in keys
    assert _holds(pool, output_id) == set()
    await sweep_content(content, BlobRefs(pool), grace=0)
    assert _keys(pool, output_id) == {}
    for backend_id in keys.values():
        assert not await content.backend.exists(backend_id)


async def test_reaper_keeps_holds_of_output_with_row_and_no_meta(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An output whose row stands is live, whatever its folder holds (phase 3 removes the
    folders): the reaper never releases its holds because its ``meta.json`` is gone."""
    store, meta = await _saved(tmp_path, projection, content, monkeypatch)
    refs = BlobRefs(pool)
    outputs_module.hold_parts(refs, meta.id, [], meta.slug)
    held = _holds(pool, meta.id)
    (store.directory(meta.id) / META_NAME).unlink()
    _age_holds(pool, meta.id)
    assert reap_orphan_holds(refs, store, grace=timedelta(0)) == 0
    assert _holds(pool, meta.id) == held
    assert store.directory(meta.id).is_dir()


async def test_reaper_still_releases_holds_of_output_with_no_row(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reaper's own job stays: an output with no row and no ``meta.json`` (a save
    that stopped before its commit) loses its holds past the grace."""
    store, meta = await _saved(tmp_path, projection, content, monkeypatch)
    refs = BlobRefs(pool)
    outputs_module.hold_parts(refs, meta.id, [], meta.slug)
    refs.add("piece-part", OUTPUT_HOLDER, meta.id)
    (store.directory(meta.id) / META_NAME).unlink()
    assert store.rows is not None
    with projection.pool.connection() as conn:
        conn.execute("DELETE FROM outputs WHERE output_id = %s", (meta.id,))
    _age_holds(pool, meta.id)
    assert reap_orphan_holds(refs, store, grace=timedelta(0)) == 1
    assert _holds(pool, meta.id) == set()


async def test_reaper_keeps_folder_of_output_with_row_and_no_holds(
    tmp_path: Path, projection: JobProjection, pool: Pool
) -> None:
    """A row with no holds (a save on a store with no blob store) and no ``meta.json`` is
    still live: the unheld-directory pass leaves its folder."""
    store, job, files = await _with_files(tmp_path)
    store.rows = OutputRows(projection.pool)
    meta = await store.save(job, name="first", files_dir=files)
    assert _holds(pool, meta.id) == set()
    directory = store.directory(meta.id)
    (directory / META_NAME).unlink()
    assert reap_orphan_holds(BlobRefs(pool), store, grace=timedelta(0)) == 0
    assert directory.is_dir()


def _mark(pool: Pool, rows: OutputRows) -> None:
    """The cut-over mark (#7257), over a row copy run and a byte run that finished."""
    with pool.connection() as conn:
        conn.execute("INSERT INTO output_copy_runs (finished_at) VALUES (clock_timestamp())")
        conn.execute("INSERT INTO output_byte_runs (finished_at) VALUES (clock_timestamp())")
    assert rows.try_cut_over() is True


async def test_reaper_after_cut_over_reads_rows_only(
    tmp_path: Path, projection: JobProjection, content: ContentStore, pool: Pool
) -> None:
    """#7257 plan A5: after the mark the reaper scans no folder and reads no
    ``output_hold_slugs`` (release B drops it while release A pods run). Past the grace, a
    deleted output's holds go; a live output's holds and a claimed id's holds stay."""
    store, job, files = await _with_files(tmp_path)
    _wire(store, projection, content)
    rows = store.rows
    assert rows is not None
    live = await store.save(job, name="live", files_dir=files)
    gone = await store.save(job, name="gone")
    refs = BlobRefs(pool)
    live_holds = _holds(pool, live.id)
    assert live_holds
    _mark(pool, rows)
    await asyncio.to_thread(store.delete, gone.id)
    # A hold taken after the delete's release (a byte copy's or a thumbnail's).
    refs.add("piece-late", OUTPUT_HOLDER, gone.id)
    claimed = "c" * 32
    assert rows.claim(claimed, live.slug)
    refs.add("piece-claimed", OUTPUT_HOLDER, claimed)
    for output_id in (live.id, gone.id, claimed):
        _age_holds(pool, output_id)
    with pool.connection() as conn:
        conn.execute("DROP TABLE output_hold_slugs")
    # Any scan of the share raises: the outputs folder is a plain file.
    shutil.rmtree(store.paths.outputs)
    store.paths.outputs.write_text("not a folder")
    assert reap_orphan_holds(refs, store, grace=timedelta(hours=1)) == 1
    assert _holds(pool, gone.id) == set()
    assert _holds(pool, live.id) == live_holds
    assert _holds(pool, claimed) == {"piece-claimed"}


async def test_reaper_after_cut_over_keeps_young_holds_and_drops_stale_claims(
    tmp_path: Path, projection: JobProjection, content: ContentStore, pool: Pool
) -> None:
    """#7257 A5 review M1. A phase 2 pod's save takes no claim: its young holds stay
    while they are younger than the grace. A claim older than the grace no longer
    protects its old holds: they go with it."""
    store, job, _ = await _with_files(tmp_path)
    _wire(store, projection, content)
    rows = store.rows
    assert rows is not None
    await store.save(job, name="live")
    _mark(pool, rows)
    refs = BlobRefs(pool)
    unclaimed = "d" * 32
    refs.add("piece-phase-2", OUTPUT_HOLDER, unclaimed)
    stale = "e" * 32
    assert rows.claim(stale, job.slug)
    refs.add("piece-stale", OUTPUT_HOLDER, stale)
    _age_holds(pool, stale)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE output_claims SET claimed_at = now() - interval '1 day' WHERE output_id = %s",
            (stale,),
        )
    assert reap_orphan_holds(refs, store, grace=timedelta(hours=1)) == 1
    assert _holds(pool, unclaimed) == {"piece-phase-2"}
    assert _holds(pool, stale) == set()
    with pool.connection() as conn:
        left = conn.execute("SELECT 1 FROM output_claims WHERE output_id = %s", (stale,))
        assert left.fetchone() is None
