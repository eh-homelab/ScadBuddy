"""`OutputStore` on `OutputRows` (#7255, outputs-in-Postgres spec 2026-11-23 §4.5 phase 1,
§6.1, §6.6): a save writes its row in one transaction, and a lookup by id reads the row.
A missing row is a missing output, found with no glob (#7257)."""

from __future__ import annotations

import errno
import json
import logging
import shutil
import threading
import time
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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
    OUTPUT_HOLDER,
    OutputBlobs,
    OutputFileBlob,
    OutputRow,
    OutputRows,
    output_blob_key,
    output_file_key,
)
from scadbuddy.library.outputs import (
    META_NAME,
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
    return store.paths.output_dir(meta.slug, meta.id)


def _old_folder(store: OutputStore, meta: OutputMeta) -> Path:
    """The folder a phase 2 pod wrote for the output: its ``meta.json`` and
    ``params.json``, for the tests of `OutputStore.restore_rows`, which #7257's task B5
    removes."""
    row = rows_of(store).get(meta.id)
    assert row is not None
    directory = _dir(store, meta)
    directory.mkdir(parents=True)
    (directory / META_NAME).write_text(row.meta.model_dump_json(), encoding="utf-8")
    (directory / "params.json").write_text(json.dumps(row.params), encoding="utf-8")
    return directory


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
    assert not store.paths.output_dir(job.slug, taken).exists()
    # The row that holds the id stays.
    assert rows_of(store).get(taken) is not None


class _HeldOpen(list[OutputRow]):
    """A copy batch's ``new`` rows that hold `write_copy`'s transaction open after its
    insert ran: the iteration waits on ``release`` before the commit."""

    def __init__(self, row: OutputRow) -> None:
        super().__init__([row])
        self.inserted = threading.Event()
        self.release = threading.Event()

    def __iter__(self) -> Iterator[OutputRow]:
        yield from list.__iter__(self)
        self.inserted.set()
        assert self.release.wait(30)


def _waits_on_a_lock(conninfo: str) -> bool:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        found = conn.execute(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE wait_event_type = 'Lock' AND datname = current_database()"
        ).fetchone()
    return found is not None and found[0] > 0


#: A save's blob keys (#7256): the keys alone; no blob is put.
_SAVE_BLOBS = OutputBlobs(
    model=output_blob_key("d" * 32, "model"),
    preview=output_blob_key("d" * 32, "preview"),
    files=(OutputFileBlob("a.stl", output_file_key("d" * 32, "a.stl"), 5),),
)


def _holds(conninfo: str, output_id: str) -> set[str]:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        found = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (OUTPUT_HOLDER, output_id),
        ).fetchall()
    return {str(key) for (key,) in found}


@pytest.mark.parametrize("blobs", [None, _SAVE_BLOBS], ids=["files", "blobs"])
async def test_a_save_wins_over_a_copy_batch_that_commits_during_its_insert(
    saved: tuple[OutputStore, OutputMeta, Job], pg_conninfo: str, blobs: OutputBlobs | None
) -> None:
    """The copy read the save's folder and inserted its row, and its batch has not
    committed when the save writes its row. The save waits for the batch, then takes
    the copy's row over: it keeps its files and its blob keys, and does not fail
    (#7360, #7256)."""
    store, meta, job = saved
    rows = rows_of(store)
    output_id = "d" * 32
    template = rows.get(meta.id)
    assert template is not None
    copied = replace(
        template,
        output_id=output_id,
        meta=template.meta.model_copy(update={"id": output_id, "name": "copied"}),
        files_stamp="stamp",
    )
    batch = _HeldOpen(copied)
    with ThreadPoolExecutor(2) as pool:
        copy = pool.submit(rows.write_copy, batch, [], rows.clock())
        assert batch.inserted.wait(30)
        staging = store.paths.root.parent / "staging"
        staging.mkdir()
        save = pool.submit(
            lambda: store.commit(
                store.prepare(job, staging, name="saved", output_id=output_id), blobs
            )
        )
        deadline = time.monotonic() + 30
        while not _waits_on_a_lock(pg_conninfo):
            assert not save.done(), save.exception()
            assert time.monotonic() < deadline
            time.sleep(0.05)
        batch.release.set()
        assert copy.result(30) == ({output_id}, set())
        saved_meta = save.result(30)
    assert saved_meta.name == "saved"
    row = rows.get(output_id)
    assert row is not None
    assert row.meta.name == "saved" and row.files_stamp is None
    if blobs is None:
        assert row.bytes_at is None and row.blobs == OutputBlobs()
        assert _holds(pg_conninfo, output_id) == set()
    else:
        assert row.bytes_at is not None
        assert (row.blobs.model, row.blobs.preview) == (blobs.model, blobs.preview)
        assert rows.files(output_id) == list(blobs.files)
        assert _holds(pg_conninfo, output_id) == set(blobs.keys())


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
    shutil.rmtree(store.paths.outputs)
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
    assert rows_of(store).get(meta.id) is not None
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
    second = await store.save(job, name="second")
    for old in (meta, second):
        _old_folder(store, old)
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


async def test_restore_rows_skips_a_folder_it_cannot_stat(
    saved: tuple[OutputStore, OutputMeta, Job],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale NFS handle (ESTALE) on one output's folder skips that folder, logged, as
    the retired copy's walk did; the other outputs get their rows back."""
    store, meta, job = saved
    second = await store.save(job, name="second")
    for old in (meta, second):
        _old_folder(store, old)
    rows_of(store).delete_slug(job.slug)
    stale = _dir(store, meta)
    is_dir = Path.is_dir

    def stale_handle(self: Path, *args: Any, **kwargs: Any) -> bool:
        if self == stale:
            raise OSError(errno.ESTALE, "Stale file handle")
        return is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", stale_handle)
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.outputs"):
        store.restore_rows(job.slug)
    assert rows_of(store).get(meta.id) is None
    assert rows_of(store).get(second.id) is not None
    assert "could not list an outputs folder" in caplog.text


async def test_orphan_sweep_keeps_outputs_whose_rows_stay(
    saved: tuple[OutputStore, OutputMeta, Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, meta, job = saved
    catalogue = _catalogue(store)
    shutil.rmtree(store.paths.model_dir(job.slug))
    # The sweep still finds a slug by its outputs folder, which a save no longer writes
    # (#7257 task B4); task B5 finds it by its rows.
    (store.paths.outputs / job.slug).mkdir(parents=True)
    # A live template besides: a models directory with none sweeps nothing (#7649).
    store.paths.model_dir("kept").mkdir()
    store.paths.model_source("kept").write_text("cube(1);\n", encoding="utf-8")
    monkeypatch.setattr(rows_of(store), "delete_slug", _refuse)
    catalogue.sweep_orphans()
    assert rows_of(store).get(meta.id) is not None
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
        if pattern.endswith(META_NAME):
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
