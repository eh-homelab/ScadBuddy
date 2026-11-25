"""The cut-over gate (#7257, plan D2 and D4): a byte run that ended writes the
`output_cutover` mark when every output's bytes are in the blob store, and after the mark
the copies of the files to rows and to blobs do nothing, at an API start and in the
housekeeping sweeps."""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy import main
from scadbuddy.library.output_byte_copy import copy_output_bytes
from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_copy import copy_outputs
from scadbuddy.library.output_rows import OutputRows, output_blob_key
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import Pool
from scadbuddy.workflows.housekeeping import OUTPUT_BYTES_SWEEP, OUTPUT_COPY_SWEEP, SWEEPS
from tests.support.arrange import finished_job
from tests.support.saved_outputs import save_files
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
async def saved(tmp_path: Path, projection: JobProjection) -> tuple[OutputStore, list[OutputMeta]]:
    """Two outputs saved as files only, as a build before the rows saved them."""
    paths, job, _ = await finished_job(tmp_path / "data")
    store = OutputStore(paths)
    metas = [save_files(store, job, name="first"), save_files(store, job, name="second")]
    store.rows = OutputRows(projection.pool)
    return store, metas


def _rows(store: OutputStore) -> OutputRows:
    assert store.rows is not None
    return store.rows


def _state(store: OutputStore, content: ContentStore, projection: JobProjection) -> Any:
    return SimpleNamespace(
        outputs=store,
        projection=SimpleNamespace(conninfo=projection.conninfo),
        store=SimpleNamespace(content=content),
    )


def _runs(pool: Pool) -> tuple[int, int]:
    """How many row copy runs and byte copy runs exist."""
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT (SELECT count(*) FROM output_copy_runs) AS rows,"
            " (SELECT count(*) FROM output_byte_runs) AS bytes"
        ).fetchone()
    assert found is not None
    return int(found["rows"]), int(found["bytes"])


async def _cut_over(store: OutputStore, content: ContentStore, projection: JobProjection) -> None:
    """Copy the rows, then the bytes through the start's byte copy, which writes the mark."""
    assert copy_outputs(store, _rows(store), projection.conninfo) is not None
    await main._copy_output_bytes_once(_state(store, content, projection), threading.Event())
    assert _rows(store).cut_over_at() is not None


async def test_byte_run_that_finished_writes_the_mark(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, _ = saved
    rows = _rows(store)
    assert copy_outputs(store, rows, projection.conninfo) is not None
    assert rows.cut_over_at() is None
    with caplog.at_level(logging.INFO, logger="scadbuddy.main"):
        await main._copy_output_bytes_once(_state(store, content, projection), threading.Event())
    at = rows.cut_over_at()
    assert at is not None
    marks = [r for r in caplog.records if r.getMessage() == "the outputs are cut over (#7257)"]
    assert len(marks) == 1
    assert marks[0].at == at  # type: ignore[attr-defined]


async def test_no_mark_while_a_row_has_no_bytes(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One output's model does not verify: the run records it as ``failed`` and its row
    keeps ``bytes_at`` NULL, so no mark is written."""
    store, metas = saved
    rows = _rows(store)
    bad = output_blob_key(metas[0].id, "model")
    verify = OutputBytes.verify

    async def failing(self: OutputBytes, key: str) -> bool:
        return False if key == bad else await verify(self, key)

    monkeypatch.setattr(OutputBytes, "verify", failing)
    assert copy_outputs(store, rows, projection.conninfo) is not None
    await main._copy_output_bytes_once(_state(store, content, projection), threading.Event())
    last = rows.last_byte_run()
    assert last is not None and [f["output_id"] for f in last["failed"]] == [metas[0].id]
    assert rows.cut_over_at() is None
    assert rows.cut_over() is False


async def test_copies_do_nothing_after_the_mark(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
    pool: Pool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After the mark the files are not the record: a row copy would drop the row of a
    folder that is gone. Neither copy runs, so no run row is added and the row stays."""
    store, metas = saved
    rows = _rows(store)
    await _cut_over(store, content, projection)
    before = _runs(pool)
    shutil.rmtree(store.directory(metas[0].id))
    with caplog.at_level(logging.INFO, logger="scadbuddy.library"):
        assert copy_outputs(store, rows, projection.conninfo) is None
        assert (
            await copy_output_bytes(store, rows, content, projection.conninfo, threading.Event())
            is None
        )
    assert _runs(pool) == before
    assert rows.get(metas[0].id) is not None
    assert [r.getMessage() for r in caplog.records].count(NOTHING_TO_COPY) == 2


async def test_sweeps_do_nothing_after_the_mark(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
    pool: Pool,
) -> None:
    """The housekeeping sweeps `OUTPUT_COPY_SWEEP` and `OUTPUT_BYTES_SWEEP` run the same
    copies, so they do nothing after the mark too."""
    store, metas = saved
    await _cut_over(store, content, projection)
    before = _runs(pool)
    shutil.rmtree(store.directory(metas[0].id))
    sweeps = dict(
        zip(
            SWEEPS,
            main._housekeeping_activities(_state(store, content, projection)),
            strict=True,
        )
    )
    await ActivityEnvironment().run(sweeps[OUTPUT_COPY_SWEEP])
    await ActivityEnvironment().run(sweeps[OUTPUT_BYTES_SWEEP])
    assert _runs(pool) == before
    assert _rows(store).get(metas[0].id) is not None


async def test_start_writes_the_mark_first_and_then_copies_nothing(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
    pool: Pool,
) -> None:
    """A start whose database met the conditions before (the last byte run ended on a pod
    of an older build) writes the mark first, so it copies nothing."""
    store, metas = saved
    rows = _rows(store)
    assert copy_outputs(store, rows, projection.conninfo) is not None
    assert await copy_output_bytes(store, rows, content, projection.conninfo) is not None
    assert rows.cut_over_at() is None  # the copy alone writes no mark
    before = _runs(pool)
    shutil.rmtree(store.directory(metas[0].id))
    fresh = OutputStore(store.paths)
    fresh.rows = OutputRows(projection.pool)
    await main._copy_outputs_and_bytes_logged(
        _state(fresh, content, projection), stop=threading.Event()
    )
    assert fresh.rows.cut_over_at() is not None
    assert _runs(pool) == before
    assert fresh.rows.get(metas[0].id) is not None


async def test_copies_check_the_mark_again_under_their_lock(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mark is written between a copy's first check and its lock (a byte run on
    another replica ended meanwhile): the copy checks again under its lock and does
    nothing."""
    store, metas = saved
    await _cut_over(store, content, projection)
    before = _runs(pool)
    shutil.rmtree(store.directory(metas[0].id))
    rows = OutputRows(projection.pool)
    real = rows.cut_over
    calls: list[bool] = []

    def late() -> bool:
        """The first check misses the mark; later checks read it."""
        calls.append(True)
        return False if len(calls) == 1 else real()

    monkeypatch.setattr(rows, "cut_over", late)
    late_store = OutputStore(store.paths)
    late_store.rows = rows
    assert copy_outputs(late_store, rows, projection.conninfo) is None
    calls.clear()
    assert await copy_output_bytes(late_store, rows, content, projection.conninfo) is None
    assert _runs(pool) == before
    assert rows.get(metas[0].id) is not None


async def test_mark_another_replica_wrote_is_not_logged(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two replicas both miss the mark and both try it: only the one whose call wrote it
    logs it. The other answers that the mark exists, and logs nothing."""
    store, _ = saved
    await _cut_over(store, content, projection)
    rows = OutputRows(projection.pool)
    real = rows.cut_over
    calls: list[bool] = []

    def late() -> bool:
        """The first check misses the mark that the other replica wrote meanwhile."""
        calls.append(True)
        return False if len(calls) == 1 else real()

    monkeypatch.setattr(rows, "cut_over", late)
    caplog.clear()  # the mark the other replica wrote above
    with caplog.at_level(logging.INFO, logger="scadbuddy.main"):
        assert await main._try_cut_over(rows) is True
    logged = [r for r in caplog.records if r.getMessage() == "the outputs are cut over (#7257)"]
    assert logged == []


THUMBNAIL_PNG = b"\x89PNG\r\n\x1a\nthumbnail after the mark"


async def _without_folder(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
) -> tuple[OutputStore, OutputMeta, Path]:
    """Review Focus 2: the mark is written, the store puts blobs, and the first output's
    folder is gone, as for an output that a release B pod saved."""
    store, metas = saved
    await _cut_over(store, content, projection)
    store.bytes = OutputBytes(content)
    directory = store.directory(metas[0].id)
    shutil.rmtree(directory)
    return store, metas[0], directory


async def test_rename_without_folder_after_cut_over(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
) -> None:
    store, meta, directory = await _without_folder(saved, content, projection)
    renamed = await asyncio.to_thread(store.rename, meta.id, "renamed")
    assert renamed.name == "renamed"
    row = _rows(store).get(meta.id)
    assert row is not None and row.meta.name == "renamed"
    assert not directory.exists()


async def test_put_thumbnail_without_folder_after_cut_over(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
) -> None:
    store, meta, directory = await _without_folder(saved, content, projection)
    await store.put_thumbnail(meta.id, THUMBNAIL_PNG)
    row = _rows(store).get(meta.id)
    assert row is not None and row.blobs.thumbnail == output_blob_key(meta.id, "thumbnail")
    async with store.local_role(meta.id, "thumbnail") as path:
        assert path is not None and path.read_bytes() == THUMBNAIL_PNG
    assert not directory.exists()


async def test_put_drawn_thumbnail_without_folder_after_cut_over(
    saved: tuple[OutputStore, list[OutputMeta]],
    content: ContentStore,
    projection: JobProjection,
) -> None:
    """The row decides: the drawn thumbnail's key is named while the row names none."""
    store, meta, directory = await _without_folder(saved, content, projection)
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
    content: ContentStore,
    projection: JobProjection,
) -> None:
    store, meta, directory = await _without_folder(saved, content, projection)
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
    content: ContentStore,
    projection: JobProjection,
) -> None:
    store, meta, directory = await _without_folder(saved, content, projection)
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


async def test_missing_folder_raises_before_cut_over(
    saved: tuple[OutputStore, list[OutputMeta]],
    projection: JobProjection,
) -> None:
    """With no mark, a missing folder fails the write, as in phase 2."""
    store, metas = saved
    assert copy_outputs(store, _rows(store), projection.conninfo) is not None
    assert _rows(store).cut_over_at() is None
    shutil.rmtree(store.directory(metas[0].id))
    with pytest.raises(FileNotFoundError):
        store.rename(metas[0].id, "renamed")
    with pytest.raises(FileNotFoundError):
        store.start_backfill(metas[0].id, "j9")
