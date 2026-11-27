"""The cut-over (#7257). Release B retired the copies of the files to rows and to blobs
(plan Task B2; #7886 removed their housekeeping activities). Release B writes no folder:
the rows and the blobs are the only record."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest

from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import OutputRows, output_blob_key
from scadbuddy.library.outputs import OutputMeta, OutputStore
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


@pytest.fixture
async def saved(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> tuple[OutputStore, list[OutputMeta]]:
    """Two outputs saved with their rows and their bytes in the blob store."""
    paths, job, _ = await finished_job(tmp_path / "data")
    store = OutputStore(paths, rows=OutputRows(projection.pool), bytes=OutputBytes(content))
    metas = [await store.save(job, name="first"), await store.save(job, name="second")]
    return store, metas


def _folder(store: OutputStore, meta: OutputMeta) -> Path:
    return store.paths.root / "outputs" / meta.slug / meta.id


def _rows(store: OutputStore) -> OutputRows:
    assert store.rows is not None
    return store.rows


THUMBNAIL_PNG = b"\x89PNG\r\n\x1a\nthumbnail after the mark"


async def _without_folder(
    saved: tuple[OutputStore, list[OutputMeta]],
) -> tuple[OutputStore, OutputMeta, Path]:
    """Review Focus 2: the store puts blobs, and the first output has no folder: a release
    B pod saves none (#7257, task B4)."""
    store, metas = saved
    directory = _folder(store, metas[0])
    assert not directory.exists()
    return store, metas[0], directory


async def test_rename_without_folder_after_cut_over(
    saved: tuple[OutputStore, list[OutputMeta]],
) -> None:
    store, meta, directory = await _without_folder(saved)
    renamed = await asyncio.to_thread(store.rename, meta.id, "renamed")
    assert renamed.name == "renamed"
    row = _rows(store).get(meta.id)
    assert row is not None and row.meta.name == "renamed"
    assert not directory.exists()


async def test_put_thumbnail_without_folder_after_cut_over(
    saved: tuple[OutputStore, list[OutputMeta]],
) -> None:
    store, meta, directory = await _without_folder(saved)
    await store.put_thumbnail(meta.id, THUMBNAIL_PNG)
    row = _rows(store).get(meta.id)
    assert row is not None and row.blobs.thumbnail == output_blob_key(meta.id, "thumbnail")
    async with store.local_role(meta.id, "thumbnail") as path:
        assert path is not None and path.read_bytes() == THUMBNAIL_PNG
    assert not directory.exists()


async def test_put_drawn_thumbnail_without_folder_after_cut_over(
    saved: tuple[OutputStore, list[OutputMeta]],
) -> None:
    """The row decides: the drawn thumbnail's key is named while the row names none."""
    store, meta, directory = await _without_folder(saved)
    row = _rows(store).get(meta.id)
    assert row is not None and row.blobs.thumbnail is None
    assert await store.put_drawn_thumbnail(meta.id, THUMBNAIL_PNG) is True
    row = _rows(store).get(meta.id)
    assert row is not None and row.blobs.thumbnail == output_blob_key(meta.id, "thumbnail")
    async with store.local_role(meta.id, "thumbnail") as path:
        assert path is not None and path.read_bytes() == THUMBNAIL_PNG
    assert not directory.exists()


async def test_write_attributed_without_folder_after_cut_over(
    saved: tuple[OutputStore, list[OutputMeta]],
) -> None:
    store, meta, directory = await _without_folder(saved)
    analysis = await store.geometry(meta.id, 1)
    named = analysis.model_copy(update={"height_mm": 7.0})
    sha = await asyncio.to_thread(store.model_sha, meta.id)
    await asyncio.to_thread(store.write_attributed, meta.id, 1, "a" * 40, named, model_sha=sha)
    found = await asyncio.to_thread(store.attributed, meta.id, 1, "a" * 40)
    assert found is not None and found.height_mm == 7.0
    assert not directory.exists()


async def test_backfill_writes_without_folder_after_cut_over(
    tmp_path: Path,
    saved: tuple[OutputStore, list[OutputMeta]],
) -> None:
    store, meta, directory = await _without_folder(saved)
    _, _, chosen = await finished_job(tmp_path / "other")
    store.start_backfill(meta.id, "j9")
    started = store.backfill(meta.id)
    assert started is not None and started.job_id == "j9" and started.error is None
    store.fail_backfill(meta.id, "j9", "boom")
    failed = store.backfill(meta.id)
    assert failed is not None and failed.error == "boom"
    store.clear_backfill(meta.id)
    assert store.backfill(meta.id) is None
    store.start_backfill(meta.id, "j10")
    store.attach_backfill(meta.id, chosen)
    assert store.backfill(meta.id) is None
    assert store.manifest(meta.id) == chosen.manifest
    assert store.record(meta.id) is not None
    assert not directory.exists()
