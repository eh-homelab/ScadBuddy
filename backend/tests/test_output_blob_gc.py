"""An output's blobs are collected once nothing holds them (#7256, outputs-in-Postgres
spec 2026-11-23 §4.3): a delete releases every byte hold of the output (its roles, its
``files/``, its plate cover), and `sweep_content` removes the blobs after the grace. A
live output's blobs stay, and the hold reaper never releases a live output's holds."""

from __future__ import annotations

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
    assert {f"output-{meta.id}-model", f"output-{meta.id}-file-0"} <= keys.keys()
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

    monkeypatch.setattr(store, "commit", failing)
    output_id = "e" * 32
    with pytest.raises(OSError, match="the commit failed"):
        await store.save(job, name="first", files_dir=files, output_id=output_id)
    keys = _keys(pool, output_id)
    assert f"output-{output_id}-model" in keys
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
