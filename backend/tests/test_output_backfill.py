"""#902: an output saved before manifests gets one by re-rendering it."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from temporalio.testing import ActivityEnvironment

from scadbuddy import main
from scadbuddy.core.events import Event, InProcessEventBus, JobEvent, OutputEvent
from scadbuddy.library import outputs as outputs_module
from scadbuddy.library.backfill import (
    attach_backfills,
    attach_job_backfills,
    choose_output,
    follow_backfills,
)
from scadbuddy.library.outputs import (
    OUTPUT_HOLDER,
    BackfillState,
    OutputStore,
    hold_parts,
    reap_orphan_holds,
    release_parts,
)
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
async def test_a_job_that_does_not_validate_is_retried_not_failed(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """#1007: a ValidationError reading the job is not the output's fault; the next pass
    tries again instead of marking the backfill failed for good."""
    store, old, job, written = await _legacy_output(tmp_path)
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
    tmp_path: Path, pg_conninfo: str, state: str
) -> None:
    """#1970: Arrange reads the output on this rather than polling it."""
    store, old, job, _ = await _legacy_output(tmp_path)
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
async def test_an_unfinished_backfill_is_not_announced(tmp_path: Path, pg_conninfo: str) -> None:
    store, old, job, _ = await _legacy_output(tmp_path)
    store.start_backfill(old.id, job.id)
    running = job.model_copy(update={"state": "running", "result": None, "outputs": []})
    bus, heard = _output_events()
    with store_pool(pg_conninfo) as pool:
        attach_job_backfills(store, BlobRefs(pool), _jobs(running), job.id, bus)
    assert heard == []


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
            await remove()
    assert store.manifest(old.id) == written.manifest


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
async def test_an_attach_needs_one_connection_of_the_pool(tmp_path: Path, pg_conninfo: str) -> None:
    """#1757: the claim's lock must not keep a pool connection while the attach takes
    another, or a pool of one (`SCADBUDDY_DATABASE_POOL_SIZE=1`) never attaches."""
    store, old, job, written = await _legacy_output(tmp_path)
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
            await remove()
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


def _age_holds(refs: BlobRefs, output_id: str, hours: int) -> None:
    with refs.pool.connection() as conn:
        conn.execute(
            "UPDATE blob_refs SET created_at = now() - make_interval(hours => %s)"
            " WHERE holder_kind = %s AND holder_id = %s",
            (hours, OUTPUT_HOLDER, output_id),
        )


@pytest.mark.requires_postgres
async def test_the_reaper_releases_old_holds_of_outputs_with_no_record(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """#1007: a delete racing a write can leave Parts held by an output with no
    meta.json; the reaper releases them, and only them."""
    store, old, _, written = await _legacy_output(tmp_path)
    part = written.manifest[0].part
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        hold_parts(refs, old.id, written.manifest, old.slug)  # a live output
        hold_parts(refs, "gone", written.manifest, old.slug)  # its record deleted under it
        hold_parts(refs, "saving", written.manifest, old.slug)  # held, meta.json not written yet
        _age_holds(refs, old.id, 2)
        _age_holds(refs, "gone", 2)
        assert reap_orphan_holds(refs, store) == 1
        holders = _holders(refs, part)
        assert (OUTPUT_HOLDER, "gone") not in holders
        assert (OUTPUT_HOLDER, old.id) in holders
        assert (OUTPUT_HOLDER, "saving") in holders  # inside the grace
        assert reap_orphan_holds(refs, store) == 0


@pytest.mark.requires_postgres
async def test_the_reaper_removes_an_orphan_s_old_directory_too(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """#1815: an output whose meta.json is gone leaves a directory no read sees; once
    its holds are released it is removed, past the same grace. One written inside the
    grace (a save still writing) is kept."""
    store, old, _, written = await _legacy_output(tmp_path)
    stale, fresh = "a" * 32, "b" * 32
    two_hours_ago = time.time() - 7200
    for output_id in (stale, fresh):
        directory = store.paths.output_dir(old.slug, output_id)
        directory.mkdir()
        (directory / "model.3mf").write_bytes(b"x")
    os.utime(store.paths.output_dir(old.slug, stale), (two_hours_ago, two_hours_ago))
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        hold_parts(refs, old.id, written.manifest, old.slug)
        for output_id in (old.id, stale, fresh):
            hold_parts(refs, output_id, written.manifest, old.slug)
            _age_holds(refs, output_id, 2)
        assert reap_orphan_holds(refs, store) == 2
    assert not store.paths.output_dir(old.slug, stale).exists()
    assert store.paths.output_dir(old.slug, fresh).is_dir()
    assert store.get(old.id).id == old.id


@pytest.mark.requires_postgres
async def test_the_reaper_releases_nothing_when_it_finds_no_outputs(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """An unmounted or emptied outputs volume reads like every output deleted."""
    store, old, _, written = await _legacy_output(tmp_path)
    part = written.manifest[0].part
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        hold_parts(refs, old.id, written.manifest, old.slug)
        _age_holds(refs, old.id, 2)
        shutil.rmtree(store.paths.outputs)
        assert reap_orphan_holds(refs, store) == 0
        assert (OUTPUT_HOLDER, old.id) in _holders(refs, part)


@pytest.mark.requires_postgres
async def test_the_reaper_stops_on_a_directory_it_cannot_list(
    tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1806 review: glob skips an unreadable slug directory, and its outputs would look
    deleted. The reaper raises and releases nothing instead."""
    store, old, _, written = await _legacy_output(tmp_path)
    part = written.manifest[0].part
    real = os.scandir

    def refusing(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        if str(path) != str(store.paths.outputs):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        hold_parts(refs, old.id, written.manifest, old.slug)
        hold_parts(refs, "gone", written.manifest, old.slug)
        _age_holds(refs, old.id, 2)
        _age_holds(refs, "gone", 2)
        monkeypatch.setattr(os, "scandir", refusing)
        with pytest.raises(PermissionError):
            reap_orphan_holds(refs, store)
        holders = _holders(refs, part)
        assert (OUTPUT_HOLDER, old.id) in holders
        assert (OUTPUT_HOLDER, "gone") in holders


@pytest.mark.requires_postgres
async def test_the_reaper_follows_symlinks_as_the_store_does(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """#1806 review: an output reached through a symlinked directory is served by the
    store, so the reaper must count it live, not release its Parts. A second, plain
    output keeps the no-outputs guard out of the way."""
    store, old, job, written = await _legacy_output(tmp_path)
    store.create(job.model_copy(update={"outputs": [], "id": "plain"}), name="plain")
    part = written.manifest[0].part
    out_dir = store.directory(old.id)
    moved = tmp_path / "elsewhere" / out_dir.name
    moved.parent.mkdir()
    shutil.move(out_dir, moved)
    out_dir.symlink_to(moved, target_is_directory=True)
    assert store.get(old.id).id == old.id  # the store still serves it
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        hold_parts(refs, old.id, written.manifest, old.slug)
        _age_holds(refs, old.id, 2)
        assert reap_orphan_holds(refs, store) == 0
        assert (OUTPUT_HOLDER, old.id) in _holders(refs, part)


def _forget_slug(refs: BlobRefs, output_id: str) -> None:
    """A hold taken before `output_hold_slugs` existed."""
    with refs.pool.connection() as conn:
        conn.execute("DELETE FROM output_hold_slugs WHERE output_id = %s", (output_id,))


def _slug_of(refs: BlobRefs, output_id: str) -> str | None:
    with refs.pool.connection() as conn:
        row = conn.execute(
            "SELECT slug FROM output_hold_slugs WHERE output_id = %s", (output_id,)
        ).fetchone()
    return None if row is None else str(row["slug"])


@pytest.mark.requires_postgres
async def test_the_reaper_releases_nothing_of_a_missing_slug_directory(
    tmp_path: Path, pg_conninfo: str, caplog: pytest.LogCaptureFixture
) -> None:
    """#1806: a slug directory that is missing (not yet copied onto a new volume) must
    not read as every one of its outputs deleted, while another slug's outputs are live.
    A deleted output of a slug whose directory is there is still released."""
    store, old, _, written = await _legacy_output(tmp_path)
    part = written.manifest[0].part
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        hold_parts(refs, old.id, written.manifest, old.slug)
        hold_parts(refs, "uncopied", written.manifest, "builtin:not-copied-yet")
        hold_parts(refs, "gone", written.manifest, old.slug)
        for output_id in (old.id, "uncopied", "gone"):
            _age_holds(refs, output_id, 2)
        with caplog.at_level(logging.ERROR, logger="scadbuddy.library.outputs"):
            assert reap_orphan_holds(refs, store) == 1
        holders = _holders(refs, part)
        assert (OUTPUT_HOLDER, "uncopied") in holders
        assert (OUTPUT_HOLDER, "gone") not in holders
        assert (OUTPUT_HOLDER, old.id) in holders
        assert "builtin:not-copied-yet is missing" in caplog.text


@pytest.mark.requires_postgres
async def test_the_reaper_keeps_holds_of_unknown_template_and_learns_live_ones(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """A hold taken before slugs were recorded: a live output's slug is learnt from its
    directory; one with no output cannot be placed, so it is never released."""
    store, old, _, written = await _legacy_output(tmp_path)
    part = written.manifest[0].part
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        hold_parts(refs, old.id, written.manifest, old.slug)
        hold_parts(refs, "legacy", written.manifest, old.slug)
        for output_id in (old.id, "legacy"):
            _age_holds(refs, output_id, 2)
            _forget_slug(refs, output_id)
        assert reap_orphan_holds(refs, store) == 0
        assert (OUTPUT_HOLDER, "legacy") in _holders(refs, part)
        assert _slug_of(refs, old.id) == old.slug
        assert _slug_of(refs, "legacy") is None


@pytest.mark.requires_postgres
async def test_releasing_an_output_forgets_its_slug(tmp_path: Path, pg_conninfo: str) -> None:
    _, old, _, written = await _legacy_output(tmp_path)
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        hold_parts(refs, old.id, written.manifest, old.slug)
        assert _slug_of(refs, old.id) == old.slug
        release_parts(refs, old.id)
        assert _slug_of(refs, old.id) is None
