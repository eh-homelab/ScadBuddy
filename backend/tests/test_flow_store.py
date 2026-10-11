"""`FlowStore` (#1057, plan 2026-10-09-durable-phase-6-flows.md Task B1): flow
versions, and the run rows only a run's own workflow writes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg import Connection

from scadbuddy.core.events import Event, FlowRunEvent
from scadbuddy.flows.models import RESULT_MAX
from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_models import FlowRecord, FlowStep, FlowWaiting, ProjectionWrite

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 10, 10, tzinfo=UTC)


class Events:
    def __init__(self) -> None:
        self.published: list[Event] = []

    def publish_in(self, conn: Connection[Any], event: Event) -> None:
        self.published.append(event)

    def runs(self) -> list[tuple[str, str]]:
        return [(e.run_id, e.status) for e in self.published if isinstance(e, FlowRunEvent)]


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
def store(jobs: JobProjection, events: Events) -> FlowStore:
    return FlowStore(jobs.pool, events=events)


SCRIPT = "import asyncio\nasync def main():\n    return 1\nasyncio.run(main())"


async def started(store: FlowStore, run_id: str = "r1", *, session: str | None = None) -> str:
    definition = await store.create_definition("swap", SCRIPT, {"kind": "browser"})
    author: dict[str, Any] = {"kind": "browser"}
    if session is not None:
        author = {"kind": "agent", "principal": "p", "session": session}
    await store.insert_run(
        FlowRecord(
            run_id=run_id,
            definition_id=definition.id,
            version=definition.version,
            name=definition.name,
            workflow_id=f"flow-{run_id}",
            workflow_run_id="wr1",
            started_by=author,
        )
    )
    return definition.id


def step(call_id: str, seq: int, status: str = "running") -> FlowStep:
    return FlowStep.model_validate(
        {
            "seq": seq,
            "fn": "sleep",
            "call_id": call_id,
            "status": status,
            "outward": False,
            "started_at": NOW,
            "history_length": 10 + seq,
        }
    )


async def test_versions_count_up_per_name(store: FlowStore) -> None:
    one = await store.create_definition("swap", SCRIPT, {"kind": "browser"})
    two = await store.create_definition("swap", SCRIPT + "\n", {"kind": "browser"})
    other = await store.create_definition("other", SCRIPT, {"kind": "browser"})
    assert (one.version, two.version, other.version) == (1, 2, 1)
    listed = await store.list_definitions()
    assert [(d.name, d.version) for d in listed] == [("other", 1), ("swap", 2)]
    got = await store.get_definition(one.id)
    assert got is not None and got.script == SCRIPT


async def test_a_definition_keeps_its_approval_timeout(store: FlowStore) -> None:
    never = await store.create_definition("a", SCRIPT, {}, approval_timeout_s=0)
    inherit = await store.create_definition("b", SCRIPT, {})
    assert (never.approval_timeout_s, inherit.approval_timeout_s) == (0, None)


async def test_insert_run_twice_returns_the_first_row_and_publishes_once(
    store: FlowStore, events: Events
) -> None:
    definition_id = await started(store)
    again = await store.insert_run(
        FlowRecord(
            run_id="r1",
            definition_id=definition_id,
            version=1,
            name="swap",
            workflow_id="flow-r1",
            workflow_run_id="other",
            started_by={},
        )
    )
    assert (again.workflow_run_id, again.status) == ("wr1", "running")
    assert events.runs() == [("r1", "running")]


async def test_steps_merge_by_call_id_in_order(store: FlowStore) -> None:
    await started(store)
    for write in (step("b", 2), step("a", 1), step("a", 1, "succeeded")):
        await store.project(ProjectionWrite(run_id="r1", workflow_run_id="wr1", step=write))
    run = await store.get_run("r1")
    assert run is not None
    assert [(s.call_id, s.status) for s in run.steps] == [("a", "succeeded"), ("b", "running")]


async def test_waiting_moves_the_status_and_back(store: FlowStore) -> None:
    await started(store)
    waiting = FlowWaiting(call_id="c", kind="answer", fn="wait_for_human", prompt="q", since=NOW)
    run = await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr1", waiting_add=waiting)
    )
    assert run is not None and run.status == "waiting" and run.waiting_on == [waiting]
    run = await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr1", waiting_remove="c")
    )
    assert run is not None and (run.status, run.waiting_on) == ("running", [])


async def test_a_finished_run_is_not_moved_back(store: FlowStore, events: Events) -> None:
    await started(store)
    await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr1", status="succeeded", result="result: 7")
    )
    run = await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr1", step=step("x", 1))
    )
    assert run is not None and (run.status, run.steps) == ("succeeded", [])
    assert events.runs() == [("r1", "running"), ("r1", "succeeded")]
    reopened = await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr2", status="running", reset=True)
    )
    assert reopened is not None and reopened.status == "running"


async def test_project_replaces_the_workflow_run_id(store: FlowStore) -> None:
    await started(store)
    run = await store.project(ProjectionWrite(run_id="r1", workflow_run_id="wr2"))
    assert run is not None and run.workflow_run_id == "wr2"


async def test_a_long_result_is_cut_and_flagged(store: FlowStore) -> None:
    await started(store)
    run = await store.project(
        ProjectionWrite(
            run_id="r1", workflow_run_id="wr1", status="succeeded", result="x" * (RESULT_MAX + 1)
        )
    )
    assert run is not None and run.result_truncated and len(run.result or "") == RESULT_MAX


async def test_project_without_a_row_is_none(store: FlowStore) -> None:
    assert await store.project(ProjectionWrite(run_id="nope", workflow_run_id="w")) is None


async def test_list_runs_filters_on_the_starting_session(store: FlowStore) -> None:
    await started(store, "r1", session="s1")
    await started(store, "r2")
    assert [r.id for r in await store.list_runs(session="s1")] == ["r1"]
    assert {r.id for r in await store.list_runs()} == {"r1", "r2"}


async def test_mark_terminated_ends_only_an_open_run(store: FlowStore, events: Events) -> None:
    await started(store)
    assert [r.id for r in await store.open_runs(timedelta(0))] == ["r1"]
    run = await store.mark_terminated("r1")
    assert run is not None and run.status == "terminated"
    assert await store.mark_terminated("r1") is None
    assert events.runs()[-1] == ("r1", "terminated")
    assert await store.open_runs(timedelta(0)) == []


async def test_delete_run(store: FlowStore) -> None:
    await started(store)
    assert await store.delete_run("r1")
    assert await store.get_run("r1") is None
    assert not await store.delete_run("r1")
