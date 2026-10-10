"""`ProjectWorkflow` replays the histories it wrote, and survives its worker (#1057, plan
2026-10-09-durable-phase-6-flows.md Task B4, Ruling 13).

The `projects` worker is unversioned: a flow may run for weeks, so a new build picks up
runs an older one started. A change to `ProjectWorkflow`, its host functions or the
harness pin that would not replay these histories goes behind `workflow.patched()`, and
adds a history recorded on the changed code beside them.

The histories were recorded on 6b's workflow (`SCADBUDDY_RECORD_HISTORIES=1`): `slept`
(two `sleep`s, a result), `answered` (a `wait_for_human` answered) and `restarted` (a
`sleep` its worker was stopped during, finished by a new worker). Never re-record them.
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
NAMES = ["slept", "answered", "restarted"]
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
