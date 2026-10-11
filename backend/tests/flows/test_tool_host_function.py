"""A flow's `tool(name, args)` (#1057, plan 2026-10-09-durable-phase-6-flows.md Task
D2): the agent's tools as activities on `agent-tools`, here stand-ins registered under
the agent's names on a test queue. An outward tool waits for a person's approval."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.flows.history import outward_since
from scadbuddy.flows.manifest import use_manifest
from scadbuddy.flows.operations import FLOW_DECIDE, flow_kinds
from tests.flows.fake_api import Outward
from tests.flows.flows_support import script

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]


class Tools:
    """The agent's side: every call, by tool, with its activity id and arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, name: str, args: dict[str, Any]) -> None:
        self.calls.append((name, activity.info().activity_id, args))

    def all(self) -> list[Any]:
        @activity.defn(name="get_model")
        async def get_model(args: dict[str, Any]) -> str:
            self._record("get_model", args)
            return f"model {args['slug']}"

        @activity.defn(name="send_to_bambuddy")
        async def send_to_bambuddy(args: dict[str, Any]) -> str:
            self._record("send_to_bambuddy", args)
            return "sent"

        @activity.defn(name="broken")
        async def broken(args: dict[str, Any]) -> str:
            self._record("broken", args)
            raise ApplicationError("the model is gone", type="ToolError", non_retryable=True)

        return [get_model, send_to_bambuddy, broken]


@pytest.fixture(autouse=True)
def manifest() -> Iterator[None]:
    use_manifest({"get_model": "read", "send_to_bambuddy": "outward", "broken": "read"})
    yield
    use_manifest({})


@pytest.fixture
async def tools(outward: Outward) -> AsyncIterator[tuple[Tools, str]]:
    served = Tools()
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    async with Worker(outward.client, task_queue=queue, activities=served.all()):
        yield served, queue


async def test_a_read_tool_runs_under_its_call_id(
    outward: Outward, tools: tuple[Tools, str]
) -> None:
    served, queue = tools
    run_id = await outward.run(
        script("return await tool('get_model', {'slug': 'box'})"), tools_queue=queue
    )
    run = await outward.finished(run_id)
    assert run.result == "result: 'model box'"
    [step] = run.steps
    assert (step.fn, step.outward, step.status) == ("tool:get_model", False, "succeeded")
    assert served.calls == [("get_model", f"tool-{step.call_id}", {"slug": "box"})]


async def test_an_outward_tool_waits_for_approval_and_runs_once(
    outward: Outward, tools: tuple[Tools, str]
) -> None:
    served, queue = tools
    run_id = await outward.run(
        script("return await tool('send_to_bambuddy', {'output_id': 'o1'})"), tools_queue=queue
    )
    call_id = await outward.approval(run_id)
    parked = await outward.store.get_run(run_id)
    assert parked is not None
    assert [(w.fn, w.kind) for w in parked.waiting_on] == [("tool:send_to_bambuddy", "approval")]
    assert served.calls == []
    preview = await outward_since(outward.client, parked, 1)
    assert preview.calls == []
    decide = next(k for k in flow_kinds(outward.store) if k.name == FLOW_DECIDE)
    request = {"run_id": run_id, "call_id": call_id, "approved": True}
    env = ActivityEnvironment(client=outward.client)
    await env.run(decide.run, request, await env.run(decide.check, request))
    run = await outward.finished(run_id)
    assert run.result == "result: 'sent'"
    assert served.calls == [("send_to_bambuddy", f"tool-{call_id}", {"output_id": "o1"})]
    # A Reset to before it would send it again: the preview names it.
    history = await outward.client.get_workflow_handle(run.workflow_id).fetch_history()
    sent = next(
        e.event_id
        for e in history.events
        if e.activity_task_scheduled_event_attributes.activity_id == f"tool-{call_id}"
    )
    point = max(
        e.event_id
        for e in history.events
        if e.HasField("workflow_task_completed_event_attributes") and e.event_id < sent
    )
    again = await outward_since(outward.client, run, point)
    assert [(c.fn, c.call_id) for c in again.calls] == [("tool:send_to_bambuddy", call_id)]


async def test_an_unknown_tool_is_a_script_error_and_sends_nothing(
    outward: Outward, tools: tuple[Tools, str]
) -> None:
    served, queue = tools
    run_id = await outward.run(
        script("try:", "    await tool('nope', {})", "except Exception as e:", "    return str(e)"),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert "there is no tool 'nope'" in (run.result or "")
    assert served.calls == []


async def test_a_tools_own_error_reaches_the_script_once(
    outward: Outward, tools: tuple[Tools, str]
) -> None:
    served, queue = tools
    run_id = await outward.run(
        script(
            "try:", "    await tool('broken', {})", "except Exception as e:", "    return str(e)"
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert "the model is gone" in (run.result or "")
    await asyncio.sleep(0)
    assert [c[0] for c in served.calls] == ["broken"]
    assert [(s.fn, s.status, s.error) for s in run.steps] == [
        ("tool:broken", "failed", "ToolFailedError")
    ]


async def test_the_tiers_are_the_runs_own_from_its_start(
    outward: Outward, tools: tuple[Tools, str]
) -> None:
    """A worker on another image, whose manifest differs, runs the call as the run
    started it: the tiers are in the run's history, not the worker's state."""
    served, queue = tools
    run_id = await outward.run(
        script(
            "await wait_for_human('Go?', 600)",
            "return await tool('send_to_bambuddy', {'output_id': 'o1'})",
        ),
        tools_queue=queue,
    )
    run = await outward.row(run_id, lambda r: r.status == "waiting")
    use_manifest({"send_to_bambuddy": "read"})
    await AgentClient(outward.client, run.workflow_id).provide_callback_result(
        run.waiting_on[0].call_id, result={"answer": "yes"}
    )
    parked = await outward.row(run_id, lambda r: any(w.kind == "approval" for w in r.waiting_on))
    assert [w.fn for w in parked.waiting_on] == ["tool:send_to_bambuddy"]
    assert served.calls == []
