"""The `projects` worker is a Worker Deployment version (#4146): an old build beside a new
one in a rolling update never runs a flow the new API started with a host function only
the new build has, and the queue's calls to and from the agent's queues still route."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.worker import Worker

from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows import client as temporal
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flow_tools import HOST_FUNCTION_NAMES
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.project import PROJECT_WORKFLOW, FlowStart, RunFlow
from scadbuddy.workflows.projects_worker import projects_worker
from scadbuddy.workflows.registered import RegisteredActivities
from tests.flows.flows_support import AgentTiers, Flows, Keys, define, script
from tests.support.deployment import wait_until_build_serves
from tests.support.temporal import terminate_open_workflows
from tests.test_library_versioning import _make

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

#: How long the old build is left current with the run waiting on it: long enough for
#: its workflow task to be retried there more than once.
OLD_BUILD_HOLD = 25.0
#: The host function the old build does not have.
NEW_FUNCTION = "sleep"


def test_the_deployment_is_its_own_and_auto_upgrade() -> None:
    config = temporal.projects_deployment("abc123")
    assert config.use_worker_versioning
    assert config.version.deployment_name == "scadbuddy-projects"
    assert config.version.build_id == "abc123"
    assert config.default_versioning_behavior == VersioningBehavior.AUTO_UPGRADE


async def test_the_worker_tells_its_workflows_what_it_serves(flows: Flows) -> None:
    worker = projects_worker(flows.client, "q", FlowActivities(flows.store).all())
    [registered] = [
        i for i in worker.config()["interceptors"] if isinstance(i, RegisteredActivities)
    ]
    assert registered.host_functions == HOST_FUNCTION_NAMES
    assert registered.names is not None
    assert {"flow_entry_timeout", "flow_record"} <= registered.names


@pytest.fixture
async def client(temporal_address: str) -> AsyncIterator[Client]:
    yield await connect_flows(temporal_address, "default", Keys())


async def _start(client: Client, store: FlowStore, queue: str, body: str, tools_queue: str) -> str:
    """Start a run as the API does (`flows.launch.start_execution`): its start names this
    build's host functions."""
    definition = await define(store, "t")
    run_id = str(uuid.uuid4())
    await AgentClient(client, f"flow-{run_id}").start_and_submit_message(
        "execute",
        RunFlow(script=body).model_dump(),
        workflow_name=PROJECT_WORKFLOW,
        task_queue=queue,
        start_config=AgentConfig(),
        start_data=FlowStart(
            run_id=run_id,
            definition_id=definition.id,
            version=1,
            name="t",
            started_by={"kind": "browser"},
            tools_queue=tools_queue,
            tool_tiers={"get_model": "read"},
            host_functions=sorted(HOST_FUNCTION_NAMES),
        ),
    )
    return run_id


@activity.defn(name="get_model")
async def get_model(args: dict[str, Any]) -> str:
    return f"model {args['slug']}"


async def test_a_run_with_a_new_host_function_waits_for_the_build_that_has_it(
    client: Client, jobs: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old build is current and lacks `sleep`; the new one has it. The start is
    accepted and the run recorded on the old build, then its workflow task fails there
    before the script starts (no step, no timer), and is retried until the new build is
    current, which runs the script. Its `tool(...)` call goes to the agent's unversioned
    `agent-tools` worker."""
    name = f"scadbuddy-projects-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(temporal, "PROJECTS_DEPLOYMENT_NAME", name)
    store = FlowStore(jobs.pool)
    flows = Flows(client, f"projects-{uuid.uuid4().hex[:8]}", store)
    queue = flows.queue
    tools_queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    activities = FlowActivities(store).all()
    old = projects_worker(
        client,
        queue,
        activities,
        deployment_config=temporal.projects_deployment("old"),
        host_functions=HOST_FUNCTION_NAMES - {NEW_FUNCTION},
    )
    new = projects_worker(
        client, queue, activities, deployment_config=temporal.projects_deployment("new")
    )
    agent = Worker(
        client,
        task_queue=tools_queue,
        activities=[get_model, AgentTiers({"get_model": "read"}).activity()],
    )
    body = script(
        f"await {NEW_FUNCTION}(0.1)",
        "return await tool('get_model', {'slug': 'box'})",
    )
    try:
        async with agent, old:
            await _make(client, name, "old")
            assert await wait_until_build_serves(
                client, queue, "old", timeout=30, poll=0.05, max_poll=0.5, deployment_name=name
            )
            # Accepted: the deferral comes after the first task, which carries the Update.
            run_id = await asyncio.wait_for(_start(client, store, queue, body, tools_queue), 30)
            loop = asyncio.get_running_loop()
            began = loop.time()
            await asyncio.sleep(OLD_BUILD_HOLD)
            handle = client.get_workflow_handle(f"flow-{run_id}")
            described = await handle.describe()
            attempt = described.raw_description.pending_workflow_task.attempt
            held = loop.time() - began
            print(f"deferred workflow task: attempt {attempt} after {held:.1f}s")
            assert 2 <= attempt <= held / 5 + 2
            history = await handle.fetch_history()
            scheduled = [
                e.activity_task_scheduled_event_attributes.activity_type.name
                for e in history.events
                if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            ]
            assert scheduled == ["flow_record"]
            assert not [
                e for e in history.events if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
            ]
            recorded = await store.get_run(run_id)
            assert recorded is not None and recorded.steps == []
            async with new:
                await _make(client, name, "new")
                run = await flows.finished(run_id)
    finally:
        await terminate_open_workflows(client, queue)
    assert run.status == "succeeded"
    assert run.result == "result: 'model box'"


PROBE = "projects_probe"


@workflow.defn(name="CallsProjects", sandboxed=False)
class CallsProjects:
    """As the agent's session workflows call `flow_grant_check` (agent `gate.ts`,
    agent-durable): unversioned, on a queue of their own, an activity on `projects`."""

    @workflow.run
    async def run(self, queue: str) -> str:
        result: str = await workflow.execute_activity(
            PROBE, "unused", task_queue=queue, start_to_close_timeout=timedelta(seconds=30)
        )
        return result


async def test_an_unversioned_workflow_reaches_the_versioned_queue(
    client: Client, jobs: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = f"scadbuddy-projects-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(temporal, "PROJECTS_DEPLOYMENT_NAME", name)
    store = FlowStore(jobs.pool)
    queue = f"projects-{uuid.uuid4().hex[:8]}"
    caller_queue = f"agent-{uuid.uuid4().hex[:8]}"

    @activity.defn(name=PROBE)
    async def probe(_: str) -> str:
        return "versioned"

    versioned = projects_worker(
        client,
        queue,
        [*FlowActivities(store).all(), probe],
        deployment_config=temporal.projects_deployment("b1"),
    )
    caller = Worker(client, task_queue=caller_queue, workflows=[CallsProjects])
    try:
        async with versioned, caller:
            await _make(client, name, "b1")
            assert await wait_until_build_serves(
                client, queue, "b1", timeout=30, poll=0.05, max_poll=0.5, deployment_name=name
            )
            answer = await asyncio.wait_for(
                client.execute_workflow(
                    CallsProjects.run,
                    queue,
                    id=f"calls-{uuid.uuid4().hex}",
                    task_queue=caller_queue,
                ),
                60,
            )
    finally:
        await terminate_open_workflows(client, caller_queue)
    assert answer == "versioned"
