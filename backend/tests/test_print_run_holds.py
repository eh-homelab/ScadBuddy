"""A LAN run's hold on its sliced files (#6122): `hold_run_file` takes it while the run
is in use, each end of the run drops it, `reap_run_holds` drops what an end left, and
`PrintRunStore.transferable` (the print worker's read URL) names only a file of a run in
use."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.runs import (
    RUN_HOLDER,
    PrintRun,
    PrintRunError,
    PrintRunStore,
    hold_run_file,
    reap_run_holds,
)
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.orders.lifecycle import QUOTE_HOLDER
from scadbuddy.printers.lan_models import LanStopped
from scadbuddy.render.projection import JobProjection
from scadbuddy.slicing.store import HOLDER, NativeSliceStore
from scadbuddy.store.refs import BlobRefs

pytestmark = pytest.mark.requires_postgres

OUTPUT = "a" * 32
OTHER = "b" * 32
FILE = "slice-" + "1" * 64
DONE = PrintRunResult(library_file_id=1, copies=1, path="native")
WAITING = DONE.model_copy(update={"lan_waiting": [2]})
REFUSED = PrintRunError(status=422, title="Unprocessable Content", detail="no spool")
STOPPED = LanStopped(stage="cancelled", message="Stopped.")


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def store(jobs: JobProjection) -> PrintRunStore:
    return PrintRunStore(jobs.pool)


async def _run(store: PrintRunStore, run_id: str = "r1", subject: str = OUTPUT) -> PrintRun:
    return await store.insert_accepted(
        run_id,
        subject=PrintSubject.output(subject),
        key=run_id,
        slug="demo",
        workflow_id=f"print-{run_id}",
        workflow_run_id="w1",
        retention=None,
    )


def _holds(jobs: JobProjection) -> set[tuple[str, str]]:
    with jobs.pool.connection() as conn:
        rows = conn.execute(
            "SELECT key, holder_id FROM blob_refs WHERE holder_kind = %s", (RUN_HOLDER,)
        ).fetchall()
    return {(row["key"], row["holder_id"]) for row in rows}


async def _sliced(jobs: JobProjection, subject: str, key: str) -> int:
    """A completed `native_slices` row of ``subject`` naming ``key``, holding it as the
    slice's own record does."""
    slices = NativeSliceStore(jobs.pool)
    row = await slices.start(
        workflow_id=f"slice-{subject}-{key}",
        subject=subject,
        library_file_id=1,
        plate_id=1,
        preset_key="k",
    )
    await slices.finish(row.id, status="completed", sliced_blob_key=key, failure=None, warnings=[])
    BlobRefs(jobs.pool).add(key, HOLDER, str(row.id))
    return row.id


async def test_a_run_in_use_holds_its_file_and_an_ended_run_does_not(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    run = await _run(store)
    assert hold_run_file(jobs.pool, run.id, FILE)
    # Taken again: one row.
    assert hold_run_file(jobs.pool, run.id, FILE)
    assert _holds(jobs) == {(FILE, run.id)}
    ended = await _run(store, "r2")
    await store.fail(ended.id, "demo", REFUSED)
    assert not hold_run_file(jobs.pool, ended.id, FILE)
    assert not hold_run_file(jobs.pool, "no-such-run", FILE)
    assert _holds(jobs) == {(FILE, run.id)}


async def test_a_swept_rows_file_of_a_running_lan_run_is_transferable(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    """Acceptance: the slice's row is gone (the sweep deletes it 6 h after it finished),
    but the LAN run still has prints to start, and its hold names the file."""
    run = await _run(store)
    hold_run_file(jobs.pool, run.id, FILE)
    await store.succeed(run.id, "demo", WAITING)
    assert await store.transferable(FILE)


async def test_a_preview_slice_a_quote_slice_and_a_slicers_input_are_not_transferable(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    """Acceptance: the print dialog's slice of a subject no run prints, an order quote's
    held file, and a slicer's input (held by its slice, named by no row) are no run's."""
    preview = "slice-" + "2" * 64
    await _sliced(jobs, OTHER, preview)
    quote = "slice-" + "3" * 64
    BlobRefs(jobs.pool).add(quote, QUOTE_HOLDER, "quote-1")
    slicer_input = "slice-" + "4" * 64
    slice_id = await _sliced(jobs, OUTPUT, FILE)
    BlobRefs(jobs.pool).add(slicer_input, HOLDER, str(slice_id))
    # A LAN run of OUTPUT has prints to start: its own slice is its file, the input is not.
    run = await _run(store)
    await store.succeed(run.id, "demo", WAITING)
    assert not await store.transferable(preview)
    assert not await store.transferable(quote)
    assert not await store.transferable(slicer_input)
    assert await store.transferable(FILE)


async def test_a_completed_slice_is_transferable_only_while_a_lan_run_of_its_subject_waits(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    """A LAN run from before the hold (#6122) reads its later copies' slices by their
    rows: only while its result marks it a LAN run (``path`` ``native``) with prints
    still to start (#6586)."""
    await _sliced(jobs, OUTPUT, FILE)
    assert not await store.transferable(FILE)
    run = await _run(store)
    await store.succeed(run.id, "demo", WAITING)
    assert await store.transferable(FILE)
    await store.update_result(run.id, "demo", DONE)
    assert not await store.transferable(FILE)


async def test_a_bambuddy_queue_run_in_use_grants_no_slice_of_its_subject(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    """#6586: a running run with no hold names no file. A Bambuddy-queue run never
    reads a transfer URL, and a LAN run holds its file before its first start."""
    await _sliced(jobs, OUTPUT, FILE)
    await _run(store)
    assert not await store.transferable(FILE)


async def test_a_bambuddy_path_result_with_prints_to_start_grants_no_slice(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    """#6588: a succeeded run whose result says ``path`` ``bambuddy`` grants no slice of
    its subject, even with a stray ``lan_waiting``. Today no Bambuddy-queue run writes
    one, so `_in_use` alone keeps it out; the path check keeps it out if one ever does."""
    await _sliced(jobs, OUTPUT, FILE)
    run = await _run(store)
    stray = WAITING.model_copy(update={"path": "bambuddy"})
    await store.succeed(run.id, "demo", stray)
    assert not await store.transferable(FILE)


async def test_a_hold_an_ended_run_left_grants_nothing(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    run = await _run(store)
    await store.fail(run.id, "demo", REFUSED)
    BlobRefs(jobs.pool).add(FILE, RUN_HOLDER, run.id)
    assert not await store.transferable(FILE)


async def test_a_success_with_prints_to_start_keeps_the_hold_and_the_last_start_drops_it(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    run = await _run(store)
    hold_run_file(jobs.pool, run.id, FILE)
    await store.succeed(run.id, "demo", WAITING)
    assert _holds(jobs) == {(FILE, run.id)}
    # The end of `PrintLanSequence`: its last start leaves no print to start.
    await store.update_result(run.id, "demo", DONE)
    assert _holds(jobs) == set()


async def test_a_success_with_no_print_to_start_drops_the_hold(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    run = await _run(store)
    hold_run_file(jobs.pool, run.id, FILE)
    await store.succeed(run.id, "demo", DONE)
    assert _holds(jobs) == set()


async def test_a_failure_and_a_lost_run_drop_the_hold(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    failed = await _run(store, "r1")
    lost = await _run(store, "r2")
    hold_run_file(jobs.pool, failed.id, FILE)
    hold_run_file(jobs.pool, lost.id, FILE)
    await store.fail(failed.id, "demo", REFUSED)
    assert _holds(jobs) == {(FILE, lost.id)}
    await store.fail_lost(lost.id)
    assert _holds(jobs) == set()


async def test_a_stop_drops_the_hold(jobs: JobProjection, store: PrintRunStore) -> None:
    """A stop the sequence records (``lan_stopped`` in its update), and one the API
    records for a sequence that closed without it (`record_lan_stopped`)."""
    stopped = await _run(store, "r1")
    closed = await _run(store, "r2")
    for run in (stopped, closed):
        hold_run_file(jobs.pool, run.id, FILE)
        await store.succeed(run.id, "demo", WAITING)
    await store.update_result(
        stopped.id, "demo", WAITING.model_copy(update={"lan_stopped": STOPPED})
    )
    assert _holds(jobs) == {(FILE, closed.id)}
    await store.record_lan_stopped(closed.id, STOPPED)
    assert _holds(jobs) == set()


async def test_the_reaper_drops_the_holds_of_ended_and_gone_runs_only(
    jobs: JobProjection, store: PrintRunStore
) -> None:
    kept = await _run(store, "r1")
    ended = await _run(store, "r2")
    hold_run_file(jobs.pool, kept.id, FILE)
    await store.fail(ended.id, "demo", REFUSED)
    refs = BlobRefs(jobs.pool)
    # An end that left its hold (a slice attempt that outlived it), and a pruned run's.
    refs.add(FILE, RUN_HOLDER, ended.id)
    refs.add(FILE, RUN_HOLDER, "pruned")
    refs.add(FILE, QUOTE_HOLDER, "pruned")
    assert reap_run_holds(refs) == 2
    assert _holds(jobs) == {(FILE, kept.id)}
    with jobs.pool.connection() as conn:
        quote = conn.execute(
            "SELECT 1 FROM blob_refs WHERE holder_kind = %s", (QUOTE_HOLDER,)
        ).fetchone()
    assert quote is not None
