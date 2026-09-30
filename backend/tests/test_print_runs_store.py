"""`PrintRunStore` and `PrintRuns` (#470): a run's record in Postgres, and its task."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta

import pytest

from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.runs import (
    LOST_DETAIL,
    LOST_UNQUEUED_DETAIL,
    BeforeEnqueue,
    PrintRuns,
    PrintRunStore,
)
from scadbuddy.core.events import InProcessEventBus
from scadbuddy.render.projection import JobProjection

pytestmark = pytest.mark.requires_postgres

OUTPUT = "a" * 32
RESULT = PrintRunResult(library_file_id=1, copies=1, bambuddy_url="http://b/queue")


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    """The pool the runs share with the render projection, as the app builds it."""
    store = JobProjection(pg_conninfo, pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


async def test_two_claims_of_one_key_racing_get_one_run(jobs: JobProjection) -> None:
    store = PrintRunStore(jobs.pool)
    claims = await asyncio.gather(*(store.claim(OUTPUT, "k") for _ in range(8)))
    assert len({run.id for run, _ in claims}) == 1
    assert sum(created for _, created in claims) == 1


async def test_a_success_is_repeated_only_within_the_window(jobs: JobProjection) -> None:
    store = PrintRunStore(jobs.pool, repeat_window=timedelta(0))
    run, _ = await store.claim(OUTPUT, "k")
    await store.succeed(run.id, RESULT)

    assert await store.find("k") is None
    again, created = await store.claim(OUTPUT, "k")
    assert created and again.id != run.id
    assert (await store.get(run.id)).result == RESULT  # type: ignore[union-attr]


async def test_a_run_this_process_is_still_running_at_shutdown_is_failed(
    jobs: JobProjection,
) -> None:
    store = PrintRunStore(jobs.pool)
    runs = PrintRuns(store, InProcessEventBus())
    run, _ = await store.claim(OUTPUT, "k")
    started = asyncio.Event()

    async def work(before_enqueue: BeforeEnqueue) -> PrintRunResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("never reached")

    runs.start(run, "demo", work)
    await started.wait()
    await runs.aclose()

    ended = await store.get(run.id)
    assert ended is not None and ended.status == "failed"
    # It never reached the queue, so it says nothing was queued and frees its key.
    assert ended.error is not None and ended.error.detail == LOST_UNQUEUED_DETAIL
    assert not ended.may_have_queued
    assert await store.find("k") is None
    assert runs.running == frozenset()


async def test_a_live_run_keeps_its_heartbeat(jobs: JobProjection) -> None:
    store = PrintRunStore(jobs.pool, lost_after=timedelta(milliseconds=300))
    runs = PrintRuns(store, None, heartbeat_interval=0.05)
    run, _ = await store.claim(OUTPUT, "k")
    release = asyncio.Event()

    async def work(before_enqueue: BeforeEnqueue) -> PrintRunResult:
        await release.wait()
        return RESULT

    runs.start(run, "demo", work)
    await asyncio.sleep(0.6)
    assert (await store.get(run.id)).status == "running"  # type: ignore[union-attr]
    release.set()
    while run.id in runs.running:
        await asyncio.sleep(0.01)
    assert (await store.get(run.id)).status == "succeeded"  # type: ignore[union-attr]


async def _until_ended(runs: PrintRuns, run_id: str) -> None:
    while run_id in runs.running:
        await asyncio.sleep(0.01)


async def test_a_run_whose_heartbeat_lapsed_is_not_expired_by_its_own_process(
    jobs: JobProjection,
) -> None:
    """A database blip or a saturated thread pool stops the beats; the process that is
    running the run knows it is alive and does not fail it."""
    store = PrintRunStore(jobs.pool, lost_after=timedelta(0))
    runs = PrintRuns(store, None, heartbeat_interval=3600)
    run, _ = await store.claim(OUTPUT, "k")
    release = asyncio.Event()

    async def work(before_enqueue: BeforeEnqueue) -> PrintRunResult:
        await release.wait()
        await before_enqueue()
        return RESULT

    runs.start(run, "demo", work)
    await asyncio.sleep(0.05)
    assert (await store.get(run.id)).status == "running"  # type: ignore[union-attr]
    again, created = await store.claim(OUTPUT, "k")
    assert not created and again.id == run.id
    release.set()
    await _until_ended(runs, run.id)
    assert (await store.get(run.id)).status == "succeeded"  # type: ignore[union-attr]


async def test_a_lapsed_run_expired_by_another_replica_before_it_queues_never_queues(
    jobs: JobProjection,
) -> None:
    """Another replica cannot tell a slow run from a dead one and fails it, freeing the
    key for a retry. The slow run then finds itself failed at its ``before_enqueue`` and
    stops, so only the retry can queue; its own end does not flip the row back."""
    store = PrintRunStore(jobs.pool, lost_after=timedelta(0))
    other = PrintRunStore(jobs.pool, lost_after=timedelta(0))
    runs = PrintRuns(store, None, heartbeat_interval=3600)
    run, _ = await store.claim(OUTPUT, "k")
    release = asyncio.Event()
    enqueued: list[str] = []

    async def work(before_enqueue: BeforeEnqueue) -> PrintRunResult:
        await release.wait()
        await before_enqueue()
        enqueued.append(run.id)
        return RESULT

    runs.start(run, "demo", work)
    await asyncio.sleep(0.05)
    assert await other.find("k") is None  # expired there, and the key is free
    release.set()
    await _until_ended(runs, run.id)

    ended = await store.get(run.id)
    assert ended is not None and ended.status == "failed"
    assert ended.error is not None and ended.error.detail == LOST_UNQUEUED_DETAIL
    assert enqueued == []


async def test_a_lapsed_run_expired_after_it_started_queueing_keeps_its_key(
    jobs: JobProjection,
) -> None:
    """Expired once it had begun queueing: the print may be on the queue, so a retry
    on the other replica answers with this run, and the slow run's end stays out."""
    store = PrintRunStore(jobs.pool, lost_after=timedelta(0))
    other = PrintRunStore(jobs.pool, lost_after=timedelta(0))
    runs = PrintRuns(store, None, heartbeat_interval=3600)
    run, _ = await store.claim(OUTPUT, "k")
    queueing = asyncio.Event()
    release = asyncio.Event()

    async def work(before_enqueue: BeforeEnqueue) -> PrintRunResult:
        await before_enqueue()
        queueing.set()
        await release.wait()
        return RESULT

    runs.start(run, "demo", work)
    await queueing.wait()
    await asyncio.sleep(0.01)
    held, created = await other.claim(OUTPUT, "k")
    assert not created and held.id == run.id
    assert held.status == "failed" and held.may_have_queued
    assert held.error is not None and held.error.detail == LOST_DETAIL
    release.set()
    await _until_ended(runs, run.id)
    assert (await store.get(run.id)).status == "failed"  # type: ignore[union-attr]


async def test_a_run_expired_after_it_started_queueing_holds_its_key_from_its_real_end(
    jobs: JobProjection,
) -> None:
    """Expired as lost while it queues, the slow run goes on: each beat and its end
    move ``finished_at``, so the key is held for the repeat window after the run
    really stopped, not after the expiry."""
    store = PrintRunStore(jobs.pool, lost_after=timedelta(0))
    other = PrintRunStore(jobs.pool, lost_after=timedelta(0))
    run, _ = await store.claim(OUTPUT, "k")
    await store.start_enqueue(run.id)
    held, created = await other.claim(OUTPUT, "k")
    assert not created and held.status == "failed" and held.may_have_queued

    def backdate() -> None:
        with jobs.pool.connection() as conn:
            conn.execute(
                "UPDATE print_runs SET finished_at = now() - interval '1 hour' WHERE id = %s",
                (run.id,),
            )

    # The expiry was long ago: without the run's own beats the key would be free.
    await asyncio.to_thread(backdate)
    assert await other.find("k") is None
    await store.heartbeat(run.id)
    assert (await other.find("k")).id == run.id  # type: ignore[union-attr]

    await asyncio.to_thread(backdate)
    await store.succeed(run.id, RESULT)
    ended = await other.find("k")
    assert ended is not None and ended.id == run.id
    assert ended.status == "failed" and ended.error is not None
    assert ended.error.detail == LOST_DETAIL


async def test_a_run_expired_before_it_queued_is_not_held_by_its_beats(
    jobs: JobProjection,
) -> None:
    store = PrintRunStore(jobs.pool, lost_after=timedelta(0))
    other = PrintRunStore(jobs.pool, lost_after=timedelta(0))
    run, _ = await store.claim(OUTPUT, "k")
    assert await other.find("k") is None  # expired, never queued: the key is free
    await store.heartbeat(run.id)
    assert await other.find("k") is None
