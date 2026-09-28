"""`PrintRunStore` and `PrintRuns` (#470): a run's record in Postgres, and its task."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from scadbuddy.bambuddy.pipelines import PrintRunResult
from scadbuddy.bambuddy.runs import LOST_DETAIL, PrintRuns, PrintRunStore
from scadbuddy.core.events import InProcessEventBus
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.pg_store import PostgresJobStore

pytestmark = pytest.mark.requires_postgres

OUTPUT = "a" * 32
RESULT = PrintRunResult(library_file_id=1, copies=1, bambuddy_url="http://b/queue")


@pytest.fixture
def jobs(pg_conninfo: str, tmp_path: Path) -> Iterator[PostgresJobStore]:
    store = PostgresJobStore(pg_conninfo, DataPaths(tmp_path / "data"), pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


async def test_two_claims_of_one_key_racing_get_one_run(jobs: PostgresJobStore) -> None:
    store = PrintRunStore(jobs.pool)
    claims = await asyncio.gather(*(store.claim(OUTPUT, "k") for _ in range(8)))
    assert len({run.id for run, _ in claims}) == 1
    assert sum(created for _, created in claims) == 1


async def test_a_success_is_repeated_only_within_the_window(jobs: PostgresJobStore) -> None:
    store = PrintRunStore(jobs.pool, repeat_window=timedelta(0))
    run, _ = await store.claim(OUTPUT, "k")
    await store.succeed(run.id, RESULT)

    assert await store.find("k") is None
    again, created = await store.claim(OUTPUT, "k")
    assert created and again.id != run.id
    assert (await store.get(run.id)).result == RESULT  # type: ignore[union-attr]


async def test_a_run_this_process_is_still_running_at_shutdown_is_failed(
    jobs: PostgresJobStore,
) -> None:
    store = PrintRunStore(jobs.pool)
    runs = PrintRuns(store, InProcessEventBus())
    run, _ = await store.claim(OUTPUT, "k")
    started = asyncio.Event()

    async def work() -> PrintRunResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("never reached")

    runs.start(run, "demo", work)
    await started.wait()
    await runs.aclose()

    ended = await store.get(run.id)
    assert ended is not None and ended.status == "failed"
    assert ended.error is not None and ended.error.detail == LOST_DETAIL
    assert runs.running == frozenset()


async def test_a_live_run_keeps_its_heartbeat(jobs: PostgresJobStore) -> None:
    store = PrintRunStore(jobs.pool, lost_after=timedelta(milliseconds=300))
    runs = PrintRuns(store, None, heartbeat_interval=0.05)
    run, _ = await store.claim(OUTPUT, "k")
    release = asyncio.Event()

    async def work() -> PrintRunResult:
        await release.wait()
        return RESULT

    runs.start(run, "demo", work)
    await asyncio.sleep(0.6)
    assert (await store.get(run.id)).status == "running"  # type: ignore[union-attr]
    release.set()
    while run.id in runs.running:
        await asyncio.sleep(0.01)
    assert (await store.get(run.id)).status == "succeeded"  # type: ignore[union-attr]
