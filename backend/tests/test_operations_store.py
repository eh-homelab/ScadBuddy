"""`OperationStore` (#1053, spec 2026-10-01 §4.2 "Our record"): a generic command's
record, written only by its `Operation` workflow's activities."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from temporalio.client import WorkflowHandle

from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.core.events import Event, OperationEvent
from scadbuddy.operations.store import Operation, OperationStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.client import reconcile_lost_operations
from scadbuddy.workflows.problems import OPERATION_LOST
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_postgres

FAILED = PrintRunError(status=502, title="Bad Gateway", detail="Bambuddy said no")


class Events:
    def __init__(self) -> None:
        self.published: list[Event] = []

    def publish_in(self, conn: Connection[Any], event: Event) -> None:
        self.published.append(event)


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
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
def store(jobs: JobProjection, events: Events) -> OperationStore:
    return OperationStore(jobs.pool, events=events)


async def insert(
    store: OperationStore,
    op_id: str,
    *,
    key: str = "k",
    run: str = "r1",
    retention: timedelta | None = None,
) -> Operation:
    return await store.insert(
        op_id,
        kind="reprint",
        subject="archive:5",
        key=key,
        request={"archive_id": 5},
        workflow_id=f"op-reprint-{key}",
        workflow_run_id=run,
        retention=retention,
    )


async def test_insert_twice_for_one_execution_returns_the_first_row_and_publishes_once(
    store: OperationStore, events: Events
) -> None:
    first = await insert(store, "a")
    again = await insert(store, "b")
    assert again.id == first.id == "a"
    assert again.status == "running"
    assert [
        (e.operation_id, e.op_kind, e.subject)
        for e in events.published
        if isinstance(e, OperationEvent)
    ] == [("a", "reprint", "archive:5")]


async def test_find_returns_the_keys_newest_operation_whatever_its_status(
    store: OperationStore,
) -> None:
    assert await store.find("k") is None
    await insert(store, "a")
    await store.finish("a", error=FAILED)
    found = await store.find("k")
    assert found is not None and found.id == "a"
    assert found.status == "failed" and found.error == FAILED


async def test_finish_changes_only_a_running_row_and_announces_it_once(
    store: OperationStore, events: Events
) -> None:
    await insert(store, "a")
    done = await store.finish("a", result={"queue_item_id": 7})
    again = await store.finish("a", error=FAILED)
    assert done.status == again.status == "succeeded"
    assert again.result == {"queue_item_id": 7} and again.finished_at is not None
    assert len(events.published) == 2


async def test_get_reads_one_operation(store: OperationStore) -> None:
    await insert(store, "a")
    got = await store.get("a")
    assert got is not None and got.kind == "reprint" and got.subject == "archive:5"
    assert await store.get("nope") is None


async def test_insert_prunes_operations_finished_before_the_retention(
    store: OperationStore, pg_conninfo: str
) -> None:
    await insert(store, "old", key="k1", run="r1")
    await store.finish("old", result={})
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "UPDATE operations SET finished_at = now() - interval '2 days' WHERE id = 'old'"
        )
    await insert(store, "new", key="k2", run="r2", retention=timedelta(days=1))
    assert await store.get("old") is None
    assert await store.get("new") is not None


async def test_running_executions_names_each_running_rows_execution(
    store: OperationStore,
) -> None:
    running = await insert(store, "a", key="k1", run="w1")
    await insert(store, "b", key="k2", run="w2")
    await store.finish("b", result={})
    assert await store.running_executions(timedelta(0)) == [(running.id, "op-reprint-k1", "w1")]
    assert await store.running_executions(timedelta(hours=1)) == []


async def test_reconcile_fails_the_operations_whose_execution_is_gone_or_closed(
    store: OperationStore,
) -> None:
    """Review #1063 1: an execution terminated after ``op_insert`` never runs
    ``op_finish``; its row ends ``failed``, not ``running`` forever. One still running,
    or whose workflow still runs under a later run id (a reset), is left alone."""
    async with temporal_client() as client:
        queue = f"unserved-{uuid.uuid4().hex[:8]}"

        async def started(name: str) -> WorkflowHandle[Any, Any]:
            return await client.start_workflow(
                "Operation", "x", id=f"op-{name}-{uuid.uuid4().hex[:8]}", task_queue=queue
            )

        live, killed = await started("live"), await started("killed")
        await killed.terminate("an operator ended it")
        reset_first = await started("reset")
        await reset_first.terminate("reset")
        reset = await client.start_workflow("Operation", "x", id=reset_first.id, task_queue=queue)
        for op_id, handle in (("live", live), ("killed", killed), ("reset", reset_first)):
            await store.insert(
                op_id,
                kind="reprint",
                subject="archive:5",
                key=op_id,
                request={},
                workflow_id=handle.id,
                workflow_run_id=handle.result_run_id or "",
                retention=None,
            )
        await insert(store, "gone", key="gone", run=str(uuid.uuid4()))
        try:
            ended = await reconcile_lost_operations(client, store, older_than=timedelta(0))
        finally:
            await live.terminate("test over")
            await reset.terminate("test over")
    assert ended == 2
    assert (await store.get("live")).status == "running"  # type: ignore[union-attr]
    assert (await store.get("reset")).status == "running"  # type: ignore[union-attr]
    killed_op = await store.get("killed")
    assert killed_op is not None and killed_op.status == "failed"
    assert killed_op.error == OPERATION_LOST
    assert (await store.get("gone")).status == "failed"  # type: ignore[union-attr]
