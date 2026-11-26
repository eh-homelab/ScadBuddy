"""The cut-over (#7257). Release B retired the copies of the files to rows and to blobs
(plan Task B2): the housekeeping activities `OUTPUT_COPY_SWEEP` and `OUTPUT_BYTES_SWEEP`
stay registered for one release and do nothing. And after the `output_cutover` mark, a
write into an output's folder tolerates a missing folder (plan D1), until release B
stops the folder writes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy import main
from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import OutputRows, output_blob_key
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import Pool
from scadbuddy.workflows.housekeeping import OUTPUT_BYTES_SWEEP, OUTPUT_COPY_SWEEP, SWEEPS
from tests.support.arrange import finished_job
from tests.support.row_outputs import mark_cut_over
from tests.support.store import local_content, store_pool

pytestmark = pytest.mark.requires_postgres

NOTHING_TO_COPY = "the outputs are cut over (#7257); nothing to copy"


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


def _mark(rows: OutputRows) -> None:
    """Write the cut-over mark as release A wrote it: a finished row copy run, then a
    finished byte run with no refusal and no failure."""
    mark_cut_over(rows)


def _runs(pool: Pool) -> tuple[int, int]:
    """How many row copy runs and byte copy runs exist."""
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT (SELECT count(*) FROM output_copy_runs) AS rows,"
            " (SELECT count(*) FROM output_byte_runs) AS bytes"
        ).fetchone()
    assert found is not None
    return int(found["rows"]), int(found["bytes"])


def _outputs(pool: Pool) -> list[tuple[str, Any, Any]]:
    """Each row's id, ``copied_at`` and ``bytes_at``: what a copy would write."""
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT output_id, copied_at, bytes_at FROM outputs ORDER BY output_id"
        ).fetchall()
    return [(str(r["output_id"]), r["copied_at"], r["bytes_at"]) for r in found]


async def test_copy_sweeps_do_nothing(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
    pool: Pool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plan Task B2: both copy activities return, log that the outputs are cut over, and
    touch no table: no run is added, and no row changes, though no output has a folder."""
    store, metas = saved
    before = (_runs(pool), _outputs(pool))
    assert not _folder(store, metas[0]).exists()
    state = SimpleNamespace(
        outputs=store,
        projection=SimpleNamespace(conninfo=projection.conninfo),
        store=SimpleNamespace(content=content),
    )
    sweeps = dict(zip(SWEEPS, main._housekeeping_activities(state), strict=True))  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO, logger="scadbuddy.main"):
        await ActivityEnvironment().run(sweeps[OUTPUT_COPY_SWEEP])
        await ActivityEnvironment().run(sweeps[OUTPUT_BYTES_SWEEP])
    assert (_runs(pool), _outputs(pool)) == before
    assert [r.getMessage() for r in caplog.records].count(NOTHING_TO_COPY) == 2


THUMBNAIL_PNG = b"\x89PNG\r\n\x1a\nthumbnail after the mark"


async def _without_folder(
    saved: tuple[OutputStore, list[OutputMeta]],
) -> tuple[OutputStore, OutputMeta, Path]:
    """Review Focus 2: the mark is written, the store puts blobs, and the first output has
    no folder: a release B pod saves none (#7257, task B4)."""
    store, metas = saved
    _mark(_rows(store))
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
