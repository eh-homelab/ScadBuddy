"""`OutputStore` on `OutputRows` (#7255, outputs-in-Postgres spec 2026-11-23 §4.5 phase 1,
§6.1, §6.6): a save writes its row in one transaction, and a lookup by id reads the row.
Before a copy run finished, a missing row falls back to the files; after it, a missing
row is a missing output, found with no glob."""

from __future__ import annotations

import errno
import json
import shutil
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, ModelMeta
from scadbuddy.library.output_index import OutputIndex
from scadbuddy.library.output_rows import OutputRow, OutputRows
from scadbuddy.library.outputs import (
    BACKFILL_NAME,
    META_NAME,
    MODEL_NAME,
    BackfillState,
    OutputMeta,
    OutputNotFoundError,
    OutputStore,
)
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from scadbuddy.render.job_models import Job
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.support.arrange import finished_job
from tests.support.media import MemoryMediaStore

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    jobs = JobProjection(pg_conninfo, pool_size=2)
    jobs.open()
    try:
        yield jobs
    finally:
        jobs.close()


def _store(paths: DataPaths, projection: JobProjection) -> OutputStore:
    outputs = OutputStore(paths)
    outputs.index = OutputIndex(projection.pool, paths)
    outputs.rows = OutputRows(projection.pool)
    return outputs


def rows_of(store: OutputStore) -> OutputRows:
    assert store.rows is not None
    return store.rows


def finish_copy(store: OutputStore) -> None:
    rows = rows_of(store)
    run = rows.start_run()
    rows.finish_run(run, copied=0, refreshed=0, dropped=0, not_copied=[], damaged=[])


@pytest.fixture
async def saved(tmp_path: Path, projection: JobProjection) -> tuple[OutputStore, OutputMeta, Job]:
    paths, job, _ = await finished_job(tmp_path)
    store = _store(paths, projection)
    meta = store.create(job, name="first", inputs={"v": 1, "values": {"width": 12}})
    return store, meta, job


async def test_create_writes_row_with_meta_params_inputs(
    saved: tuple[OutputStore, OutputMeta, Job],
) -> None:
    store, meta, job = saved
    row = rows_of(store).get(meta.id)
    assert row is not None
    assert row.slug == job.slug
    on_disk = OutputMeta.model_validate_json(
        (store.directory(meta.id) / META_NAME).read_text(encoding="utf-8")
    )
    assert row.meta == on_disk
    assert row.params == job.params
    assert row.inputs == store.inputs(meta.id)
    assert row.inputs is not None
    assert row.inputs["v"] == 1
    assert row.record is not None and row.record["output_index"] == 0
    assert row.bom is not None and row.bom[0]["piece"] == "wall"
    assert row.manifest is not None and len(row.manifest) == len(store.manifest(meta.id))
    with zipfile.ZipFile(store.directory(meta.id) / MODEL_NAME) as archive:
        assert row.has_plate_cover == (PLATE_THUMBNAIL in archive.namelist())


async def test_get_reads_row_not_meta_file(
    saved: tuple[OutputStore, OutputMeta, Job],
) -> None:
    store, meta, job = saved
    directory = store.directory(meta.id)
    (directory / META_NAME).unlink()
    (directory / "params.json").unlink()
    got = store.get(meta.id)
    assert got.id == meta.id
    assert got.name == "first"
    assert store.name_and_slug(meta.id) == ("first", job.slug)
    assert store.params(meta.id) == job.params
    assert store.record(meta.id) is not None
    assert store.bom(meta.id)[0].piece == "wall"


async def test_missing_row_after_copy_does_not_glob(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved
    finish_copy(store)

    def no_glob(self: Path, pattern: str) -> Iterator[Path]:
        raise AssertionError(f"globbed {self} for {pattern}")

    monkeypatch.setattr(Path, "glob", no_glob)
    unknown = "f" * 32
    with pytest.raises(OutputNotFoundError):
        store.get(unknown)
    with pytest.raises(OutputNotFoundError):
        store.name_and_slug(unknown)
    with pytest.raises(OutputNotFoundError):
        store.directory(unknown)
    assert store.manifest(unknown) == []
    assert store.provenance(unknown) is None
    # A known output is found with no glob too, and so is its model's list.
    assert store.get(meta.id).id == meta.id
    assert [m.id for m in store.list_for(job.slug)] == [meta.id]
    assert store.pending_backfills() == []


async def test_missing_row_before_copy_falls_back_to_files(
    saved: tuple[OutputStore, OutputMeta, Job],
) -> None:
    store, meta, job = saved
    rows_of(store).delete(meta.id)
    assert not rows_of(store).copied()
    assert store.get(meta.id).name == "first"
    assert store.name_and_slug(meta.id) == ("first", job.slug)
    assert store.params(meta.id) == job.params
    assert store.record(meta.id) is not None
    assert [m.id for m in store.list_for(job.slug)] == [meta.id]
    assert store.existing({meta.id, "e" * 32}) == {meta.id}


async def test_create_fails_and_cleans_up_when_row_insert_fails(
    saved: tuple[OutputStore, OutputMeta, Job],
) -> None:
    store, _, job = saved
    taken = "a" * 32
    other = store.create(job, output_id="b" * 32)
    row = rows_of(store).get(other.id)
    assert row is not None
    rows_of(store).insert(
        replace(row, output_id=taken, meta=row.meta.model_copy(update={"id": taken}))
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.create(job, output_id=taken)
    assert not store.paths.output_dir(job.slug, taken).exists()
    # The other save's row stays: the clean-up deletes only a row the copy wrote.
    assert rows_of(store).get(taken) is not None


async def test_a_failed_save_leaves_no_row_the_copy_wrote(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy read the save's folder and wrote its row, then the save's own insert
    failed: the save removes its folder, and the copy's row goes with it."""
    store, _, job = saved
    rows = rows_of(store)

    def failing(row: OutputRow, conn: object = None) -> None:
        rows.write_copy([replace(row, files_stamp="stamp")], [], rows.clock())
        raise psycopg.OperationalError("the connection was lost")

    monkeypatch.setattr(rows, "insert", failing)
    output_id = "c" * 32
    with pytest.raises(psycopg.OperationalError):
        store.create(job, output_id=output_id)
    assert not store.paths.output_dir(job.slug, output_id).exists()
    assert rows.get(output_id) is None


async def test_rename_updates_row(saved: tuple[OutputStore, OutputMeta, Job]) -> None:
    store, meta, _ = saved
    renamed = store.rename(meta.id, "second")
    assert renamed.name == "second"
    row = rows_of(store).get(meta.id)
    assert row is not None and row.meta.name == "second"
    on_disk = json.loads((store.directory(meta.id) / META_NAME).read_text(encoding="utf-8"))
    assert on_disk["name"] == "second"
    marked = store.mark_storefront(meta.id)
    row = rows_of(store).get(meta.id)
    assert row is not None and row.meta == marked


async def test_backfill_round_trip_through_rows(
    saved: tuple[OutputStore, OutputMeta, Job],
) -> None:
    store, meta, job = saved
    store.start_backfill(meta.id, "rerender")
    row = rows_of(store).get(meta.id)
    assert row is not None and row.backfill == {"job_id": "rerender", "error": None}
    # The row answers, not the file.
    (store.directory(meta.id) / BACKFILL_NAME).unlink()
    assert store.backfill(meta.id) == BackfillState(job_id="rerender")
    finish_copy(store)
    assert store.pending_backfills() == [(meta.id, BackfillState(job_id="rerender"))]
    store.fail_backfill(meta.id, "rerender", "boom")
    assert store.backfill(meta.id) == BackfillState(job_id="rerender", error="boom")
    assert store.pending_backfills() == []
    store.clear_backfill(meta.id)
    assert store.backfill(meta.id) is None
    store.start_backfill(meta.id, "again")
    assert job.outputs
    chosen = job.outputs[0]
    store.attach_backfill(meta.id, chosen)
    row = rows_of(store).get(meta.id)
    assert row is not None
    assert row.backfill is None
    assert row.manifest == [m.model_dump(mode="json") for m in chosen.manifest]
    # The record it had stays, as the file's does.
    assert row.record is not None and row.record["output_index"] == 0


async def test_delete_and_forget_indexed_drop_rows(
    saved: tuple[OutputStore, OutputMeta, Job],
) -> None:
    store, meta, job = saved
    other = store.create(job, name="other")
    store.delete(meta.id)
    assert rows_of(store).get(meta.id) is None
    assert rows_of(store).get(other.id) is not None
    shutil.rmtree(store.paths.outputs / job.slug)
    store.forget_indexed(job.slug)
    assert rows_of(store).get(other.id) is None


async def test_existing_is_one_query_after_copy(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, _ = saved
    finish_copy(store)

    def no_lookup(output_id: str) -> Path:
        raise AssertionError("looked an output up one by one")

    monkeypatch.setattr(store, "directory", no_lookup)
    monkeypatch.setattr(store, "_find_dir", no_lookup)
    assert store.existing([meta.id, "e" * 32]) == {meta.id}
    assert store.existing([]) == set()


async def test_plate_cover_output_from_rows(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved
    rows = rows_of(store)
    row = rows.get(meta.id)
    assert row is not None
    # Before the copy: the scan of the files, as today.
    before = store.plate_cover_output(job.slug)
    assert before == (meta.id if row.has_plate_cover else None)
    finish_copy(store)
    with rows._pool.connection() as conn:
        conn.execute("UPDATE outputs SET has_plate_cover = true WHERE output_id = %s", (meta.id,))
    store.forget_plate_cover(job.slug)

    def no_scan(slug: str) -> Path | None:
        raise AssertionError("scanned the files")

    monkeypatch.setattr(store, "_scan_plate_cover", no_scan)
    assert store.plate_cover_output(job.slug) == meta.id
    assert (
        store.plate_cover_archive(job.slug)
        == store.paths.output_dir(job.slug, meta.id) / MODEL_NAME
    )
    with rows._pool.connection() as conn:
        conn.execute("UPDATE outputs SET has_plate_cover = false")
    assert store.plate_cover_output(job.slug) is None
    assert not store.has_plate_cover(job.slug)


def _catalogue(store: OutputStore) -> Catalogue:
    return Catalogue(
        store.paths, outputs=store, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX
    )


def _refuse(*_: object) -> None:
    raise psycopg.OperationalError("the database went away")


async def test_model_delete_fails_whole_when_its_output_rows_stay(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved
    catalogue = _catalogue(store)
    monkeypatch.setattr(rows_of(store), "delete_slug", _refuse)
    with pytest.raises(psycopg.OperationalError):
        catalogue.delete(job.slug)
    # The model and its outputs stay whole, for a retry.
    assert store.paths.model_source(job.slug).is_file()
    assert store.directory(meta.id).is_dir()
    assert rows_of(store).get(meta.id) is not None
    monkeypatch.undo()
    catalogue.delete(job.slug)
    assert rows_of(store).get(meta.id) is None
    assert not (store.paths.outputs / job.slug).exists()


async def test_a_model_delete_whose_move_fails_puts_its_output_rows_back(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows go before the model's folder moves. When the move fails (EIO, not a
    concurrent delete), the model and its outputs stay, so their rows come back from
    the files, as the copy writes them."""
    store, meta, job = saved
    second = store.create(job, name="second")
    finish_copy(store)
    catalogue = _catalogue(store)
    model_dir = store.paths.model_dir(job.slug)
    rename = Path.rename

    def failing(self: Path, target: Path) -> Path:
        if self == model_dir:
            raise OSError(errno.EIO, "Input/output error")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", failing)
    with pytest.raises(OSError, match="Input/output error"):
        catalogue.delete(job.slug)
    monkeypatch.undo()
    assert store.paths.model_source(job.slug).is_file()
    for kept in (meta, second):
        row = rows_of(store).get(kept.id)
        assert row is not None and row.slug == job.slug
        assert row.files_stamp is not None
        assert store.get(kept.id).name == kept.name
    catalogue.delete(job.slug)
    assert rows_of(store).get(meta.id) is None


async def test_orphan_sweep_keeps_outputs_whose_rows_stay(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved
    catalogue = _catalogue(store)
    shutil.rmtree(store.paths.model_dir(job.slug))
    monkeypatch.setattr(rows_of(store), "delete_slug", _refuse)
    catalogue.sweep_orphans()
    assert store.directory(meta.id).is_dir()
    monkeypatch.undo()
    catalogue.sweep_orphans()
    assert rows_of(store).get(meta.id) is None
    assert not (store.paths.outputs / job.slug).exists()


async def test_a_reused_slug_is_refused_while_its_output_rows_stay(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved
    catalogue = _catalogue(store)
    shutil.rmtree(store.paths.model_dir(job.slug))
    monkeypatch.setattr(rows_of(store), "delete_slug", _refuse)
    with pytest.raises(psycopg.OperationalError):
        catalogue.create(job.slug, "cube();\n", ModelMeta(name="Again"))
    # The claim is given back, and the earlier model's outputs stay for a retry.
    assert not store.paths.model_dir(job.slug).exists()
    assert store.directory(meta.id).is_dir()


async def test_rename_fails_when_its_row_write_fails(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, _ = saved
    monkeypatch.setattr(rows_of(store), "update_meta", _refuse)
    with pytest.raises(psycopg.OperationalError):
        store.rename(meta.id, "second")
    with pytest.raises(psycopg.OperationalError):
        store.mark_storefront(meta.id)


async def test_start_backfill_fails_when_its_row_write_fails(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, _ = saved
    monkeypatch.setattr(rows_of(store), "set_backfill", _refuse)
    with pytest.raises(psycopg.OperationalError):
        store.start_backfill(meta.id, "rerender")
    with pytest.raises(psycopg.OperationalError):
        store.fail_backfill(meta.id, "rerender", "boom")


async def test_lookups_after_a_row_miss_ask_the_rows_once(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved
    rows = rows_of(store)
    rows.delete(meta.id)

    def no_second_ask(output_id: str) -> str | None:
        raise AssertionError("asked the rows again after a miss")

    monkeypatch.setattr(rows, "slug_of", no_second_ask)
    assert store.get(meta.id).name == "first"
    assert store.name_and_slug(meta.id) == ("first", job.slug)
    assert store.params(meta.id) == job.params
    assert store.record(meta.id) is not None
    assert store.bom(meta.id)[0].piece == "wall"
    assert store.manifest(meta.id)
    assert store.inputs(meta.id)["v"] == 1
    assert store.arranged_from(meta.id) == []
    assert store.backfill(meta.id) is None


async def test_a_save_orders_by_the_rows_after_a_copy(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved
    finish_copy(store)
    real_glob = Path.glob

    def no_meta_glob(self: Path, pattern: str) -> Iterator[Path]:
        if pattern.endswith(META_NAME):
            raise AssertionError(f"globbed {self} for {pattern}")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", no_meta_glob)
    later = store.create(job, name="second")
    assert later.created_at > meta.created_at
    assert [m.id for m in store.list_for(job.slug)] == [later.id, meta.id]
