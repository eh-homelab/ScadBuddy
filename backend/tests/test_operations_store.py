"""`OperationStore` (#1053, spec 2026-10-01 §4.2 "Our record"): a generic command's
record, written only by its `Operation` workflow's activities."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg import Connection

from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.core.events import Event, OperationEvent
from scadbuddy.operations.store import Operation, OperationStore
from scadbuddy.render.projection import JobProjection

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
