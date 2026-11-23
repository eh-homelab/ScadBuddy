"""The copy of the saved outputs' files into rows at every API start (#7255,
outputs-in-Postgres spec 2026-11-23 §6.1, §6.3, §6.5): it fills, refreshes, drops,
lists what it cannot read, runs on one replica at a time and stops when told."""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.output_copy import COPY_LOCK, copy_outputs
from scadbuddy.library.output_rows import OutputRows
from scadbuddy.library.outputs import (
    BOM_NAME,
    META_NAME,
    OutputMeta,
    OutputStore,
)
from scadbuddy.render.job_models import Job
from scadbuddy.render.projection import JobProjection
from tests.support.arrange import finished_job

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
async def files(tmp_path: Path) -> tuple[DataPaths, Job, list[OutputMeta]]:
    """Two outputs saved as files only, as a build before the rows saved them."""
    paths, job, _ = await finished_job(tmp_path)
    store = OutputStore(paths)
    metas = [store.create(job, name="first"), store.create(job, name="second")]
    return paths, job, metas


def _stores(paths: DataPaths, projection: JobProjection) -> tuple[OutputStore, OutputRows]:
    store = OutputStore(paths)
    rows = OutputRows(projection.pool)
    store.rows = rows
    return store, rows


def _touch_later(path: Path) -> None:
    """Make ``path`` newer than any row written so far, whatever the clocks' grain."""
    later = time.time() + 60
    os.utime(path, (later, later))


def test_copy_fills_missing_rows_and_finishes_run(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, job, metas = files
    store, rows = _stores(paths, projection)
    result = copy_outputs(store, rows, projection.pool)
    assert result is not None
    assert result.copied == 2
    assert (result.refreshed, result.dropped) == (0, 0)
    assert result.not_copied == [] and result.damaged == []
    assert rows.copied() is True
    for meta in metas:
        row = rows.get(meta.id)
        assert row is not None
        assert row.slug == job.slug
        assert row.meta == meta
        assert row.params == job.params
        assert row.record is not None and row.manifest
        assert row.bom is not None
    last = rows.last_run()
    assert last is not None and last["copied"] == 2 and last["finished_at"] is not None


def test_copy_refreshes_row_from_newer_meta(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.pool)
    meta_path = paths.output_dir(metas[0].slug, metas[0].id) / META_NAME
    renamed = metas[0].model_copy(update={"name": "renamed by an old pod"})
    meta_path.write_text(renamed.model_dump_json(), encoding="utf-8")
    _touch_later(meta_path)
    result = copy_outputs(store, rows, projection.pool)
    assert result is not None
    assert (result.copied, result.refreshed, result.dropped) == (0, 1, 0)
    row = rows.get(metas[0].id)
    assert row is not None and row.meta.name == "renamed by an old pod"


def test_copy_second_run_changes_nothing(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.pool)
    before = {meta.id: rows.get(meta.id) for meta in metas}
    result = copy_outputs(store, rows, projection.pool)
    assert result is not None
    assert (result.copied, result.refreshed, result.dropped) == (0, 0, 0)
    assert {meta.id: rows.get(meta.id) for meta in metas} == before


def test_copy_lists_unreadable_folder(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    broken = paths.output_dir(metas[0].slug, metas[0].id)
    (broken / META_NAME).write_text("{not json", encoding="utf-8")
    bare = paths.output_dir(metas[0].slug, "f" * 32)
    bare.mkdir()
    result = copy_outputs(store, rows, projection.pool)
    assert result is not None
    assert result.copied == 1
    assert sorted(result.not_copied) == sorted(
        [f"{metas[0].slug}/{metas[0].id}", f"{metas[0].slug}/{'f' * 32}"]
    )
    assert rows.get(metas[0].id) is None
    last = rows.last_run()
    assert last is not None and sorted(last["not_copied"]) == sorted(result.not_copied)


def test_copy_marks_damaged_side_file(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    directory = paths.output_dir(metas[0].slug, metas[0].id)
    (directory / BOM_NAME).write_text("[{half", encoding="utf-8")
    result = copy_outputs(store, rows, projection.pool)
    assert result is not None
    assert result.copied == 2
    assert [(d["output_id"], d["file"]) for d in result.damaged] == [(metas[0].id, BOM_NAME)]
    row = rows.get(metas[0].id)
    assert row is not None
    assert row.bom is None
    assert row.meta == metas[0]
    last = rows.last_run()
    assert last is not None and last["damaged"] == result.damaged


def test_copy_drops_rows_whose_folder_is_gone(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.pool)
    shutil.rmtree(paths.output_dir(metas[0].slug, metas[0].id))
    result = copy_outputs(store, rows, projection.pool)
    assert result is not None
    assert (result.copied, result.refreshed, result.dropped) == (0, 0, 1)
    assert rows.get(metas[0].id) is None
    assert rows.get(metas[1].id) is not None


def test_copy_keeps_rows_when_it_sees_no_folder(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    """An outputs folder that reads empty (a volume not mounted) is no delete."""
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.pool)
    shutil.rmtree(paths.outputs)
    result = copy_outputs(store, rows, projection.pool)
    assert result is not None
    assert result.dropped == 0
    assert rows.existing(m.id for m in metas) == {m.id for m in metas}


def test_copy_runs_once_under_lock(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, _ = files
    store, rows = _stores(paths, projection)
    with projection.pool.connection() as holder:
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (COPY_LOCK,))
        try:
            assert copy_outputs(store, rows, projection.pool) is None
        finally:
            holder.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (COPY_LOCK,))
    assert rows.last_run() is None
    assert copy_outputs(store, rows, projection.pool) is not None


def test_copy_stops_unfinished_on_stop(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, _ = files
    store, rows = _stores(paths, projection)
    stop = threading.Event()
    stop.set()
    result = copy_outputs(store, rows, projection.pool, stop)
    assert result is not None
    assert result.copied == 0
    assert rows.copied() is False
    last = rows.last_run()
    assert last is not None and last["finished_at"] is None
