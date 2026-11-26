"""`OutputStore` on `OutputRows` (#7255, outputs-in-Postgres spec 2026-11-23 §4.5 phase 1,
§6.1, §6.6): a save writes its row in one transaction, and a lookup by id reads the row.
A missing row is a missing output, found with no glob (#7257)."""

from __future__ import annotations

import errno
import shutil
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scadbuddy.api.outputs import detail
from scadbuddy.bambuddy.project_file import cached_defaults
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, ModelMeta
from scadbuddy.library.output_query import params_text
from scadbuddy.library.output_rows import (
    OutputRow,
    OutputRows,
)
from scadbuddy.library.outputs import (
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
from tests.support.row_outputs import output_store

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
    return output_store(paths.root.parent, projection.pool, paths=paths)


def rows_of(store: OutputStore) -> OutputRows:
    return store.rows


def _dir(store: OutputStore, meta: OutputMeta) -> Path:
    """The output's folder on the old share, which no save writes since #7257 (task B4)
    and no reader reads."""
    return store.paths.root / "outputs" / meta.slug / meta.id


@pytest.fixture
async def saved(tmp_path: Path, projection: JobProjection) -> tuple[OutputStore, OutputMeta, Job]:
    paths, job, _ = await finished_job(tmp_path)
    store = _store(paths, projection)
    meta = await store.save(job, name="first", inputs={"v": 1, "values": {"width": 12}})
    return store, meta, job


async def test_create_writes_row_with_meta_params_inputs(
    saved: tuple[OutputStore, OutputMeta, Job],
) -> None:
    store, meta, job = saved
    row = rows_of(store).get(meta.id)
    assert row is not None
    assert row.slug == job.slug
    assert row.meta == meta
    assert not _dir(store, meta).exists()
    assert row.params == job.params
    assert row.inputs == store.inputs(meta.id)
    assert row.inputs is not None
    assert row.inputs["v"] == 1
    assert row.record is not None and row.record["output_index"] == 0
    assert row.bom is not None and row.bom[0]["piece"] == "wall"
    assert row.manifest is not None and len(row.manifest) == len(store.manifest(meta.id))
    async with store.local_role(meta.id, "model") as model:
        assert model is not None
        with zipfile.ZipFile(model) as archive:
            assert row.has_plate_cover == (PLATE_THUMBNAIL in archive.namelist())


async def test_save_writes_params_text(tmp_path: Path, projection: JobProjection) -> None:
    """#7257: the row carries the list's parameter text."""
    paths, job, _ = await finished_job(tmp_path, width=30)
    job = job.model_copy(update={"params": {"width": 30}})
    store = _store(paths, projection)
    meta = await store.save(job)
    row = rows_of(store).get(meta.id)
    assert row is not None
    expected = params_text(job.params, cached_defaults(store.paths, job.slug))
    assert "width=30" in expected and row.params_text == expected


async def test_missing_row_does_not_glob(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved

    def no_glob(self: Path, pattern: str) -> Iterator[Path]:
        raise AssertionError(f"globbed {self} for {pattern}")

    monkeypatch.setattr(Path, "glob", no_glob)
    unknown = "f" * 32
    with pytest.raises(OutputNotFoundError):
        store.get(unknown)
    with pytest.raises(OutputNotFoundError):
        store.name_and_slug(unknown)
    assert store.manifest(unknown) == []
    # A known output is found with no glob too, and so is its model's list.
    assert store.get(meta.id).id == meta.id
    assert [m.id for m in store.list_for(job.slug)] == [meta.id]
    assert store.pending_backfills() == []


async def test_create_fails_and_cleans_up_when_row_insert_fails(
    saved: tuple[OutputStore, OutputMeta, Job],
) -> None:
    store, _, job = saved
    taken = "a" * 32
    other = await store.save(job, output_id="b" * 32)
    row = rows_of(store).get(other.id)
    assert row is not None
    rows_of(store).insert(
        replace(row, output_id=taken, meta=row.meta.model_copy(update={"id": taken}))
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        await store.save(job, output_id=taken)
    assert not (store.paths.root / "outputs" / job.slug / taken).exists()
    # The row that holds the id stays.
    assert rows_of(store).get(taken) is not None


async def test_rename_updates_row(saved: tuple[OutputStore, OutputMeta, Job]) -> None:
    store, meta, _ = saved
    renamed = store.rename(meta.id, "second")
    assert renamed.name == "second"
    row = rows_of(store).get(meta.id)
    assert row is not None and row.meta.name == "second"
    assert not _dir(store, meta).exists()


async def test_backfill_round_trip_through_rows(
    saved: tuple[OutputStore, OutputMeta, Job],
) -> None:
    store, meta, job = saved
    store.start_backfill(meta.id, "rerender")
    row = rows_of(store).get(meta.id)
    assert row is not None and row.backfill == {"job_id": "rerender", "error": None}
    assert not _dir(store, meta).exists()
    assert store.backfill(meta.id) == BackfillState(job_id="rerender")
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
    other = await store.save(job, name="other")
    store.delete(meta.id)
    assert rows_of(store).get(meta.id) is None
    assert rows_of(store).get(other.id) is not None
    store.forget_indexed(job.slug)
    assert rows_of(store).get(other.id) is None


async def test_existing_is_one_query(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, _ = saved

    def no_lookup(output_id: str) -> OutputRow | None:
        raise AssertionError("looked an output up one by one")

    monkeypatch.setattr(rows_of(store), "get", no_lookup)
    assert store.existing([meta.id, "e" * 32]) == {meta.id}
    assert store.existing([]) == set()


async def test_plate_cover_output_from_rows(
    saved: tuple[OutputStore, OutputMeta, Job],
) -> None:
    store, meta, job = saved
    rows = rows_of(store)
    with rows._pool.connection() as conn:
        conn.execute("UPDATE outputs SET has_plate_cover = true WHERE output_id = %s", (meta.id,))
    assert not (store.paths.root / "outputs").exists()  # #7257: no save makes it
    assert store.plate_cover_output(job.slug) == meta.id
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


async def test_model_delete_leaves_output_rows_it_cannot_drop_to_the_sweep(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7257: the model's folder moves first, then its output rows go. When they cannot,
    the delete still stands, logged, and the orphan sweep drops them later."""
    store, meta, job = saved
    catalogue = _catalogue(store)
    monkeypatch.setattr(rows_of(store), "delete_slug", _refuse)
    catalogue.delete(job.slug)
    assert not store.paths.model_dir(job.slug).exists()
    assert rows_of(store).get(meta.id) is not None
    monkeypatch.undo()
    assert catalogue.sweep_orphans(deleted=(job.slug,)) == [f"outputs/{job.slug}"]
    assert rows_of(store).get(meta.id) is None


async def test_a_model_delete_whose_move_fails_keeps_its_output_rows(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7257: the model's folder moves before its rows go. When the move fails (EIO, not
    a concurrent delete), the model and its output rows stay as they were."""
    store, meta, job = saved
    second = await store.save(job, name="second")
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
        assert store.get(kept.id).name == kept.name
    catalogue.delete(job.slug)
    assert rows_of(store).get(meta.id) is None


async def test_orphan_sweep_keeps_outputs_whose_rows_stay(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved
    catalogue = _catalogue(store)
    shutil.rmtree(store.paths.model_dir(job.slug))
    # A live template besides: a models directory with none sweeps nothing (#7649).
    store.paths.model_dir("kept").mkdir()
    store.paths.model_source("kept").write_text("cube(1);\n", encoding="utf-8")
    monkeypatch.setattr(rows_of(store), "delete_slug", _refuse)
    catalogue.sweep_orphans()
    assert rows_of(store).get(meta.id) is not None
    monkeypatch.undo()
    catalogue.sweep_orphans()
    assert rows_of(store).get(meta.id) is None


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
    assert rows_of(store).get(meta.id) is not None


async def test_rename_fails_when_its_row_write_fails(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, _ = saved
    monkeypatch.setattr(rows_of(store), "update_meta", _refuse)
    with pytest.raises(psycopg.OperationalError):
        store.rename(meta.id, "second")


async def test_start_backfill_fails_when_its_row_write_fails(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, _ = saved
    monkeypatch.setattr(rows_of(store), "set_backfill", _refuse)
    with pytest.raises(psycopg.OperationalError):
        store.start_backfill(meta.id, "rerender")
    with pytest.raises(psycopg.OperationalError):
        store.fail_backfill(meta.id, "rerender", "boom")


async def test_a_save_orders_by_the_rows(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved
    real_glob = Path.glob

    def no_meta_glob(self: Path, pattern: str) -> Iterator[Path]:
        if pattern.endswith("meta.json"):
            raise AssertionError(f"globbed {self} for {pattern}")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", no_meta_glob)
    later = await store.save(job, name="second")
    assert later.created_at > meta.created_at
    assert [m.id for m in store.list_for(job.slug)] == [later.id, meta.id]


def _counting_connections(monkeypatch: pytest.MonkeyPatch, store: OutputStore) -> list[int]:
    pool = rows_of(store)._pool
    taken: list[int] = []
    connection = pool.connection

    def counted(*args: Any, **kwargs: Any) -> Any:
        taken.append(1)
        return connection(*args, **kwargs)

    monkeypatch.setattr(pool, "connection", counted)
    return taken


async def test_an_output_detail_reads_its_row_once(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`detail` answers the params, inputs, bom, record, manifest, sources and backfill
    of one output from one read of its row, not one query each; its files are one more."""
    store, meta, job = saved
    # The blob versions and sizes are the index's reads, on this test's one pool too;
    # only the rows' are counted here.
    monkeypatch.setattr(store, "_indexed", lambda output_id, role: None)
    expected = detail(store, meta, [])
    taken = _counting_connections(monkeypatch, store)
    got = detail(store, meta, [])
    # The row, and its `output_files` rows (#7256): a save names its files as blobs.
    assert len(taken) == 2
    assert got == expected
    assert got.params == job.params
    assert got.record is not None and got.manifest and got.bom
    assert got.inputs["v"] == 1
