"""`ProjectWorkflow` replays the histories it wrote, and survives its worker (#1057, plan
2026-10-09-durable-phase-6-flows.md Task B4, Ruling 13).

The `projects` worker is unversioned: a flow may run for weeks, so a new build picks up
runs an older one started. A change to `ProjectWorkflow`, its host functions or the
harness pin that would not replay these histories goes behind `workflow.patched()`, and
adds a history recorded on the changed code beside them.

The histories were recorded on 6b's workflow (`SCADBUDDY_RECORD_HISTORIES=1`): `slept`
(two `sleep`s, a result), `answered` (a `wait_for_human` answered) and `restarted` (a
`sleep` its worker was stopped during, finished by a new worker); `printed` on 6c's
(`render`, `save_output` and an approved `queue_print`, recorded by
`test_outward_host_functions.py::test_printed`); `tooled` on the workflow before #2704
(a read `tool(...)` and an approved outward one at the tiers the run started with,
recorded by `test_tool_host_function.py::test_tooled`); `inputted` on the workflow of
#2483 (`inputs()` of a flow with an inputs schema, #2663); `turned` on the workflow before
#2937 (an approved outward turn tool on the queue tiered before it parked, recorded once
by `test_tool_host_function.py::test_an_outward_turn_tool_waits_for_approval` on that
code; the test no longer records, as it now runs the patched workflow) and
`requeued` on #2937's (that queue asked for again once approved, recorded by
`test_an_approved_turn_tool_runs_on_the_replica_live_once_approved` before #3420 raised an
approved call's schedule-to-start timeout from 60 s to 600 s: replay does not compare
timeouts, so it still stands for the current code); `rehomed` and `retried` on #4152's
(an approved call in a run whose first pinned call replays from before #2937, sent to the
live replica, recorded by
`test_an_approved_turn_tool_in_a_run_from_before_2937_runs_on_the_live_replica`; and one
nothing picked up, sent once more to the replica live then, recorded by
`test_an_approved_turn_tool_nothing_picks_up_goes_to_the_replica_live_then`); `beatless`
on #4262's (a `turn_tool_tier` whose three attempts missed their beats, asked once more
with no heartbeat timeout, recorded by
`test_a_tier_on_agent_replicas_that_never_beat_is_asked_again_without_beats`). Never
re-record them.
Their payloads are sealed under `flows_support.Keys`, a key derived from each subject.
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flow_tools import FLOW_TOOLS
from scadbuddy.workflows.flows_client import connect_flows, harness_plugins
from scadbuddy.workflows.payload_codec import flows_converter
from scadbuddy.workflows.project import ProjectWorkflow
from scadbuddy.workflows.projects_worker import projects_runner, projects_worker
from tests.flows.flows_support import Flows, Keys, script

HISTORIES = Path(__file__).parent.parent / "fixtures" / "project_workflow_histories"
NAMES = [
    "slept",
    "answered",
    "restarted",
    "printed",
    "tooled",
    "inputted",
    "turned",
    "requeued",
    "rehomed",
    "retried",
    "beatless",
]
RECORD = os.environ.get("SCADBUDDY_RECORD_HISTORIES") == "1"


@pytest.mark.parametrize("name", NAMES)
async def test_a_recorded_history_replays(name: str) -> None:
    path = HISTORIES / f"{name}.json"
    text = path.read_text()
    history = WorkflowHistory.from_json(f"flow-{name}", text)
    replayer = Replayer(
        workflows=[ProjectWorkflow],
        plugins=harness_plugins(FLOW_TOOLS),
        data_converter=flows_converter(Keys()),
        workflow_runner=projects_runner(),
    )
    await replayer.replay_workflow(history)


async def _record(flows: Flows, name: str, run_id: str) -> None:
    if not RECORD:
        return
    history = await flows.client.get_workflow_handle(f"flow-{run_id}").fetch_history()
    HISTORIES.mkdir(parents=True, exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())


@pytest.mark.requires_temporal
@pytest.mark.requires_postgres
async def test_slept(flows: Flows) -> None:
    run_id = await flows.start(script("await sleep(0.5)", "await sleep(0.1)", "return 7"))
    await flows.finished(run_id)
    await _record(flows, "slept", run_id)


@pytest.mark.requires_temporal
@pytest.mark.requires_postgres
async def test_answered(flows: Flows) -> None:
    run_id = await flows.start(
        script("a = await wait_for_human('Swap to pink?', 600)", "return a['answer']")
    )
    run = await flows.row(run_id, lambda r: r.status == "waiting")
    await AgentClient(flows.client, f"flow-{run_id}").provide_callback_result(
        run.waiting_on[0].call_id, result={"answer": "ok"}
    )
    await flows.finished(run_id)
    await _record(flows, "answered", run_id)


@pytest.mark.requires_temporal
@pytest.mark.requires_postgres
async def test_inputted(flows: Flows) -> None:
    """A run of a flow with an inputs schema, whose script reads `inputs()` (#2663)."""
    schema = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "copies": {"type": "integer", "default": 2},
        },
        "required": ["slug"],
    }
    run_id = await flows.start(
        script("i = await inputs()", "return [i['slug'], i['copies']]"),
        inputs_schema=schema,
        inputs={"slug": "box", "copies": 2},
    )
    run = await flows.finished(run_id)
    assert run.result == "result: ['box', 2]"
    await _record(flows, "inputted", run_id)


@pytest.mark.requires_temporal
@pytest.mark.requires_postgres
async def test_a_run_outlives_its_worker_with_each_step_once(
    temporal_address: str, jobs: JobProjection
) -> None:
    client = await connect_flows(temporal_address, "default", Keys())
    store = FlowStore(jobs.pool)
    queue = f"projects-{uuid.uuid4().hex[:8]}"
    flows = Flows(client, queue, store)
    async with projects_worker(client, queue, FlowActivities(store).all()):
        run_id = await flows.start(script("await sleep(0.1)", "await sleep(5)", "return 1"))
        await flows.row(run_id, lambda r: len(r.steps) == 2)
    # The first worker is gone mid-sleep; a new one picks the run up from its history.
    await asyncio.sleep(1)
    async with projects_worker(client, queue, FlowActivities(store).all()):
        run = await flows.finished(run_id)
        await _record(flows, "restarted", run_id)
    assert run.status == "succeeded"
    assert [(s.seq, s.status) for s in run.steps] == [(1, "succeeded"), (2, "succeeded")]
