"""The copy of the saved outputs' files into rows at every API start and in every
housekeeping sweep (#7255, outputs-in-Postgres spec 2026-11-23 §6.1, §6.3, §6.5): it
fills, refreshes, drops, lists what it cannot read, runs on one replica at a time and
stops when told."""

from __future__ import annotations

import dataclasses
import errno
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library import output_copy
from scadbuddy.library.output_copy import COPY_LOCK, CopyResult, copy_outputs
from scadbuddy.library.output_rows import OutputRow, OutputRows
from scadbuddy.library.outputs import (
    BOM_NAME,
    META_NAME,
    PARAMS_NAME,
    RECORD_NAME,
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
    result = copy_outputs(store, rows, projection.conninfo)
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
    copy_outputs(store, rows, projection.conninfo)
    meta_path = paths.output_dir(metas[0].slug, metas[0].id) / META_NAME
    renamed = metas[0].model_copy(update={"name": "renamed by an old pod"})
    meta_path.write_text(renamed.model_dump_json(), encoding="utf-8")
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert (result.copied, result.refreshed, result.dropped) == (0, 1, 0)
    row = rows.get(metas[0].id)
    assert row is not None and row.meta.name == "renamed by an old pod"


def test_copy_second_run_changes_nothing(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.conninfo)
    before = {meta.id: rows.get(meta.id) for meta in metas}
    result = copy_outputs(store, rows, projection.conninfo)
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
    result = copy_outputs(store, rows, projection.conninfo)
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
    result = copy_outputs(store, rows, projection.conninfo)
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
    copy_outputs(store, rows, projection.conninfo)
    shutil.rmtree(paths.output_dir(metas[0].slug, metas[0].id))
    result = copy_outputs(store, rows, projection.conninfo)
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
    copy_outputs(store, rows, projection.conninfo)
    shutil.rmtree(paths.outputs)
    result = copy_outputs(store, rows, projection.conninfo)
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
            assert copy_outputs(store, rows, projection.conninfo) is None
        finally:
            holder.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (COPY_LOCK,))
    assert rows.last_run() is None
    assert copy_outputs(store, rows, projection.conninfo) is not None


def test_copy_stops_unfinished_on_stop(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, _ = files
    store, rows = _stores(paths, projection)
    stop = threading.Event()
    stop.set()
    result = copy_outputs(store, rows, projection.conninfo, stop)
    assert result is not None
    assert result.copied == 0
    assert rows.copied() is False
    last = rows.last_run()
    assert last is not None and last["finished_at"] is None


def test_copy_refreshes_a_changed_file_whatever_its_mtime(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    """The files' stamp, not the NFS server's clock against the database's, says what
    changed: a file whose mtime reads older than the row is still read again."""
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.conninfo)
    meta_path = paths.output_dir(metas[0].slug, metas[0].id) / META_NAME
    renamed = metas[0].model_copy(update={"name": "renamed by a pod with a slow clock"})
    meta_path.write_text(renamed.model_dump_json(), encoding="utf-8")
    long_ago = time.time() - 365 * 24 * 3600
    os.utime(meta_path, (long_ago, long_ago))
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.refreshed == 1
    row = rows.get(metas[0].id)
    assert row is not None and row.meta.name == "renamed by a pod with a slow clock"


def _in_the_gap(
    monkeypatch: pytest.MonkeyPatch,
    store: OutputStore,
    output_id: str,
    action: Callable[[Path], Any],
) -> None:
    """Run ``action`` once, just after the copy read ``output_id``'s folder and before it
    writes that folder's batch: what a live writer on another pod can do."""
    read = store.row_from_files

    def hooked(directory: Path, slug: str) -> Any:
        got = read(directory, slug)
        if directory.name == output_id:
            action(directory)
        return got

    monkeypatch.setattr(store, "row_from_files", hooked)


def _written_by_an_old_pod(paths: DataPaths, meta: OutputMeta) -> None:
    meta_path = paths.output_dir(meta.slug, meta.id) / META_NAME
    old = meta.model_copy(update={"name": "written by an old pod"})
    meta_path.write_text(old.model_dump_json(), encoding="utf-8")
    _touch_later(meta_path)


def test_a_rename_in_the_gap_survives(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.conninfo)
    _written_by_an_old_pod(paths, metas[0])
    _in_the_gap(monkeypatch, store, metas[0].id, lambda _: store.rename(metas[0].id, "live"))
    copy_outputs(store, rows, projection.conninfo)
    row = rows.get(metas[0].id)
    assert row is not None and row.meta.name == "live"


def test_a_delete_in_the_gap_stays_deleted(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.conninfo)
    _written_by_an_old_pod(paths, metas[0])
    _in_the_gap(monkeypatch, store, metas[0].id, lambda _: store.delete(metas[0].id))
    copy_outputs(store, rows, projection.conninfo)
    assert rows.get(metas[0].id) is None
    assert rows.get(metas[1].id) is not None


def test_a_folder_deleted_in_the_gap_gets_no_row(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder with no row yet, removed after the read: the copy's insert is undone."""
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    _in_the_gap(monkeypatch, store, metas[0].id, shutil.rmtree)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert rows.get(metas[0].id) is None
    assert rows.get(metas[1].id) is not None
    assert result.copied == 1


def _live_row(row: OutputRow, name: str) -> OutputRow:
    return dataclasses.replace(row, meta=row.meta.model_copy(update={"name": name}))


def test_a_create_in_the_gap_keeps_its_row(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live save's row wins over the copy's read of the same folder."""
    paths, _, metas = files
    store, rows = _stores(paths, projection)

    def save(directory: Path) -> None:
        row, _ = OutputStore(paths).row_from_files(directory, metas[0].slug)
        assert row is not None
        rows.insert(_live_row(row, "saved live"))

    _in_the_gap(monkeypatch, store, metas[0].id, save)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.copied == 1
    row = rows.get(metas[0].id)
    assert row is not None and row.meta.name == "saved live"


def test_a_save_after_the_copy_wrote_its_folder_succeeds(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    """The copy read a save's folder before the save wrote its row: the save's insert
    replaces the copy's row, and does not fail on the id."""
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.conninfo)
    row = rows.get(metas[0].id)
    assert row is not None
    rows.insert(_live_row(row, "saved live"))
    got = rows.get(metas[0].id)
    assert got is not None and got.meta.name == "saved live"


def test_copy_marks_an_undecodable_side_file(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    (paths.output_dir(metas[0].slug, metas[0].id) / PARAMS_NAME).write_bytes(b"\xff\xfe{")
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.copied == 2
    assert [(d["output_id"], d["file"], d["error"]) for d in result.damaged] == [
        (metas[0].id, PARAMS_NAME, "UnicodeDecodeError")
    ]
    row = rows.get(metas[0].id)
    assert row is not None and row.params == {}


def test_copy_marks_a_directory_where_a_side_file_belongs(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    bom = paths.output_dir(metas[0].slug, metas[0].id) / BOM_NAME
    bom.unlink()
    bom.mkdir()
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.copied == 2
    assert [(d["output_id"], d["file"], d["error"]) for d in result.damaged] == [
        (metas[0].id, BOM_NAME, "IsADirectoryError")
    ]
    row = rows.get(metas[0].id)
    assert row is not None and row.bom is None


def test_copy_marks_a_side_file_it_cannot_read(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    broken = paths.output_dir(metas[0].slug, metas[0].id) / RECORD_NAME
    read_text = Path.read_text

    def failing(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == broken:
            raise OSError(errno.EIO, "Input/output error")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.copied == 2
    assert [(d["output_id"], d["file"], d["error"]) for d in result.damaged] == [
        (metas[0].id, RECORD_NAME, "OSError")
    ]
    row = rows.get(metas[0].id)
    assert row is not None and row.record is None


def test_a_stale_slug_folder_drops_nothing(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listing that fails part-way (ESTALE on one model) is no delete of its rows."""
    paths, job, metas = files
    store, rows = _stores(paths, projection)
    other = store.create(job.model_copy(update={"slug": "other-model"}), name="other")
    copy_outputs(store, rows, projection.conninfo)
    stale = paths.outputs / job.slug
    iterdir = Path.iterdir

    def failing(self: Path) -> Iterator[Path]:
        if self == stale:
            raise OSError(errno.ESTALE, "Stale file handle")
        return iterdir(self)

    monkeypatch.setattr(Path, "iterdir", failing)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.dropped == 0
    every = {*(m.id for m in metas), other.id}
    assert rows.existing(every) == every


def test_a_stale_output_folder_drops_nothing(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.conninfo)
    stale = paths.output_dir(metas[0].slug, metas[0].id)
    stat = Path.stat

    def failing(self: Path, **kwargs: Any) -> os.stat_result:
        if self == stale:
            raise OSError(errno.ESTALE, "Stale file handle")
        return stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", failing)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.dropped == 0
    assert rows.get(metas[0].id) is not None


def test_a_row_written_during_the_run_is_not_dropped(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save on another pod whose folder the walk did not list keeps its row: its
    ``copied_at`` is after the run's start, so `OutputRows.ids_copied_before` leaves it
    out of the rows to drop (the drop's own guard is tested in test_output_rows.py)."""
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    new_id = "e" * 32

    def save(directory: Path) -> None:
        row, _ = OutputStore(paths).row_from_files(directory, metas[0].slug)
        assert row is not None
        meta = row.meta.model_copy(update={"id": new_id})
        rows.insert(dataclasses.replace(row, output_id=new_id, meta=meta))

    _in_the_gap(monkeypatch, store, metas[0].id, save)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.dropped == 0
    assert rows.get(new_id) is not None


def test_a_folder_removed_after_its_batch_loses_the_copys_row(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live delete removes the row, then the folder. When the copy's insert commits
    between the two, and its batch's check ran while the folder was still there, the
    run's end checks the folders of the rows it inserted again and undoes the row."""
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    undo = rows.undo_copy
    calls = 0

    def hooked(output_ids: Any, mark: Any) -> set[str]:
        nonlocal calls
        calls += 1
        got = undo(output_ids, mark)
        if calls == 1:
            # The batch's check is done: the delete's folder removal ends now.
            shutil.rmtree(paths.output_dir(metas[0].slug, metas[0].id))
        return got

    monkeypatch.setattr(rows, "undo_copy", hooked)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert rows.get(metas[0].id) is None
    assert rows.get(metas[1].id) is not None
    assert result.copied == 1
    last = rows.last_run()
    assert last is not None and last["copied"] == 1


def test_a_stop_mid_batch_writes_what_it_read(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _, metas = files
    store, rows = _stores(paths, projection)
    stop = threading.Event()
    read = store.row_from_files

    def hooked(directory: Path, slug: str) -> Any:
        got = read(directory, slug)
        stop.set()
        return got

    monkeypatch.setattr(store, "row_from_files", hooked)
    result = copy_outputs(store, rows, projection.conninfo, stop)
    assert result is not None
    assert result.copied == 1
    assert len(rows.existing(m.id for m in metas)) == 1
    assert rows.copied() is False


def test_a_first_run_with_a_stale_folder_does_not_end_the_fallback(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A walk that missed folders leaves outputs with no row: the run is recorded as
    incomplete, with no ``finished_at``, so a missing row still falls back to the files."""
    paths, job, metas = files
    store, rows = _stores(paths, projection)
    other = OutputStore(paths).create(job.model_copy(update={"slug": "other-model"}), name="other")
    stale = paths.outputs / job.slug
    iterdir = Path.iterdir

    def failing(self: Path) -> Iterator[Path]:
        if self == stale:
            raise OSError(errno.ESTALE, "Stale file handle")
        return iterdir(self)

    monkeypatch.setattr(Path, "iterdir", failing)
    result = copy_outputs(store, rows, projection.conninfo)
    monkeypatch.undo()
    assert result is not None
    assert result.incomplete is True
    assert result.copied == 1
    assert rows.copied() is False
    last = rows.last_run()
    assert last is not None
    assert last["finished_at"] is None
    assert last["incomplete"] is True
    assert last["copied"] == 1
    assert rows.get(other.id) is not None
    # The outputs the walk missed still read from their files.
    assert store.get(metas[0].id).id == metas[0].id


def test_an_outputs_folder_that_reads_empty_is_an_incomplete_run(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, _ = files
    store, rows = _stores(paths, projection)
    copy_outputs(store, rows, projection.conninfo)
    shutil.rmtree(paths.outputs)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.incomplete is True
    last = rows.last_run()
    assert last is not None and last["incomplete"] is True and last["finished_at"] is None


def test_a_first_run_with_no_outputs_folder_does_not_end_the_fallback(
    tmp_path: Path, projection: JobProjection
) -> None:
    """A first run that finds no outputs folder (a volume not mounted) is a failed walk,
    not an empty, complete one: the fallback to the files stays."""
    paths = DataPaths(tmp_path / "data")
    assert not paths.outputs.exists()
    store, rows = _stores(paths, projection)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.incomplete is True
    assert rows.copied() is False
    last = rows.last_run()
    assert last is not None and last["incomplete"] is True and last["finished_at"] is None


def test_a_first_run_with_an_empty_outputs_folder_finishes(
    tmp_path: Path, projection: JobProjection
) -> None:
    """A fresh install: the outputs folder is there and empty, and there is no row. The
    run finishes, so the fallback ends."""
    paths = DataPaths(tmp_path / "data")
    paths.outputs.mkdir(parents=True)
    store, rows = _stores(paths, projection)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None
    assert result.incomplete is False
    assert rows.copied() is True


def test_a_complete_run_is_not_incomplete(
    files: tuple[DataPaths, Job, list[OutputMeta]], projection: JobProjection
) -> None:
    paths, _, _ = files
    store, rows = _stores(paths, projection)
    result = copy_outputs(store, rows, projection.conninfo)
    assert result is not None and result.incomplete is False
    last = rows.last_run()
    assert last is not None and last["incomplete"] is False


def test_the_lock_is_on_a_connection_of_its_own_and_ends_with_the_run(
    files: tuple[DataPaths, Job, list[OutputMeta]],
    projection: JobProjection,
    pg_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run holds its lock for up to 15 minutes: never on a connection of the API's
    pool, so the pool keeps all of its connections (#7360). The run's end releases it."""
    paths, _, _ = files
    store, rows = _stores(paths, projection)
    seen: list[tuple[str, int]] = []

    def run(*args: Any) -> CopyResult:
        with psycopg.connect(pg_conninfo, autocommit=True) as other:
            found = other.execute(
                "SELECT a.application_name FROM pg_locks l"
                " JOIN pg_stat_activity a ON a.pid = l.pid"
                " WHERE l.locktype = 'advisory' AND l.granted"
                " AND a.datname = current_database()"
            ).fetchall()
        seen.extend((str(name), 0) for (name,) in found)
        stats = projection.pool.get_stats()
        seen.append(("pool in use", stats["pool_size"] - stats["pool_available"]))
        return CopyResult()

    monkeypatch.setattr(output_copy, "_copy", run)
    assert copy_outputs(store, rows, projection.conninfo) is not None
    assert seen == [(output_copy.LOCK_APPLICATION, 0), ("pool in use", 0)]
    with psycopg.connect(pg_conninfo, autocommit=True) as other:
        got = other.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (COPY_LOCK,)
        ).fetchone()
        assert got is not None and got[0] is True
