"""The flow-run sweep (#1057, plan 2026-10-09-durable-phase-6-flows.md Task B6): a row
whose execution closed without finishing it is ended, within one sweep."""

from datetime import timedelta
from typing import Any

import pytest
from psycopg import Connection

from scadbuddy.core.events import Event, FlowRunEvent
from scadbuddy.flows.sweep import sweep_flow_runs
from scadbuddy.workflows.flow_models import FlowRecord
from tests.flows.flows_support import Flows, script

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]


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
    assert await sweep_flow_runs(flows.store, flows.client, quiet=timedelta(0)) == [killed]
    gone = await flows.store.get_run(killed)
    assert gone is not None and gone.status == "terminated"
    assert ("terminated", killed) in [
        (e.status, e.run_id) for e in events.published if isinstance(e, FlowRunEvent)
    ]
    running = await flows.store.get_run(alive)
    assert running is not None and running.status == "running"


async def test_a_run_with_no_execution_is_ended(flows: Flows) -> None:
    definition = await flows.store.create_definition("t", script("return 1"), {})
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
    assert await sweep_flow_runs(flows.store, flows.client, quiet=timedelta(0)) == ["never"]


async def test_a_quiet_row_is_the_only_one_described(flows: Flows) -> None:
    run_id = await flows.start(script("await sleep(60)"))
    await flows.row(run_id, lambda r: len(r.steps) == 1)
    await flows.client.get_workflow_handle(f"flow-{run_id}").terminate(reason="operator")
    assert await sweep_flow_runs(flows.store, flows.client) == []
