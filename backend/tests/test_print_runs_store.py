"""`PrintRunStore` (#470, #1052): a run's record in Postgres, written only by its
`PrintRun` workflow's activities."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from psycopg import Connection

from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.runs import LOST, LOST_UNQUEUED, PrintRun, PrintRunError, PrintRunStore
from scadbuddy.core.events import Event, PrintRunEvent
from scadbuddy.render.pg_store import MIGRATIONS_DIR
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.client import reconcile_lost_runs
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_postgres

OUTPUT = "a" * 32
RESULT = PrintRunResult(library_file_id=1, copies=1, bambuddy_url="http://b/queue")
REFUSED = PrintRunError(status=422, title="Unprocessable Content", detail="no spool")


class Events:
    """Records what is published inside a transaction."""

    def __init__(self) -> None:
        self.published: list[Event] = []

    def publish_in(self, conn: Connection[Any], event: Event) -> None:
        self.published.append(event)


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    """The pool the runs share with the render projection, as the app builds it."""
    store = JobProjection(pg_conninfo, pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
def store(jobs: JobProjection, events: Events) -> PrintRunStore:
    return PrintRunStore(jobs.pool, events=events)


async def accept(
    store: PrintRunStore, key: str = "k", *, run_id: str = "r1", wf_run: str = "w1"
) -> PrintRun:
    return await store.insert_accepted(
        run_id,
        subject=OUTPUT,
        key=key,
        slug="demo",
        workflow_id=f"print-{key}",
        workflow_run_id=wf_run,
        retention=None,
    )


async def test_insert_accepted_twice_returns_the_first_row_and_publishes_once(
    store: PrintRunStore, events: Events
) -> None:
    """Temporal retries the first activity when its worker died after the commit but
    before it reported (§4.2 step 3): the retry must not add a row or announce twice."""
    first = await accept(store, run_id="r1")
    again = await accept(store, run_id="r2")

    assert again.id == first.id == "r1"
    assert again.status == "running"
    assert [type(e) for e in events.published] == [PrintRunEvent]


async def test_a_new_execution_of_the_same_workflow_id_is_a_new_row(store: PrintRunStore) -> None:
    first = await accept(store, run_id="r1", wf_run="w1")
    second = await accept(store, run_id="r2", wf_run="w2")
    assert {first.id, second.id} == {"r1", "r2"}


async def test_finish_is_guarded_on_running_and_publishes_in_the_transaction(
    store: PrintRunStore, events: Events
) -> None:
    run = await accept(store)
    done = await store.succeed(run.id, "demo", RESULT)
    late = await store.fail(run.id, "demo", REFUSED)

    assert done.status == "succeeded" and done.result == RESULT
    assert late.status == "succeeded"  # the second end changed nothing
    assert len(events.published) == 2  # accepted, succeeded; not the no-op fail


async def test_start_enqueue_marks_may_have_queued_on_a_later_failure(store: PrintRunStore) -> None:
    run = await accept(store)
    await store.start_enqueue(run.id)
    failed = await store.fail(run.id, "demo", REFUSED)
    assert failed.may_have_queued


async def test_a_failure_before_any_enqueue_may_not_have_queued(store: PrintRunStore) -> None:
    run = await accept(store)
    failed = await store.fail(run.id, "demo", REFUSED)
    assert not failed.may_have_queued


async def test_find_with_a_request_id_returns_a_failed_run_of_any_age(
    jobs: JobProjection, events: Events
) -> None:
    store = PrintRunStore(jobs.pool, events=events, repeat_window=timedelta(0))
    run = await accept(store)
    await store.fail(run.id, "demo", REFUSED)

    found = await store.find("k", has_request_id=True)
    assert found is not None and found.id == run.id
    assert await store.find("k", has_request_id=False) is None


async def test_find_without_a_request_id_keeps_the_repeat_window(store: PrintRunStore) -> None:
    run = await accept(store)
    running = await store.find("k", has_request_id=False)
    assert running is not None and running.id == run.id
    await store.succeed(run.id, "demo", RESULT)
    found = await store.find("k", has_request_id=False)
    assert found is not None and found.status == "succeeded"
    other = await accept(store, "other", run_id="r9", wf_run="w9")
    await store.fail(other.id, "demo", REFUSED)
    assert await store.find("other", has_request_id=False) is None


async def test_retention_none_keeps_every_row_and_a_number_prunes_older_finished_rows(
    store: PrintRunStore,
) -> None:
    old = await accept(store, "old", run_id="old", wf_run="w-old")
    await store.succeed(old.id, "demo", RESULT)
    await accept(store, "a", run_id="a", wf_run="w-a")
    assert await store.get("old") is not None

    await asyncio.sleep(0.05)
    await store.insert_accepted(
        "b",
        subject=OUTPUT,
        key="b",
        slug="demo",
        workflow_id="print-b",
        workflow_run_id="w-b",
        retention=timedelta(milliseconds=10),
    )
    assert await store.get("old") is None
    assert await store.get("a") is not None  # still running: never pruned


async def test_get_reads_a_row_and_an_unknown_id_is_none(store: PrintRunStore) -> None:
    run = await accept(store)
    got = await store.get(run.id)
    assert got is not None and got.output_id == OUTPUT
    assert await store.get("nope") is None


async def test_fail_lost_says_whether_anything_could_have_been_queued(
    store: PrintRunStore, events: Events
) -> None:
    """Review #1061 F1: a run whose execution ended without recording an outcome."""
    unqueued = await accept(store, "k1", run_id="r1")
    queueing = await accept(store, "k2", run_id="r2")
    await store.start_enqueue(queueing.id)

    first = await store.fail_lost(unqueued.id)
    second = await store.fail_lost(queueing.id)

    assert first.status == second.status == "failed"
    assert first.error == LOST_UNQUEUED and not first.may_have_queued
    assert second.error == LOST and second.may_have_queued
    announced = [event for event in events.published if isinstance(event, PrintRunEvent)]
    assert [event.slug for event in announced[-2:]] == ["demo", "demo"]


async def test_fail_lost_leaves_an_ended_run_alone(store: PrintRunStore) -> None:
    run = await accept(store)
    await store.succeed(run.id, "demo", RESULT)
    assert (await store.fail_lost(run.id)).status == "succeeded"


async def test_running_executions_names_each_running_rows_execution(
    store: PrintRunStore,
) -> None:
    running = await accept(store, "k1", run_id="r1", wf_run="w1")
    done = await accept(store, "k2", run_id="r2", wf_run="w2")
    await store.succeed(done.id, "demo", RESULT)

    assert await store.running_executions(timedelta(0)) == [(running.id, "print-k1", "w1")]
    assert await store.running_executions(timedelta(hours=1)) == []


def test_the_migration_says_every_run_it_ends_may_have_queued(
    jobs: JobProjection,
) -> None:
    """Review #1061 (third) 1: during a rolling update an old pod may still queue a run
    the migration ends, so no row it ends says nothing was queued."""
    migration = (MIGRATIONS_DIR / "20261003T0223Z_print_runs_on_temporal.sql").read_text()
    update = migration[migration.index("UPDATE print_runs") :]
    with jobs.pool.connection() as conn:
        for run_id, attempted in (("r1", False), ("r2", True)):
            conn.execute(
                "INSERT INTO print_runs (id, output_id, idempotency_key, status,"
                " enqueue_attempted) VALUES (%s, %s, %s, 'running', %s)",
                (run_id, OUTPUT, run_id, attempted),
            )
        conn.execute(update)
        rows = conn.execute(
            "SELECT error->>'detail' AS detail, enqueue_attempted FROM print_runs"
        ).fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["enqueue_attempted"]
        assert "check Bambuddy's queue before printing again" in row["detail"]


def test_the_migration_keeps_heartbeat_at_for_pods_that_still_write_it(
    jobs: JobProjection,
) -> None:
    """Review #1061 3a: expand/contract. A pre-#1052 pod still running during the rolling
    update reads and writes ``heartbeat_at``; a later migration drops it."""
    with jobs.pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status)"
            " VALUES ('old', %s, 'old', 'running')",
            (OUTPUT,),
        )
        conn.execute("UPDATE print_runs SET heartbeat_at = now() WHERE id = 'old'")


async def test_reconcile_fails_the_runs_whose_execution_is_gone_or_closed(
    store: PrintRunStore,
) -> None:
    """Review #1061 F1: a terminated execution, or one that is gone, never ends its
    row itself; one still running is left to end it."""
    async with temporal_client() as client:
        queue = f"unserved-{uuid.uuid4().hex[:8]}"
        live = await client.start_workflow(
            "PrintRun", "x", id=f"print-live-{uuid.uuid4().hex[:8]}", task_queue=queue
        )
        killed = await client.start_workflow(
            "PrintRun", "x", id=f"print-killed-{uuid.uuid4().hex[:8]}", task_queue=queue
        )
        await killed.terminate("an operator ended it")
        for run_id, handle in (("live", live), ("killed", killed)):
            await store.insert_accepted(
                run_id,
                subject=OUTPUT,
                key=run_id,
                slug="demo",
                workflow_id=handle.id,
                workflow_run_id=handle.result_run_id or "",
                retention=None,
            )
        await accept(store, "gone", run_id="gone", wf_run=str(uuid.uuid4()))
        try:
            ended = await reconcile_lost_runs(client, store, older_than=timedelta(0))
        finally:
            await live.terminate("test over")
    assert ended == 2
    assert (await store.get("live")).status == "running"  # type: ignore[union-attr]
    assert (await store.get("killed")).error == LOST_UNQUEUED  # type: ignore[union-attr]
    assert (await store.get("gone")).status == "failed"  # type: ignore[union-attr]


async def test_reconcile_leaves_a_run_whose_workflow_was_reset_and_still_runs(
    store: PrintRunStore,
) -> None:
    """Review #1061 (third) 2: a reset terminates the run that inserted the row and
    goes on under a new run id with the same row; the row is not lost."""
    async with temporal_client() as client:
        queue = f"unserved-{uuid.uuid4().hex[:8]}"
        workflow_id = f"print-reset-{uuid.uuid4().hex[:8]}"
        first = await client.start_workflow("PrintRun", "x", id=workflow_id, task_queue=queue)
        await first.terminate("reset")
        reset = await client.start_workflow("PrintRun", "x", id=workflow_id, task_queue=queue)
        await store.insert_accepted(
            "reset",
            subject=OUTPUT,
            key="reset",
            slug="demo",
            workflow_id=workflow_id,
            workflow_run_id=first.result_run_id or "",
            retention=None,
        )
        try:
            ended = await reconcile_lost_runs(client, store, older_than=timedelta(0))
        finally:
            await reset.terminate("test over")
    assert ended == 0
    assert (await store.get("reset")).status == "running"  # type: ignore[union-attr]
