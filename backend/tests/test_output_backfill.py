"""#902: an output saved before manifests gets one by re-rendering it."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from temporalio.testing import ActivityEnvironment

from scadbuddy import main
from scadbuddy.core.events import Event, InProcessEventBus, JobEvent, OutputEvent
from scadbuddy.library import backfill as backfill_module
from scadbuddy.library.backfill import (
    AmbiguousOutputError,
    _announce,
    attach_backfills,
    attach_job_backfills,
    choose_output,
    follow_backfills,
)
from scadbuddy.library.outputs import (
    OUTPUT_HOLDER,
    BackfillState,
    OutputStore,
    release_parts,
)
from scadbuddy.render.job_models import Job, JobNotFoundError
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.housekeeping import BACKFILL_SWEEP, SWEEPS
from tests.conftest import PgPool
from tests.support.arrange import finished_job
from tests.support.row_outputs import output_store, update_row
from tests.support.store import store_pool
from tests.support.waits import until


def _jobs(*jobs: Job):  # type: ignore[no-untyped-def]
    by_id = {job.id: job for job in jobs}

    def read(job_id: str) -> Job:
        if job_id not in by_id:
            raise JobNotFoundError(job_id)
        return by_id[job_id]

    return read


def _holders(refs: BlobRefs, key: str) -> list[tuple[str, str]]:
    with refs._pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_kind, holder_id FROM blob_refs WHERE key = %s ORDER BY 1, 2", (key,)
        ).fetchall()
    return [(row["holder_kind"], row["holder_id"]) for row in rows]


async def _legacy_output(tmp_path: Path, pool: PgPool):  # type: ignore[no-untyped-def]
    """An output saved before manifests, and the done re-render of it."""
    paths, job, written = await finished_job(tmp_path, job_id="rerender")
    store = output_store(tmp_path, pool, paths=paths)
    old = await store.save(job.model_copy(update={"outputs": [], "id": "old"}), name="mine")
    assert store.manifest(old.id) == []
    return store, old, job, written


@pytest.mark.requires_postgres
async def test_a_finished_rerender_gives_the_output_its_manifest_and_holds(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        assert attach_backfills(store, refs, _jobs(job)) == 1
        key = written.manifest[0].part
        assert _holders(refs, key) == [(OUTPUT_HOLDER, old.id)]
    assert store.manifest(old.id) == written.manifest
    assert store.backfill(old.id) is None
    kept = store.get(old.id)
    assert (kept.id, kept.name, kept.job_id) == (old.id, "mine", old.job_id)
    assert store.ids_for("demo") == [old.id]  # nothing duplicated


@pytest.mark.requires_postgres
async def test_attaching_again_after_a_crash_changes_nothing(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """A crash after the hold, before the manifest: the next pass finishes the job, and
    one after that finds nothing to do."""
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    key = written.manifest[0].part
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        refs.add(key, OUTPUT_HOLDER, old.id)  # the hold the crashed pass took
        assert attach_backfills(store, refs, _jobs(job)) == 1
        assert attach_backfills(store, refs, _jobs(job)) == 0
        assert _holders(refs, key) == [(OUTPUT_HOLDER, old.id)]
    assert store.manifest(old.id) == written.manifest


@pytest.mark.requires_postgres
async def test_a_stopped_pass_attaches_nothing_more(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """#2128: the boot's pass runs in a thread the shutdown cannot cancel; once its stop
    is set it attaches no further output, and what it left waits for the next pass."""
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    stop = threading.Event()
    stop.set()
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        assert attach_backfills(store, refs, _jobs(job), stop=stop) == 0
        assert store.backfill(old.id) == BackfillState(job_id=job.id)
        assert attach_backfills(store, refs, _jobs(job)) == 1
    assert store.manifest(old.id) == written.manifest


@pytest.mark.requires_postgres
async def test_an_unfinished_rerender_is_left_for_the_next_pass(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    store, old, job, _ = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    running = job.model_copy(update={"state": "running", "result": None, "outputs": []})
    with store_pool(pg_conninfo) as pool:
        assert attach_backfills(store, BlobRefs(pool), _jobs(running)) == 0
    state = store.backfill(old.id)
    assert state is not None and state.job_id == job.id and state.error is None
    assert store.manifest(old.id) == []


@pytest.mark.requires_postgres
@pytest.mark.parametrize(
    ("failed", "message"),
    [
        ({"state": "failed", "error": "openscad exited with 1"}, "openscad exited with 1"),
        ({"state": "cancelled"}, "the re-render was cancelled"),
        (None, "the re-render is gone"),
    ],
)
async def test_a_rerender_that_did_not_finish_says_why_and_is_not_retried(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str, failed: dict[str, str] | None, message: str
) -> None:
    store, old, job, _ = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    jobs = (
        _jobs(job.model_copy(update={**failed, "result": None, "outputs": []}))
        if failed
        else _jobs()
    )
    with store_pool(pg_conninfo) as pool:
        assert attach_backfills(store, BlobRefs(pool), jobs) == 0
        assert attach_backfills(store, BlobRefs(pool), jobs) == 0
    state = store.backfill(old.id)
    assert state is not None and state.error is not None and message in state.error
    assert store.manifest(old.id) == []


async def test_the_output_is_matched_by_its_recorded_parts(tmp_path: Path) -> None:
    """A pipeline job writes several outputs; the one with the same Parts is this one."""
    _, job, written = await finished_job(tmp_path, job_id="j1")
    _, _, other = await finished_job(tmp_path / "b", width=20, job_id="j2")
    both = job.model_copy(update={"outputs": [other, written]})
    assert choose_output(both, written.record) == written
    assert choose_output(both, other.record) == other
    assert choose_output(both, None) is None  # no record: cannot tell which
    assert choose_output(job, None) == written  # one output: that one


async def test_outputs_with_the_same_parts_are_told_apart_by_their_saved_index(
    tmp_path: Path,
) -> None:
    """#1007 item 2: a pipeline writing several outputs from one set of Parts. The Parts
    match both, so the index the output recorded when saved picks; without one, neither
    is given, rather than whichever comes first."""
    _, job, written = await finished_job(tmp_path, job_id="j1")
    twin = written.model_copy(update={"name": "second layout"})
    both = job.model_copy(update={"outputs": [written, twin]})
    second = written.record.model_copy(update={"output_index": 1})
    assert choose_output(both, second) is twin
    assert choose_output(both, written.record.model_copy(update={"output_index": 0})) is written
    with pytest.raises(AmbiguousOutputError) as unrecorded:
        choose_output(both, written.record)  # saved before the index was recorded
    assert unrecorded.value.recorded_index is None
    with pytest.raises(AmbiguousOutputError) as elsewhere:
        # Recorded, but the re-render's outputs with these Parts are not at that index.
        choose_output(both, written.record.model_copy(update={"output_index": 2}))
    assert elsewhere.value.recorded_index == 2


@pytest.mark.requires_postgres
async def test_a_saved_output_records_which_of_its_jobs_outputs_it_is(
    tmp_path: Path, pg_pool: PgPool
) -> None:
    paths, job, written = await finished_job(tmp_path, job_id="j1")
    twin = written.model_copy(update={"name": "second layout"})
    store = output_store(tmp_path, pg_pool, paths=paths)
    saved = await store.save(job.model_copy(update={"outputs": [written, twin]}), index=1)
    record = store.record(saved.id)
    assert record is not None and record.output_index == 1


@pytest.mark.requires_postgres
async def test_an_ambiguous_rerender_fails_the_backfill_and_says_why(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    update_row(store, old.id, record=written.record.model_dump(mode="json"))
    twin = written.model_copy(update={"name": "second layout"})
    rerender = job.model_copy(update={"outputs": [written, twin]})
    with store_pool(pg_conninfo) as pool:
        assert attach_backfills(store, BlobRefs(pool), _jobs(rerender)) == 0
    state = store.backfill(old.id)
    assert state is not None and state.error is not None and "several outputs" in state.error
    assert "saved before outputs recorded" in state.error
    assert store.manifest(old.id) == []


@pytest.mark.requires_postgres
async def test_an_ambiguous_rerender_of_an_output_with_an_index_says_it_is_not_there(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """#3142: an output that did record its index, which none of the matching outputs
    has, was told it was saved before outputs recorded one."""
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    update_row(
        store,
        old.id,
        record=written.record.model_copy(update={"output_index": 2}).model_dump(mode="json"),
    )
    twin = written.model_copy(update={"name": "second layout"})
    rerender = job.model_copy(update={"outputs": [written, twin]})
    with store_pool(pg_conninfo) as pool:
        assert attach_backfills(store, BlobRefs(pool), _jobs(rerender)) == 0
    state = store.backfill(old.id)
    assert state is not None and state.error is not None and "several outputs" in state.error
    assert "saved before outputs recorded" not in state.error
    assert "not among them" in state.error
    assert store.manifest(old.id) == []


async def _two_legacy(store: OutputStore, job: Job) -> tuple[str, str]:
    """A second output saved before manifests, of the same render, both waiting on it."""
    first = store.ids_for("demo")[0]
    second = (await store.save(job.model_copy(update={"outputs": [], "id": "old2"}), name="two")).id
    store.start_backfill(first, job.id)
    store.start_backfill(second, job.id)
    return first, second


def _in_order(store: OutputStore, monkeypatch: pytest.MonkeyPatch, first: str) -> None:
    """Make ``first`` the output the pass reaches first."""
    pending = store.pending_backfills

    def ordered() -> list[tuple[str, BackfillState]]:
        return sorted(pending(), key=lambda entry: entry[0] != first)

    monkeypatch.setattr(store, "pending_backfills", ordered)


@pytest.mark.requires_postgres
async def test_an_output_deleted_before_its_hold_keeps_no_holds_and_the_pass_goes_on(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete lands while the Parts are being held, after the output was read: its
    release has already run, so the pass must drop the holds it took after it."""
    store, _, job, written = await _legacy_output(tmp_path, pg_pool)
    gone, kept = await _two_legacy(store, job)
    _in_order(store, monkeypatch, gone)
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        add = refs.add

        def delete_then_add(key: str, kind: str, holder: str) -> None:
            if holder == gone and gone in store.ids_for("demo"):
                store.delete(gone)
                release_parts(refs, gone)  # what DELETE /outputs/{id} does after
            add(key, kind, holder)

        monkeypatch.setattr(refs, "add", delete_then_add)
        assert attach_backfills(store, refs, _jobs(job)) == 1
        assert _holders(refs, written.manifest[0].part) == [(OUTPUT_HOLDER, kept)]
    assert store.manifest(kept) == written.manifest


@pytest.mark.requires_postgres
async def test_an_output_deleted_inside_the_attach_keeps_no_holds_and_the_pass_goes_on(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete lands while the attach runs: the attach's write finds no row."""
    store, _, job, written = await _legacy_output(tmp_path, pg_pool)
    gone, kept = await _two_legacy(store, job)
    _in_order(store, monkeypatch, gone)
    attach = store.rows.attach_backfill

    def delete_then_attach(output_id: str, *args: Any, **kwargs: Any) -> Any:
        if output_id == gone and store.rows.get(gone) is not None:
            store.delete(gone)
        return attach(output_id, *args, **kwargs)

    monkeypatch.setattr(store.rows, "attach_backfill", delete_then_attach)
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        assert attach_backfills(store, refs, _jobs(job)) == 1
        assert _holders(refs, written.manifest[0].part) == [(OUTPUT_HOLDER, kept)]
    assert store.manifest(kept) == written.manifest


@pytest.mark.requires_postgres
async def test_a_corrupt_output_does_not_stop_the_ones_behind_it(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, job, written = await _legacy_output(tmp_path, pg_pool)
    corrupt, good = await _two_legacy(store, job)
    _in_order(store, monkeypatch, corrupt)
    update_row(store, corrupt, record=["not", "a", "record"])
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        assert attach_backfills(store, refs, _jobs(job)) == 1
        assert _holders(refs, written.manifest[0].part) == [(OUTPUT_HOLDER, good)]
    assert store.manifest(good) == written.manifest
    # Permanent: said why, and not tried again.
    state = store.backfill(corrupt)
    assert state is not None and state.error is not None and "record" in state.error
    assert store.manifest(corrupt) == []


@pytest.mark.requires_postgres
async def test_a_transient_failure_is_left_for_the_next_pass(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, job, written = await _legacy_output(tmp_path, pg_pool)
    flaky, good = await _two_legacy(store, job)
    _in_order(store, monkeypatch, flaky)
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        add = refs.add

        def add_or_fail(key: str, kind: str, holder: str) -> None:
            if holder == flaky:
                raise psycopg.OperationalError("the connection dropped")
            add(key, kind, holder)

        monkeypatch.setattr(refs, "add", add_or_fail)
        assert attach_backfills(store, refs, _jobs(job)) == 1
        assert store.manifest(good) == written.manifest
        state = store.backfill(flaky)
        assert state is not None and state.error is None  # still pending
        monkeypatch.setattr(refs, "add", add)
        assert attach_backfills(store, refs, _jobs(job)) == 1
    assert store.manifest(flaky) == written.manifest


@pytest.mark.requires_postgres
async def test_a_job_that_does_not_validate_is_retried_not_failed(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """#1007: a ValidationError reading the job is not the output's fault; the next pass
    tries again instead of marking the backfill failed for good."""
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)

    def unreadable(job_id: str) -> Job:
        Job.model_validate({"id": job_id})  # raises a ValidationError, a ValueError
        raise AssertionError("unreachable")

    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        assert attach_backfills(store, refs, unreadable) == 0
        assert store.backfill(old.id) == BackfillState(job_id=job.id)  # still pending
        assert attach_backfills(store, refs, _jobs(job)) == 1
    assert store.manifest(old.id) == written.manifest


@pytest.mark.requires_postgres
async def test_a_job_that_never_validates_logs_its_trace_once(
    tmp_path: Path,
    pg_pool: PgPool,
    pg_conninfo: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1007 re-review: retried every pass, a row that never validates logged a full
    trace each time. Once per job, then a warning."""
    monkeypatch.setattr(backfill_module, "_unreadable_jobs", set())
    store, old, job, _ = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)

    def unreadable(job_id: str) -> Job:
        Job.model_validate({"id": job_id})
        raise AssertionError("unreachable")

    with (
        caplog.at_level(logging.WARNING, logger="scadbuddy.library.backfill"),
        store_pool(pg_conninfo) as pool,
    ):
        for _ in range(3):
            assert attach_backfills(store, BlobRefs(pool), unreadable) == 0
    mine = [r for r in caplog.records if r.name == "scadbuddy.library.backfill"]
    assert [r.exc_info is not None for r in mine] == [True, False, False]
    assert [r.levelno for r in mine] == [logging.ERROR, logging.WARNING, logging.WARNING]


@pytest.mark.requires_postgres
async def test_a_pass_forgets_an_unreadable_job_no_output_waits_on(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3143: a job whose row never validated, and whose outputs then stopped waiting on
    it, was kept for the life of the process. A full pass keeps only the jobs pending."""
    monkeypatch.setattr(backfill_module, "_unreadable_jobs", set())
    store, old, job, _ = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)

    def unreadable(job_id: str) -> Job:
        Job.model_validate({"id": job_id})
        raise AssertionError("unreachable")

    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        assert attach_backfills(store, refs, unreadable) == 0
        assert backfill_module._unreadable_jobs == {job.id}
        assert attach_backfills(store, refs, unreadable) == 0
        assert backfill_module._unreadable_jobs == {job.id}  # still waited on
        store.clear_backfill(old.id)
        assert attach_backfills(store, refs, unreadable) == 0
    assert backfill_module._unreadable_jobs == set()


@pytest.mark.requires_postgres
async def test_attaching_one_job_forgets_no_other_unreadable_job(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3792: only the full pass prunes the unreadable jobs. A per-job attach, for a job
    no output waits on, leaves a job another output still waits on in the set."""
    store, old, job, _ = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    monkeypatch.setattr(backfill_module, "_unreadable_jobs", {job.id, "gone-job"})
    with store_pool(pg_conninfo) as pool:
        assert attach_job_backfills(store, BlobRefs(pool), _jobs(), "nobody-waits") == 0
    assert backfill_module._unreadable_jobs == {job.id, "gone-job"}


@pytest.mark.requires_postgres
async def test_a_marker_left_after_its_manifest_was_written_is_cleared_not_failed(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """A crash after the manifest, before the marker's removal, then the job pruned."""
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        assert attach_backfills(store, refs, _jobs(job)) == 1
        store.start_backfill(old.id, job.id)  # the marker the crash left
        assert attach_backfills(store, refs, _jobs()) == 0
    assert store.backfill(old.id) is None
    assert store.manifest(old.id) == written.manifest


async def _until(check, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    await until(check, timeout, interval=0.02, what="timed out")


def _output_events() -> tuple[InProcessEventBus, list[OutputEvent]]:
    bus = InProcessEventBus()
    heard: list[OutputEvent] = []

    def on_event(event: Event) -> None:
        if isinstance(event, OutputEvent):
            heard.append(event)

    bus.add_listener(on_event)
    return bus, heard


@pytest.mark.requires_postgres
@pytest.mark.parametrize(
    "state",
    [pytest.param("done", id="attached"), pytest.param("failed", id="marked-failed")],
)
async def test_a_settled_backfill_is_announced_on_the_output(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str, state: str
) -> None:
    """#1970: Arrange reads the output on this rather than polling it."""
    store, old, job, _ = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    if state == "failed":
        job = job.model_copy(
            update={"state": "failed", "error": "x", "result": None, "outputs": []}
        )
    bus, heard = _output_events()
    with store_pool(pg_conninfo) as pool:
        attach_backfills(store, BlobRefs(pool), _jobs(job), bus)
        attach_backfills(store, BlobRefs(pool), _jobs(job), bus)  # nothing left: no event
    assert [(e.kind, e.output_id, e.slug) for e in heard] == [("output.updated", old.id, "demo")]


@pytest.mark.requires_postgres
async def test_a_failed_row_read_is_logged_and_announces_nothing(
    tmp_path: Path, pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    """The slug's row read fails (a pool timeout): the rows are the only record (#7257),
    so the announcement is logged as failed and not sent; it never raises."""
    store, old, _, _ = await _legacy_output(tmp_path, pg_pool)

    class _Unreadable:
        def slug_of(self, output_id: str) -> str | None:
            raise TimeoutError("the pool gave no connection")

    store.rows = _Unreadable()
    bus, heard = _output_events()
    with caplog.at_level(logging.ERROR, logger="scadbuddy.library.backfill"):
        _announce(bus, store, old.id)
    assert heard == []
    assert "could not announce a backfill" in caplog.text


@pytest.mark.requires_postgres
async def test_an_unfinished_backfill_is_not_announced(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    store, old, job, _ = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    running = job.model_copy(update={"state": "running", "result": None, "outputs": []})
    bus, heard = _output_events()
    with store_pool(pg_conninfo) as pool:
        attach_job_backfills(store, BlobRefs(pool), _jobs(running), job.id, bus)
    assert heard == []


@pytest.mark.requires_postgres
async def test_a_job_done_event_attaches_its_backfill_promptly(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    bus, heard = _output_events()
    with store_pool(pg_conninfo) as pool:
        # As main.py wires it: the attach announces on the same bus it follows.
        remove = follow_backfills(
            bus, partial(attach_job_backfills, store, BlobRefs(pool), _jobs(job), events=bus)
        )
        try:
            bus.publish(JobEvent(kind="job.done", job_id=job.id, slug="demo"))
            await _until(lambda: store.backfill(old.id) is None, timeout=2.0)
            # #2038: and Arrange, waiting on the output, hears that it was attached.
            await _until(lambda: bool(heard), timeout=2.0)
        finally:
            await remove()
    assert store.manifest(old.id) == written.manifest
    assert [(e.kind, e.output_id) for e in heard] == [("output.updated", old.id)]


@pytest.mark.requires_postgres
async def test_a_failed_re_read_after_an_attach_never_marks_it_failed(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2038: the re-read that decides the announcement runs after the attach is done.
    A ``ValueError`` from it (a half-written ``backfill.json``) must not reach the
    attach's ``fail_backfill``; it announces anyway, which only makes a reader re-read."""
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    reads = 0
    backfill: Callable[[str], BackfillState | None] = store.backfill

    def flaky(output_id: str) -> BackfillState | None:
        nonlocal reads
        reads += 1
        if reads == 2:  # the re-read after the attach
            raise ValueError("half-written backfill.json")
        return backfill(output_id)

    monkeypatch.setattr(store, "backfill", flaky)
    bus, heard = _output_events()
    with store_pool(pg_conninfo) as pool:
        assert attach_backfills(store, BlobRefs(pool), _jobs(job), bus) == 1
    assert reads == 2
    assert store.manifest(old.id) == written.manifest
    assert backfill(old.id) is None  # attached, not marked failed
    assert [e.output_id for e in heard] == [old.id]


async def test_removing_the_follower_waits_for_an_attach_in_flight() -> None:
    """#1759: the lifespan closes the pool after the remover; an attach still running
    in its thread then would fail on a closed pool."""
    started, release = threading.Event(), threading.Event()
    finished: list[str] = []

    def attach(job_id: str) -> int:
        started.set()
        release.wait(5)
        finished.append(job_id)
        return 0

    bus = InProcessEventBus()
    remove = follow_backfills(bus, attach)
    bus.publish(JobEvent(kind="job.done", job_id="j1", slug="demo"))
    await _until(started.is_set)
    removing = asyncio.create_task(remove())
    await asyncio.sleep(0.1)
    assert not removing.done()
    release.set()
    await asyncio.wait_for(removing, 5)
    assert finished == ["j1"]
    bus.publish(JobEvent(kind="job.done", job_id="j2", slug="demo"))
    await asyncio.sleep(0.1)
    assert finished == ["j1"]  # removed: heard no more


@pytest.mark.requires_postgres
async def test_an_attach_needs_one_connection_of_the_pool(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """#1757: the claim's lock must not keep a pool connection while the attach takes
    another, or a pool of one (`SCADBUDDY_DATABASE_POOL_SIZE=1`) never attaches."""
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    with store_pool(pg_conninfo):  # migrated
        pass
    one: ConnectionPool[Connection[DictRow]] = ConnectionPool(
        pg_conninfo,
        min_size=1,
        max_size=1,
        timeout=2,
        connection_class=Connection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    with one:
        refs = BlobRefs(one)
        assert attach_backfills(store, refs, _jobs(job)) == 1
        assert _holders(refs, written.manifest[0].part) == [(OUTPUT_HOLDER, old.id)]
    assert store.manifest(old.id) == written.manifest


@pytest.mark.requires_postgres
async def test_another_jobs_event_attaches_nothing(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    store, old, job, _ = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    with store_pool(pg_conninfo) as pool:
        assert attach_job_backfills(store, BlobRefs(pool), _jobs(job), "someone-else") == 0
    assert store.manifest(old.id) == []


@pytest.mark.requires_postgres
async def test_duplicate_events_and_the_backstop_attach_once(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """Two `job.done` (a replica each) and the Schedule's backstop, all at once."""
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    read: Callable[[str], Job] = _jobs(job)

    def slow(job_id: str) -> Job:
        time.sleep(0.3)  # every caller is inside its attach together
        return read(job_id)

    results: list[int] = []
    lock = threading.Lock()

    def attach(job_id: str) -> int:
        attached = attach_job_backfills(store, refs, slow, job_id)
        with lock:
            results.append(attached)
        return attached

    bus = InProcessEventBus()
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        remove = follow_backfills(bus, attach)
        try:
            bus.publish(JobEvent(kind="job.done", job_id=job.id, slug="demo"))
            bus.publish(JobEvent(kind="job.done", job_id=job.id, slug="demo"))
            backstop = await asyncio.to_thread(attach_backfills, store, refs, slow)
            await _until(lambda: len(results) == 2)
        finally:
            await remove()
        assert sum(results) + backstop == 1
        assert _holders(refs, written.manifest[0].part) == [(OUTPUT_HOLDER, old.id)]
    assert store.manifest(old.id) == written.manifest
    assert store.backfill(old.id) is None


@pytest.mark.requires_postgres
async def test_the_backstop_sweep_attaches_a_backfill_whose_event_was_missed(
    tmp_path: Path, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """The API was down when the job finished: no event, so the Schedule's sweep."""
    store, old, job, written = await _legacy_output(tmp_path, pg_pool)
    store.start_backfill(old.id, job.id)
    with store_pool(pg_conninfo) as pool:
        state = SimpleNamespace(
            outputs=store,
            refs=BlobRefs(pool),
            render=SimpleNamespace(store=SimpleNamespace(read=_jobs(job))),
            events=None,
        )
        activities = main._housekeeping_activities(state)  # type: ignore[arg-type]
        names = [fn.__temporal_activity_definition.name for fn in activities]  # type: ignore[attr-defined]
        assert names == list(SWEEPS)
        sweep = activities[SWEEPS.index(BACKFILL_SWEEP)]
        await ActivityEnvironment().run(sweep)
    assert store.manifest(old.id) == written.manifest
    assert store.backfill(old.id) is None


def test_the_api_runs_no_attach_loop() -> None:
    assert not hasattr(main, "_attach_backfills_forever")
    assert not hasattr(main, "BACKFILL_ATTACH_INTERVAL")
