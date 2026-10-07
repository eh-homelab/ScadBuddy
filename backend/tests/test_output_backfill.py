"""#902: an output saved before manifests gets one by re-rendering it."""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy import main
from scadbuddy.core.events import InProcessEventBus, JobEvent
from scadbuddy.library import outputs as outputs_module
from scadbuddy.library.backfill import (
    attach_backfills,
    attach_job_backfills,
    choose_output,
    follow_backfills,
)
from scadbuddy.library.outputs import OUTPUT_HOLDER, BackfillState, OutputStore, release_parts
from scadbuddy.render.job_models import Job, JobNotFoundError
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.housekeeping import BACKFILL_SWEEP, SWEEPS
from tests.support.arrange import finished_job
from tests.support.store import store_pool


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


async def _legacy_output(tmp_path: Path):  # type: ignore[no-untyped-def]
    """An output saved before manifests, and the done re-render of it."""
    paths, job, written = await finished_job(tmp_path, job_id="rerender")
    store = OutputStore(paths)
    old = store.create(job.model_copy(update={"outputs": [], "id": "old"}), name="mine")
    assert store.manifest(old.id) == []
    return store, old, job, written


@pytest.mark.requires_postgres
async def test_a_finished_rerender_gives_the_output_its_manifest_and_holds(
    tmp_path: Path, pg_conninfo: str
) -> None:
    store, old, job, written = await _legacy_output(tmp_path)
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
    tmp_path: Path, pg_conninfo: str
) -> None:
    """A crash after the hold, before the manifest: the next pass finishes the job, and
    one after that finds nothing to do."""
    store, old, job, written = await _legacy_output(tmp_path)
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
async def test_an_unfinished_rerender_is_left_for_the_next_pass(
    tmp_path: Path, pg_conninfo: str
) -> None:
    store, old, job, _ = await _legacy_output(tmp_path)
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
    tmp_path: Path, pg_conninfo: str, failed: dict[str, str] | None, message: str
) -> None:
    store, old, job, _ = await _legacy_output(tmp_path)
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


def _two_legacy(store: OutputStore, job: Job) -> tuple[str, str]:
    """A second output saved before manifests, of the same render, both waiting on it."""
    first = store.ids_for("demo")[0]
    second = store.create(job.model_copy(update={"outputs": [], "id": "old2"}), name="two").id
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
    tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete lands while the Parts are being held, after the output was read: its
    release has already run, so the pass must drop the holds it took after it."""
    store, _, job, written = await _legacy_output(tmp_path)
    gone, kept = _two_legacy(store, job)
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
    tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete's rmtree lands while the attach writes: the write's temporary file has
    no directory to go to (FileNotFoundError)."""
    store, _, job, written = await _legacy_output(tmp_path)
    gone, kept = _two_legacy(store, job)
    _in_order(store, monkeypatch, gone)
    directory = store.directory(gone)
    replace = outputs_module._replace

    def delete_then_replace(path: Path, text: str) -> None:
        if path.parent == directory and directory.exists():
            shutil.rmtree(directory)
        replace(path, text)

    monkeypatch.setattr(outputs_module, "_replace", delete_then_replace)
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        assert attach_backfills(store, refs, _jobs(job)) == 1
        assert _holders(refs, written.manifest[0].part) == [(OUTPUT_HOLDER, kept)]
    assert store.manifest(kept) == written.manifest


@pytest.mark.requires_postgres
async def test_a_corrupt_output_does_not_stop_the_ones_behind_it(
    tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, job, written = await _legacy_output(tmp_path)
    corrupt, good = _two_legacy(store, job)
    _in_order(store, monkeypatch, corrupt)
    (store.directory(corrupt) / "record.json").write_text("{not json", encoding="utf-8")
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
    tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, job, written = await _legacy_output(tmp_path)
    flaky, good = _two_legacy(store, job)
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
async def test_a_marker_left_after_its_manifest_was_written_is_cleared_not_failed(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """A crash after the manifest, before the marker's removal, then the job pruned."""
    store, old, job, written = await _legacy_output(tmp_path)
    store.start_backfill(old.id, job.id)
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        assert attach_backfills(store, refs, _jobs(job)) == 1
        store.start_backfill(old.id, job.id)  # the marker the crash left
        assert attach_backfills(store, refs, _jobs()) == 0
    assert store.backfill(old.id) is None
    assert store.manifest(old.id) == written.manifest


async def _until(check, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


@pytest.mark.requires_postgres
async def test_a_job_done_event_attaches_its_backfill_promptly(
    tmp_path: Path, pg_conninfo: str
) -> None:
    store, old, job, written = await _legacy_output(tmp_path)
    store.start_backfill(old.id, job.id)
    bus = InProcessEventBus()
    with store_pool(pg_conninfo) as pool:
        remove = follow_backfills(
            bus, partial(attach_job_backfills, store, BlobRefs(pool), _jobs(job))
        )
        try:
            bus.publish(JobEvent(kind="job.done", job_id=job.id, slug="demo"))
            await _until(lambda: store.backfill(old.id) is None, timeout=2.0)
        finally:
            remove()
    assert store.manifest(old.id) == written.manifest


@pytest.mark.requires_postgres
async def test_another_jobs_event_attaches_nothing(tmp_path: Path, pg_conninfo: str) -> None:
    store, old, job, _ = await _legacy_output(tmp_path)
    store.start_backfill(old.id, job.id)
    with store_pool(pg_conninfo) as pool:
        assert attach_job_backfills(store, BlobRefs(pool), _jobs(job), "someone-else") == 0
    assert store.manifest(old.id) == []


@pytest.mark.requires_postgres
async def test_duplicate_events_and_the_backstop_attach_once(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """Two `job.done` (a replica each) and the Schedule's backstop, all at once."""
    store, old, job, written = await _legacy_output(tmp_path)
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
            remove()
        assert sum(results) + backstop == 1
        assert _holders(refs, written.manifest[0].part) == [(OUTPUT_HOLDER, old.id)]
    assert store.manifest(old.id) == written.manifest
    assert store.backfill(old.id) is None


@pytest.mark.requires_postgres
async def test_the_backstop_sweep_attaches_a_backfill_whose_event_was_missed(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """The API was down when the job finished: no event, so the Schedule's sweep."""
    store, old, job, written = await _legacy_output(tmp_path)
    store.start_backfill(old.id, job.id)
    with store_pool(pg_conninfo) as pool:
        state = SimpleNamespace(
            outputs=store,
            refs=BlobRefs(pool),
            render=SimpleNamespace(store=SimpleNamespace(read=_jobs(job))),
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
