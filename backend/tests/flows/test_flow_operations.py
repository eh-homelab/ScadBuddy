"""A person's answer and approval as `projects` operations (#1057, plan
2026-10-09-durable-phase-6-flows.md Task C1): the decision is recorded, then the
harness is told; a decision the harness refuses is deleted again."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.models import Run
from scadbuddy.flows.operations import (
    ALREADY_RESOLVED,
    FLOW_ANSWER,
    FLOW_DECIDE,
    STALE_ENTRY,
    flow_kinds,
    request_id,
)
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.workflows.flow_models import FlowRecord, FlowWaiting, ProjectionWrite
from scadbuddy.workflows.flows_client import harness_plugins
from tests.flows.flows_support import Flows, script
from tests.flows.harness_probe import (
    PROBE_TOOLS,
    PROBE_WORKFLOW,
    ProbeChild,
    ProbeWorkflow,
    probe_close,
)

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]


def kinds(flows: Flows) -> dict[str, OperationKind]:
    return {kind.name: kind for kind in flow_kinds(flows.store)}


async def operate(flows: Flows, kind: OperationKind, request: dict[str, Any]) -> dict[str, Any]:
    """The kind's check, then its run, each as its activity would run it."""
    env = ActivityEnvironment(client=flows.client)
    checked: dict[str, Any] = await env.run(kind.check, request)
    result: dict[str, Any] = await env.run(kind.run, request, checked)
    return result


async def parked(flows: Flows) -> tuple[Run, str]:
    run_id = await flows.start(
        script("a = await wait_for_human('Swap to pink?', 600)", "return a['answer']")
    )
    run = await flows.row(run_id, lambda r: r.status == "waiting")
    return run, run.waiting_on[0].call_id


async def test_an_answer_is_recorded_then_reaches_the_run(flows: Flows) -> None:
    run, call_id = await parked(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    result = await operate(flows, answer, {"run_id": run.id, "call_id": call_id, "answer": "ok"})
    assert result["outcome"] == "answered"
    done = await flows.finished(run.id)
    assert done.result == "result: 'ok'"
    decision = await flows.store.get_decision(request_id(run, call_id))
    assert decision is not None and decision.response == {"answer": "ok"}


async def test_a_second_answer_is_refused_by_the_check(flows: Flows) -> None:
    run, call_id = await parked(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    env = ActivityEnvironment(client=flows.client)
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    checked = await env.run(answer.check, request)
    await env.run(answer.run, request, checked)
    with pytest.raises(ApiError) as err:
        await env.run(answer.check, request)
    # The run moved on, or the decision is on record: either way, nothing is sent.
    assert err.value.status == 409


async def test_an_unknown_call_is_stale_and_leaves_no_decision(flows: Flows) -> None:
    run, call_id = await parked(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    checked = await ActivityEnvironment(client=flows.client).run(answer.check, request)
    # The harness knows no such call (as after a Reset): the decision is deleted.
    ghost = {**request, "call_id": str(uuid.uuid4())}
    ghost_checked = {**checked, "request_id": request_id(run, ghost["call_id"])}
    with pytest.raises(ApiError) as err:
        await ActivityEnvironment(client=flows.client).run(answer.run, ghost, ghost_checked)
    assert (err.value.status, err.value.type) == (409, STALE_ENTRY)
    assert await flows.store.get_decision(ghost_checked["request_id"]) is None
    # The real entry is untouched and can still be answered.
    await operate(flows, answer, request)
    assert (await flows.finished(run.id)).status == "succeeded"


async def test_a_call_not_waiting_is_refused_by_the_check(flows: Flows) -> None:
    run, _ = await parked(flows)
    with pytest.raises(ApiError) as err:
        await operate(
            flows,
            kinds(flows)[FLOW_ANSWER],
            {"run_id": run.id, "call_id": "nope", "answer": "x"},
        )
    assert (err.value.status, err.value.type) == (409, STALE_ENTRY)


async def _gated(flows: Flows) -> tuple[Run, str]:
    """A probe run parked on its gated `outward` tool, with a row naming the approval
    (ProjectWorkflow's own gated host functions come with `print` and `arrange`)."""
    queue = f"probe-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        flows.client,
        task_queue=queue,
        workflows=[ProbeWorkflow, ProbeChild],
        activities=[probe_close],
        plugins=harness_plugins(PROBE_TOOLS),
    )
    task = asyncio.create_task(worker.run())
    flows.cleanup.append((worker, task))
    run_id = str(uuid.uuid4())
    wf_id = f"flow-{run_id}"
    await AgentClient(flows.client, wf_id).start_and_submit_message(
        "execute",
        {"script": script("return await outward('pink')")},
        workflow_name=PROBE_WORKFLOW,
        task_queue=queue,
        start_config=AgentConfig(),
    )
    client = AgentClient(flows.client, wf_id)
    for _ in range(200):
        if pending := await client.get_pending_approvals():
            break
        await asyncio.sleep(0.1)
    call_id = pending[0].tool_id
    described = await flows.client.get_workflow_handle(wf_id).describe()
    definition = await flows.store.create_definition("probe", script("return 1"), {})
    await flows.store.insert_run(
        FlowRecord(
            run_id=run_id,
            definition_id=definition.id,
            version=1,
            name="probe",
            workflow_id=wf_id,
            workflow_run_id=described.run_id,
            started_by={},
        )
    )
    run = await flows.store.project(
        ProjectionWrite(
            run_id=run_id,
            workflow_run_id=described.run_id,
            waiting_add=FlowWaiting(
                call_id=call_id, kind="approval", fn="outward", since=datetime.now(UTC)
            ),
        )
    )
    assert run is not None
    return run, call_id


async def test_an_approval_lets_the_call_run(flows: Flows) -> None:
    run, call_id = await _gated(flows)
    decide = kinds(flows)[FLOW_DECIDE]
    result = await operate(flows, decide, {"run_id": run.id, "call_id": call_id, "approved": True})
    assert result["outcome"] == "approved"
    status = await AgentClient(flows.client, run.workflow_id).get_status()
    assert status.pending_approvals == []


async def test_a_resolved_approval_deletes_the_decision(flows: Flows) -> None:
    run, call_id = await _gated(flows)
    decide = kinds(flows)[FLOW_DECIDE]
    request = {"run_id": run.id, "call_id": call_id, "approved": False, "reason": "not now"}
    checked = await ActivityEnvironment(client=flows.client).run(decide.check, request)
    # Someone else decided first, through the harness itself.
    await AgentClient(flows.client, run.workflow_id).approve_tool(call_id, approved=True)
    with pytest.raises(ApiError) as err:
        await ActivityEnvironment(client=flows.client).run(decide.run, request, checked)
    assert (err.value.status, err.value.type) == (409, ALREADY_RESOLVED)
    assert await flows.store.get_decision(checked["request_id"]) is None
