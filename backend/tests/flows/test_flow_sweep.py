"""The flow-run sweep (#1057, plan 2026-10-09-durable-phase-6-flows.md Task B6): a row
whose execution closed without finishing it is ended, within one sweep."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest
from psycopg import Connection

from scadbuddy.core.events import Event, FlowRunEvent
from scadbuddy.flows.grants import GrantSpec
from scadbuddy.flows.sweep import sweep_flow_runs, sweep_orphan_grants
from scadbuddy.workflows.flow_models import FlowRecord
from tests.flows.flows_support import Flows, define, script

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]


async def backdate(
    flows: Flows, column: Literal["workflow_runs.updated_at", "flow_grants.created_at"], *ids: str
) -> None:
    """Move the rows' time an hour back (#5502): a sweep with a zero margin then picks
    them even when Postgres's wall clock jumps back after the write (WSL2 does)."""
    table, field = column.split(".")
    key = "id" if table == "workflow_runs" else "run_id"

    def update() -> None:
        with flows.store._pool.connection() as conn:
            conn.execute(
                f"UPDATE {table} SET {field} = now() - interval '1 hour' WHERE {key} = ANY(%s)",
                (list(ids),),
            )

    await asyncio.to_thread(update)


class Events:
    def __init__(self) -> None:
        self.published: list[Event] = []

    def publish_in(self, conn: Connection[Any], event: Event) -> None:
        self.published.append(event)


async def test_a_terminated_run_is_ended_and_a_running_one_left(flows: Flows) -> None:
    events = Events()
    flows.store.events = events
    killed = await flows.start(script("await sleep(60)"))
    alive = await flows.start(script("await sleep(60)"))
    for run_id in (killed, alive):
        await flows.row(run_id, lambda r: len(r.steps) == 1)
    await flows.client.get_workflow_handle(f"flow-{killed}").terminate(reason="operator")
    await backdate(flows, "workflow_runs.updated_at", killed, alive)
    assert await sweep_flow_runs(flows.store, flows.client, quiet=timedelta(0)) == [killed]
    gone = await flows.store.get_run(killed)
    assert gone is not None and gone.status == "terminated"
    assert ("terminated", killed) in [
        (e.status, e.run_id) for e in events.published if isinstance(e, FlowRunEvent)
    ]
    running = await flows.store.get_run(alive)
    assert running is not None and running.status == "running"


async def test_a_run_with_no_execution_is_ended(flows: Flows) -> None:
    definition = await define(flows.store, "t")
    await flows.store.insert_run(
        FlowRecord(
            run_id="never",
            definition_id=definition.id,
            version=1,
            name="t",
            workflow_id="flow-00000000-0000-4000-8000-000000000000",
            workflow_run_id="r",
            started_by={},
        )
    )
    await backdate(flows, "workflow_runs.updated_at", "never")
    assert await sweep_flow_runs(flows.store, flows.client, quiet=timedelta(0)) == ["never"]


async def test_a_quiet_row_is_the_only_one_described(flows: Flows) -> None:
    run_id = await flows.start(script("await sleep(60)"))
    await flows.row(run_id, lambda r: len(r.steps) == 1)
    await flows.client.get_workflow_handle(f"flow-{run_id}").terminate(reason="operator")
    assert await sweep_flow_runs(flows.store, flows.client) == []


async def test_a_grant_whose_run_never_started_is_deleted(flows: Flows) -> None:
    """#2481: a start that failed after inserting its grant leaves it with no run; one
    whose run is starting (an open execution, no row yet) is kept."""
    spec = GrantSpec(expires_at=datetime.now(UTC) + timedelta(hours=1))
    orphan = "00000000-0000-4000-8000-0000000000aa"
    await flows.store.insert_grant(orphan, spec, status="approved", requested_by={})
    starting = await flows.start(script("await sleep(60)"))
    await flows.row(starting, lambda r: len(r.steps) == 1)
    await flows.store.delete_run(starting)
    await flows.store.insert_grant(starting, spec, status="approved", requested_by={})
    await backdate(flows, "flow_grants.created_at", orphan, starting)
    swept = await sweep_orphan_grants(flows.store, flows.client, older_than=timedelta(0))
    assert swept == [orphan]
    assert await flows.store.get_grant(orphan) is None
    assert await flows.store.get_grant(starting) is not None
