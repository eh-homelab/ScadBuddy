"""A flow's `tool(name, args)` (#1057, plan 2026-10-09-durable-phase-6-flows.md Task
D2): the agent's tools as activities on `agent-tools`, here stand-ins registered under
the agent's names on a test queue. An outward tool waits for a person's approval."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporalio import activity, workflow
from temporalio.common import RawValue
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer, Worker

from scadbuddy.flows.approval_hash import input_hash
from scadbuddy.flows.history import outward_since
from scadbuddy.flows.operations import FLOW_DECIDE, flow_kinds, request_id
from scadbuddy.workflows.flow_tools import (
    FLOW_BEATLESS_CALL_PATCH,
    FLOW_BEATLESS_TIER_PATCH,
    FLOW_HOST_QUEUE_PATCH,
    FLOW_PINNED_CALL_PATCH,
    FLOW_TOOLS,
)
from scadbuddy.workflows.flows_client import harness_plugins
from scadbuddy.workflows.payload_codec import flows_converter
from scadbuddy.workflows.project import ProjectWorkflow
from scadbuddy.workflows.projects_worker import projects_runner, projects_worker
from tests.flows.fake_api import Outward, outward_activities
from tests.flows.flows_support import HASH_KEY, AgentTiers, Keys, history_payloads, script
from tests.flows.test_project_replay import HISTORIES, RECORD

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

IMAGE = {"name": "a" * 64 + ".png", "mediaType": "image/png", "bytes": 68}


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

        @activity.defn(name="make_thing")
        async def make_thing(args: dict[str, Any]) -> str:
            self._record("make_thing", args)
            raise ApplicationError("lost its answer")

        @activity.defn(name="broken")
        async def broken(args: dict[str, Any]) -> str:
            self._record("broken", args)
            raise ApplicationError("the model is gone", type="ToolError", non_retryable=True)

        @activity.defn(name="snapshot")
        async def snapshot(args: dict[str, Any]) -> Any:
            self._record("snapshot", args)
            # As the agent answers a flow call that returned images (#2477): pointers only.
            return {"text": f"a view\n[image {IMAGE['name']}]", "images": [IMAGE]}

        return [get_model, send_to_bambuddy, make_thing, broken, snapshot]


@pytest.fixture
def agent() -> AgentTiers:
    """The agent's tiers for its stand-in tools, as Settings has them now (#2704)."""
    return AgentTiers(
        {
            "get_model": "read",
            "send_to_bambuddy": "outward",
            "make_thing": "write",
            "broken": "read",
            "snapshot": "read",
        }
    )


@pytest.fixture
async def tools(outward: Outward, agent: AgentTiers) -> AsyncIterator[tuple[Tools, str]]:
    served = Tools()
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    activities = [*served.all(), agent.activity()]
    async with Worker(outward.client, task_queue=queue, activities=activities):
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
    decide = next(k for k in flow_kinds(outward.store, Keys(), HASH_KEY) if k.name == FLOW_DECIDE)
    request = {"run_id": run_id, "call_id": call_id, "approved": True}
    env = ActivityEnvironment(client=outward.client)
    await env.run(decide.run, request, await env.run(decide.check, request))
    # #2318: the approval is bound to the tool and the arguments it parked with, as the
    # agent hashes them again before it runs the call.
    decision = await outward.store.get_decision(request_id(parked, call_id))
    assert decision is not None
    assert decision.input_hash == input_hash(HASH_KEY, "send_to_bambuddy", {"output_id": "o1"})
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


async def test_a_tool_the_agent_raises_in_settings_waits_for_approval(
    outward: Outward, tools: tuple[Tools, str], agent: AgentTiers
) -> None:
    """#2704: the tier is the agent's as the call runs (its definition with Settings'
    raised tiers), not one from a file: a read tool raised to outward parks."""
    served, queue = tools
    agent.tiers["get_model"] = "outward"
    run_id = await outward.run(
        script("return await tool('get_model', {'slug': 'box'})"), tools_queue=queue
    )
    parked = await outward.row(run_id, lambda r: any(w.kind == "approval" for w in r.waiting_on))
    assert [w.fn for w in parked.waiting_on] == ["tool:get_model"]
    assert [s.outward for s in parked.steps] == [True]
    assert agent.asked == ["get_model"]
    assert served.calls == []


async def test_a_tier_lowered_while_the_run_is_under_way_takes_effect_on_its_next_call(
    outward: Outward, tools: tuple[Tools, str], agent: AgentTiers
) -> None:
    """#2171: the agent's tier is taken as given, in either direction: a tool lowered
    since the run started runs without approval. Records `lowered.json`."""
    served, queue = tools
    run_id = await outward.run(
        script(
            "await wait_for_human('Go?', 600)",
            "return await tool('send_to_bambuddy', {'output_id': 'o1'})",
        ),
        tools_queue=queue,
        tool_tiers={"send_to_bambuddy": "outward"},
    )
    run = await outward.row(run_id, lambda r: r.status == "waiting")
    agent.tiers["send_to_bambuddy"] = "read"
    await AgentClient(outward.client, run.workflow_id).provide_callback_result(
        run.waiting_on[0].call_id, result={"answer": "yes"}
    )
    run = await outward.finished(run_id)
    assert run.status == "succeeded"
    assert [s.outward for s in run.steps if s.fn == "tool:send_to_bambuddy"] == [False]
    assert [c[0] for c in served.calls] == ["send_to_bambuddy"]
    if RECORD:
        handle = outward.client.get_workflow_handle(f"flow-{run_id}")
        (HISTORIES / "lowered.json").write_text((await handle.fetch_history()).to_json())


async def test_a_tool_lowered_from_outward_runs_without_approval(
    outward: Outward, tools: tuple[Tools, str], agent: AgentTiers
) -> None:
    served, queue = tools
    agent.tiers["send_to_bambuddy"] = "write"
    run_id = await outward.run(
        script("return await tool('send_to_bambuddy', {'output_id': 'o1'})"),
        tools_queue=queue,
        tool_tiers={"send_to_bambuddy": "outward"},
    )
    run = await outward.finished(run_id)
    assert run.status == "succeeded"
    assert [s.outward for s in run.steps] == [False]
    assert [c[0] for c in served.calls] == ["send_to_bambuddy"]


async def test_the_camera_off_is_refused_before_it_parks(
    outward: Outward, tools: tuple[Tools, str], agent: AgentTiers
) -> None:
    served, queue = tools
    agent.tiers["get_printer_camera"] = "outward"
    agent.disabled.add("get_printer_camera")
    run_id = await outward.run(
        script(
            "try:",
            "    await tool('get_printer_camera', {})",
            "except Exception as e:",
            "    return str(e)",
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert "disabled" in (run.result or "")
    assert run.steps == []
    assert served.calls == []


async def test_a_tool_the_agent_no_longer_has_is_refused_whatever_the_run_started_with(
    outward: Outward, tools: tuple[Tools, str], agent: AgentTiers
) -> None:
    served, queue = tools
    del agent.tiers["get_model"]
    run_id = await outward.run(
        script(
            "try:",
            "    await tool('get_model', {'slug': 'box'})",
            "except Exception as e:",
            "    return str(e)",
        ),
        tools_queue=queue,
        tool_tiers={"get_model": "read"},
    )
    run = await outward.finished(run_id)
    assert "there is no tool 'get_model'" in (run.result or "")
    assert served.calls == []


async def test_a_tool_disabled_in_settings_is_refused_before_it_parks(
    outward: Outward, tools: tuple[Tools, str], agent: AgentTiers
) -> None:
    served, queue = tools
    agent.disabled.add("send_to_bambuddy")
    run_id = await outward.run(
        script(
            "try:",
            "    await tool('send_to_bambuddy', {'output_id': 'o1'})",
            "except Exception as e:",
            "    return str(e)",
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert "send_to_bambuddy is disabled" in (run.result or "")
    assert run.steps == []
    assert served.calls == []


async def test_an_agent_that_cannot_say_the_tier_refuses_the_call(
    outward: Outward, agent: AgentTiers
) -> None:
    """No fallback to the tiers the run started with: an agent-tools worker that does
    not serve `flow_tool_tier` (one from before #2704) refuses every `tool(...)`."""
    served = Tools()
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    async with Worker(outward.client, task_queue=queue, activities=served.all()):
        run_id = await outward.run(
            script(
                "try:",
                "    await tool('get_model', {'slug': 'box'})",
                "except Exception as e:",
                "    return 'refused: ' + str(e)",
            ),
            tools_queue=queue,
            tool_tiers={"get_model": "read"},
        )
        run = await outward.finished(run_id)
    assert (run.result or "").startswith("result: 'refused: ")
    assert served.calls == []


async def test_tooled(outward: Outward, tools: tuple[Tools, str]) -> None:
    """Records `project_workflow_histories/tooled.json` (test_project_replay.py): a read
    tool and an approved outward one at the tiers the run started with. Recorded on the
    workflow before #2704, which never asked the agent, so it replays a run in flight
    across that change."""
    served, queue = tools
    run_id = await outward.run(
        script(
            "a = await tool('get_model', {'slug': 'box'})",
            "b = await tool('send_to_bambuddy', {'output_id': 'o1'})",
            "return [a, b]",
        ),
        tools_queue=queue,
        tool_tiers={"get_model": "read", "send_to_bambuddy": "outward"},
    )
    call_id = await outward.approval(run_id)
    decide = next(k for k in flow_kinds(outward.store, Keys(), HASH_KEY) if k.name == FLOW_DECIDE)
    request = {"run_id": run_id, "call_id": call_id, "approved": True}
    env = ActivityEnvironment(client=outward.client)
    await env.run(decide.run, request, await env.run(decide.check, request))
    run = await outward.finished(run_id)
    assert run.result == "result: ['model box', 'sent']"
    assert [c[0] for c in served.calls] == ["get_model", "send_to_bambuddy"]
    if os.environ.get("SCADBUDDY_RECORD_HISTORIES") == "1":
        handle = outward.client.get_workflow_handle(f"flow-{run_id}")
        (HISTORIES / "tooled.json").write_text((await handle.fetch_history()).to_json())


async def test_a_write_tool_runs_once_even_when_its_attempt_fails(
    outward: Outward, tools: tuple[Tools, str]
) -> None:
    """A write that landed and lost its answer is never sent again: only reads retry."""
    served, queue = tools
    run_id = await outward.run(
        script(
            "try:",
            "    await tool('make_thing', {})",
            "except Exception as e:",
            "    return str(e)",
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert "lost its answer" in (run.result or "")
    assert [c[0] for c in served.calls] == ["make_thing"]


class TurnTools:
    """The agent's turn tools (#2320): `turn_tool_tier` on the tools queue, and each call
    the activity `turn:<server>__<tool>`, here on a replica's own queue, as a pinned
    server's (http_request's) calls are."""

    def __init__(self, host: str) -> None:
        self.host = host
        #: False: every tool is refused as turned off, as after a plugin is disabled.
        self.offered = True
        #: Set: the tier fails with this message, as a busy agent's answer does.
        self.tier_fails: str | None = None
        #: The replicas later tiers name, one a tier, as a deploy replaces them.
        self.next_hosts: list[str] = []
        #: Seconds each tier takes, with no beat: an agent replica from before #4200.
        self.tier_delay_s = 0.0
        #: The same for each call.
        self.call_delay_s = 0.0
        self.tiered: list[str] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def tier(self) -> Any:
        @activity.defn(name="turn_tool_tier")
        async def turn_tool_tier(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args["tool"])
            self.tiered.append(name)
            await asyncio.sleep(self.tier_delay_s)
            if self.next_hosts:
                self.host = self.next_hosts.pop(0)
            if self.tier_fails is not None:
                raise ApplicationError(self.tier_fails, type="Busy", non_retryable=True)
            if not self.offered or not name.startswith("scadbuddy_http__"):
                raise ApplicationError(
                    f"{name} is not offered any more: its plugin or setting was turned off",
                    type="ToolError",
                    non_retryable=True,
                )
            method = args["input"].get("method")
            return {"tier": "read" if method == "GET" else "outward", "task_queue": self.host}

        return turn_tool_tier

    def call(self) -> Any:
        @activity.defn(dynamic=True)
        async def turn_call(args: Sequence[RawValue]) -> str:
            info = activity.info()
            fields = activity.payload_converter().from_payload(args[0].payload, dict)
            self.calls.append((info.activity_type, info.activity_id, fields))
            await asyncio.sleep(self.call_delay_s)
            return f"{fields['method']} done"

        return turn_call


@pytest.fixture
async def turn_tools(outward: Outward) -> AsyncIterator[tuple[TurnTools, str]]:
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    served = TurnTools(f"{queue}.host-1")
    async with (
        Worker(outward.client, task_queue=queue, activities=[served.tier()]),
        Worker(outward.client, task_queue=served.host, activities=[served.call()]),
    ):
        yield served, queue


async def test_a_turn_tool_runs_on_the_queue_its_tier_names(
    outward: Outward, turn_tools: tuple[TurnTools, str]
) -> None:
    served, queue = turn_tools
    run_id = await outward.run(
        script("return await tool('scadbuddy_http__http_request', {'method': 'GET'})"),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert run.result == "result: 'GET done'"
    [step] = run.steps
    assert (step.fn, step.outward) == ("tool:scadbuddy_http__http_request", False)
    assert served.calls == [
        ("turn:scadbuddy_http__http_request", f"tool-{step.call_id}", {"method": "GET"})
    ]


async def test_an_outward_turn_tool_waits_for_approval(
    outward: Outward, turn_tools: tuple[TurnTools, str]
) -> None:
    served, queue = turn_tools
    run_id = await outward.run(
        script("return await tool('scadbuddy_http__http_request', {'method': 'POST'})"),
        tools_queue=queue,
    )
    call_id = await outward.approval(run_id)
    parked = await outward.store.get_run(run_id)
    assert parked is not None
    assert [(w.fn, w.kind) for w in parked.waiting_on] == [
        ("tool:scadbuddy_http__http_request", "approval")
    ]
    assert served.calls == []
    decide = next(k for k in flow_kinds(outward.store, Keys(), HASH_KEY) if k.name == FLOW_DECIDE)
    request = {"run_id": run_id, "call_id": call_id, "approved": True}
    env = ActivityEnvironment(client=outward.client)
    await env.run(decide.run, request, await env.run(decide.check, request))
    run = await outward.finished(run_id)
    assert run.result == "result: 'POST done'"
    assert served.calls == [
        ("turn:scadbuddy_http__http_request", f"tool-{call_id}", {"method": "POST"})
    ]


async def test_an_approved_turn_tool_runs_on_the_replica_live_once_approved(
    outward: Outward,
) -> None:
    """#2937: the replica that tiered an outward call is replaced while the call waits
    for its approval (a deploy). The call goes to the replica live when it is approved,
    not to the gone one's queue, where nothing would ever pick it up."""
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    served = TurnTools(f"{queue}.gone")
    live = f"{queue}.live"
    async with (
        Worker(outward.client, task_queue=queue, activities=[served.tier()]),
        Worker(outward.client, task_queue=live, activities=[served.call()]),
    ):
        run_id = await outward.run(
            script("return await tool('scadbuddy_http__http_request', {'method': 'POST'})"),
            tools_queue=queue,
        )
        call_id = await outward.approval(run_id)
        served.host = live
        await _approve(outward, run_id, call_id)
        run = await outward.finished(run_id)
        if RECORD:
            handle = outward.client.get_workflow_handle(f"flow-{run_id}")
            (HISTORIES / "requeued.json").write_text((await handle.fetch_history()).to_json())
    assert run.result == "result: 'POST done'"
    assert served.tiered == ["scadbuddy_http__http_request"] * 2
    assert served.calls == [
        ("turn:scadbuddy_http__http_request", f"tool-{call_id}", {"method": "POST"})
    ]


async def _approve(outward: Outward, run_id: str, call_id: str) -> None:
    decide = next(k for k in flow_kinds(outward.store, Keys(), HASH_KEY) if k.name == FLOW_DECIDE)
    request = {"run_id": run_id, "call_id": call_id, "approved": True}
    env = ActivityEnvironment(client=outward.client)
    await env.run(decide.run, request, await env.run(decide.check, request))


_CAUGHT_POST = (
    "try:",
    "    await tool('scadbuddy_http__http_request', {'method': 'POST'})",
    "except Exception as e:",
    "    return str(e)",
)


async def test_an_approved_turn_tool_no_replica_picks_up_says_it_needs_approving_again(
    outward: Outward,
) -> None:
    """#3420: an approved call waits `approved_queue_start_s` (longer than a read's), and
    one nothing picks up says the person will be asked to approve the new call again."""
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    served = TurnTools(f"{queue}.gone")
    async with Worker(outward.client, task_queue=queue, activities=[served.tier()]):
        run_id = await outward.run(
            script(*_CAUGHT_POST),
            tools_queue=queue,
            host_queue_start_s=1,
            approved_queue_start_s=2,
        )
        call_id = await outward.approval(run_id)
        await _approve(outward, run_id, call_id)
        run = await outward.finished(run_id)
    result = run.result or ""
    assert "no worker on the agent replica that holds its state picked it up in 2s" in result
    assert "the new call needs approving again" in result
    # Asked to park, once approved, and once more after the wait (#4152): no other replica.
    assert served.tiered == ["scadbuddy_http__http_request"] * 3
    assert served.calls == []


async def test_an_approved_turn_tool_turned_off_while_it_waited_says_so(
    outward: Outward, turn_tools: tuple[TurnTools, str]
) -> None:
    """#3420: a tool whose plugin was turned off while its call waited for approval fails
    as a tool failure that says so, not as "there is no tool"."""
    served, queue = turn_tools
    run_id = await outward.run(script(*_CAUGHT_POST), tools_queue=queue)
    call_id = await outward.approval(run_id)
    served.offered = False
    await _approve(outward, run_id, call_id)
    run = await outward.finished(run_id)
    assert (
        "scadbuddy_http__http_request was not run: it is no longer offered since it was approved"
        in (run.result or "")
    )
    assert served.calls == []


async def test_an_approved_turn_tools_failed_re_tier_says_it_needs_approving_again(
    outward: Outward, turn_tools: tuple[TurnTools, str]
) -> None:
    """#3003 review: a re-tier that fails after the approval spends that approval too, so
    it says the next call is asked again, as the schedule-to-start path does."""
    served, queue = turn_tools
    run_id = await outward.run(script(*_CAUGHT_POST), tools_queue=queue)
    call_id = await outward.approval(run_id)
    served.tier_fails = "the agent is restarting"
    await _approve(outward, run_id, call_id)
    run = await outward.finished(run_id)
    result = run.result or ""
    assert "scadbuddy_http__http_request was not run: the agent is restarting" in result
    assert "The new call needs approving again." in result
    assert served.calls == []


async def test_a_turn_tool_on_a_gone_replicas_queue_fails_instead_of_waiting(
    outward: Outward,
) -> None:
    """#2937: a call on a replica's own queue that no worker polls any more (the replica
    died, and an activity's retries keep its queue) fails, saying so, rather than leave
    the run Running for ever."""
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    served = TurnTools(f"{queue}.gone")
    async with Worker(outward.client, task_queue=queue, activities=[served.tier()]):
        run_id = await outward.run(
            script(
                "try:",
                "    await tool('scadbuddy_http__http_request', {'method': 'GET'})",
                "except Exception as e:",
                "    return str(e)",
            ),
            tools_queue=queue,
            host_queue_start_s=2,
        )
        run = await outward.finished(run_id)
    assert "no worker on the agent replica that holds its state picked it up in 2s" in (
        run.result or ""
    )
    assert served.tiered == ["scadbuddy_http__http_request"] * 2
    assert served.calls == []


async def test_an_approved_turn_tool_in_a_run_from_before_2937_runs_on_the_live_replica(
    outward: Outward, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4152: a run whose first pinned call was made before #2937 replays it with no
    marker, and the SDK keeps that False for `flow-host-queue-2937` for the whole run. Its
    outward call, tiered by a replica since replaced and approved after a reset (here: a
    new worker replaying the run), still asks for the live replica's queue."""
    tools_queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    served = TurnTools(f"{tools_queue}.gone")
    live = f"{tools_queue}.live"
    projects = Outward(outward.client, f"projects-{uuid.uuid4().hex[:8]}", outward.store)
    activities = outward_activities(outward.store, None)
    patched = workflow.patched

    def before_2937(patch_id: str) -> bool:
        if patch_id == FLOW_HOST_QUEUE_PATCH or patch_id.startswith(FLOW_PINNED_CALL_PATCH):
            return False
        return patched(patch_id)

    async with Worker(outward.client, task_queue=tools_queue, activities=[served.tier()]):
        with monkeypatch.context() as m:
            m.setattr(workflow, "patched", before_2937)
            async with (
                projects_worker(outward.client, projects.queue, activities),
                Worker(outward.client, task_queue=served.host, activities=[served.call()]),
            ):
                run_id = await projects.run(
                    script(
                        "await tool('scadbuddy_http__http_request', {'method': 'GET'})",
                        "return await tool('scadbuddy_http__http_request', {'method': 'POST'})",
                    ),
                    tools_queue=tools_queue,
                )
                call_id = await projects.approval(run_id)
        # The replica that tiered the call is gone; a new build replays the run.
        served.host = live
        async with (
            projects_worker(outward.client, projects.queue, activities),
            Worker(outward.client, task_queue=live, activities=[served.call()]),
        ):
            await _approve(projects, run_id, call_id)
            run = await projects.finished(run_id)
            if RECORD:
                handle = outward.client.get_workflow_handle(f"flow-{run_id}")
                (HISTORIES / "rehomed.json").write_text((await handle.fetch_history()).to_json())
    assert run.result == "result: 'POST done'"
    assert served.tiered == ["scadbuddy_http__http_request"] * 3
    assert [(c[0], c[2]) for c in served.calls] == [
        ("turn:scadbuddy_http__http_request", {"method": "GET"}),
        ("turn:scadbuddy_http__http_request", {"method": "POST"}),
    ]
    assert served.calls[1][1] == f"tool-{call_id}"


async def test_an_approved_turn_tool_nothing_picks_up_goes_to_the_replica_live_then(
    outward: Outward,
) -> None:
    """#4152: the replica asked once the call was approved is replaced before it picks the
    call up (a rolling deploy). Nothing ran, so the call goes once more, to the replica the
    agent names then, and the approval is not spent on the gone one."""
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    gone = f"{queue}.gone"
    live = f"{queue}.live"
    served = TurnTools(gone)
    # Tiered to park, again once approved, and again once the wait ran out.
    served.next_hosts = [gone, gone, live]
    async with (
        Worker(outward.client, task_queue=queue, activities=[served.tier()]),
        Worker(outward.client, task_queue=live, activities=[served.call()]),
    ):
        run_id = await outward.run(
            script("return await tool('scadbuddy_http__http_request', {'method': 'POST'})"),
            tools_queue=queue,
            approved_queue_start_s=2,
        )
        call_id = await outward.approval(run_id)
        await _approve(outward, run_id, call_id)
        run = await outward.finished(run_id)
        if RECORD:
            handle = outward.client.get_workflow_handle(f"flow-{run_id}")
            (HISTORIES / "retried.json").write_text((await handle.fetch_history()).to_json())
    assert run.result == "result: 'POST done'"
    assert served.tiered == ["scadbuddy_http__http_request"] * 3
    assert served.calls == [
        ("turn:scadbuddy_http__http_request", f"tool-{call_id}", {"method": "POST"})
    ]


async def test_a_tier_on_agent_replicas_that_never_beat_is_asked_again_without_beats(
    outward: Outward, turn_tools: tuple[TurnTools, str]
) -> None:
    """#4262: every attempt of a `turn_tool_tier` lands on an agent replica from before
    #4200 (a rollout, or a rollback), which sends no beats and takes longer than the
    heartbeat timeout to answer. The tier is asked once more without one, and the call
    runs, instead of the run's tier check failing."""
    served, queue = turn_tools
    served.tier_delay_s = 2.5
    run_id = await outward.run(
        script("return await tool('scadbuddy_http__http_request', {'method': 'GET'})"),
        tools_queue=queue,
        agent_heartbeat_s=1,
    )
    run = await outward.finished(run_id)
    if RECORD:
        handle = outward.client.get_workflow_handle(f"flow-{run_id}")
        (HISTORIES / "beatless.json").write_text((await handle.fetch_history()).to_json())
    assert run.result == "result: 'GET done'"
    # Three attempts that missed their beats, then the one with no heartbeat timeout.
    assert served.tiered == ["scadbuddy_http__http_request"] * 4
    [step] = run.steps
    assert served.calls == [
        ("turn:scadbuddy_http__http_request", f"tool-{step.call_id}", {"method": "GET"})
    ]


async def test_a_beatless_tier_in_a_run_from_before_4262_still_fails_and_replays(
    outward: Outward, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that recorded its tier's heartbeat timeout before #4262 (no marker) replays
    on this code: the timeout reaches the script again, and no second tier is asked."""
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    served = TurnTools(f"{queue}.host-1")
    served.tier_delay_s = 2.5
    projects = Outward(outward.client, f"projects-{uuid.uuid4().hex[:8]}", outward.store)
    activities = outward_activities(outward.store, None)
    # The workflow sandbox passes `temporalio` through, so the run sees this stand-in
    # (as `before_2937` above); were it not, the run would fall back and the assertions
    # on the timeout below would fail.
    patched = workflow.patched

    def before_4262(patch_id: str) -> bool:
        return False if patch_id == FLOW_BEATLESS_TIER_PATCH else patched(patch_id)

    with monkeypatch.context() as m:
        m.setattr(workflow, "patched", before_4262)
        async with (
            projects_worker(outward.client, projects.queue, activities),
            Worker(outward.client, task_queue=queue, activities=[served.tier()]),
        ):
            run_id = await projects.run(
                script(
                    "try:",
                    "    await tool('scadbuddy_http__http_request', {'method': 'GET'})",
                    "except Exception as e:",
                    "    return str(e)",
                ),
                tools_queue=queue,
                agent_heartbeat_s=1,
            )
            run = await projects.finished(run_id)
    assert "Heartbeat timeout" in (run.result or "")
    assert served.tiered == ["scadbuddy_http__http_request"] * 3
    history = await outward.client.get_workflow_handle(f"flow-{run_id}").fetch_history()
    await Replayer(
        workflows=[ProjectWorkflow],
        plugins=harness_plugins(FLOW_TOOLS),
        data_converter=flows_converter(Keys()),
        workflow_runner=projects_runner(),
    ).replay_workflow(history)


async def test_a_read_on_agent_replicas_that_never_beat_is_asked_again_without_beats(
    outward: Outward, turn_tools: tuple[TurnTools, str]
) -> None:
    """#4281: every attempt of a read lands on an agent replica from before #4200, which
    sends no beats and takes longer than the heartbeat timeout to answer. The read is
    asked once more without one, as the tier is (#4262), and the run gets its answer."""
    served, queue = turn_tools
    served.call_delay_s = 2.5
    run_id = await outward.run(
        script("return await tool('scadbuddy_http__http_request', {'method': 'GET'})"),
        tools_queue=queue,
        agent_heartbeat_s=1,
    )
    run = await outward.finished(run_id)
    assert run.result == "result: 'GET done'"
    # Three attempts that missed their beats, then the one with no heartbeat timeout.
    [step] = run.steps
    assert (
        served.calls
        == [("turn:scadbuddy_http__http_request", f"tool-{step.call_id}", {"method": "GET"})] * 4
    )


async def test_a_pinned_read_whose_replica_goes_after_its_missed_beats_moves_to_the_live_one(
    outward: Outward,
) -> None:
    """#4372 review: a read pinned to a replica that never beats misses its beats three
    times, and the replica is gone (a deploy) before the attempt with no heartbeat
    timeout is picked up. That attempt is not a move, so the re-homing try (#4152) is
    still there: the read goes to the replica the agent names then, and answers."""
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    gone = f"{queue}.gone"
    live = f"{queue}.live"
    served = TurnTools(gone)
    served.call_delay_s = 2.5
    # Tiered once for the call, and once more when nothing picked up its last attempt.
    served.next_hosts = [gone, live]
    answered = TurnTools(live)
    async with (
        Worker(outward.client, task_queue=queue, activities=[served.tier()]),
        Worker(outward.client, task_queue=live, activities=[answered.call()]),
    ):
        # A graceful shutdown stops polling at once and lets the attempt in hand run on:
        # the third attempt then ends on its missed beats, and nothing takes the next.
        async with Worker(
            outward.client,
            task_queue=gone,
            activities=[served.call()],
            graceful_shutdown_timeout=timedelta(seconds=5),
        ):
            run_id = await outward.run(
                script("return await tool('scadbuddy_http__http_request', {'method': 'GET'})"),
                tools_queue=queue,
                agent_heartbeat_s=1,
                host_queue_start_s=2,
            )
            # A deadline, so a regression that stops the third attempt fails here.
            async with asyncio.timeout(30):
                while len(served.calls) < 3:
                    await asyncio.sleep(0.05)
        run = await outward.finished(run_id)
    assert run.result == "result: 'GET done'"
    assert len(served.calls) == 3
    assert [call[2] for call in answered.calls] == [{"method": "GET"}]
    assert served.tiered == ["scadbuddy_http__http_request"] * 2


async def test_a_write_that_missed_its_beats_is_not_sent_again_and_says_it_may_have_run(
    outward: Outward, turn_tools: tuple[TurnTools, str]
) -> None:
    """#4281: an approved write lands on a replica that never beats and outlasts the
    heartbeat timeout. It may still land there, so it is not sent again, and the script
    is told it may have run, not only that it timed out."""
    served, queue = turn_tools
    served.call_delay_s = 2.5
    run_id = await outward.run(
        script(
            "try:",
            "    await tool('scadbuddy_http__http_request', {'method': 'POST'})",
            "except Exception as e:",
            "    return str(e)",
        ),
        tools_queue=queue,
        agent_heartbeat_s=1,
    )
    call_id = await outward.approval(run_id)
    await _approve(outward, run_id, call_id)
    run = await outward.finished(run_id)
    assert "scadbuddy_http__http_request may have run" in (run.result or "")
    assert "Check whether it did before calling it again" in (run.result or "")
    assert served.calls == [
        ("turn:scadbuddy_http__http_request", f"tool-{call_id}", {"method": "POST"})
    ]


async def test_a_beatless_read_in_a_run_from_before_4281_still_fails_and_replays(
    outward: Outward, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that recorded a read's heartbeat timeout before #4281 (no marker) replays on
    this code: the timeout reaches the script again, and the read is not asked again."""
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    served = TurnTools(f"{queue}.host-1")
    served.call_delay_s = 2.5
    projects = Outward(outward.client, f"projects-{uuid.uuid4().hex[:8]}", outward.store)
    activities = outward_activities(outward.store, None)
    # As `before_4262` above: the sandbox passes `temporalio` through to the run.
    patched = workflow.patched

    def before_4281(patch_id: str) -> bool:
        return False if patch_id == FLOW_BEATLESS_CALL_PATCH else patched(patch_id)

    with monkeypatch.context() as m:
        m.setattr(workflow, "patched", before_4281)
        async with (
            projects_worker(outward.client, projects.queue, activities),
            Worker(outward.client, task_queue=queue, activities=[served.tier()]),
            Worker(outward.client, task_queue=served.host, activities=[served.call()]),
        ):
            run_id = await projects.run(
                script(
                    "try:",
                    "    await tool('scadbuddy_http__http_request', {'method': 'GET'})",
                    "except Exception as e:",
                    "    return str(e)",
                ),
                tools_queue=queue,
                agent_heartbeat_s=1,
            )
            run = await projects.finished(run_id)
    assert "Heartbeat timeout" in (run.result or "")
    assert len(served.calls) == 3
    history = await outward.client.get_workflow_handle(f"flow-{run_id}").fetch_history()
    await Replayer(
        workflows=[ProjectWorkflow],
        plugins=harness_plugins(FLOW_TOOLS),
        data_converter=flows_converter(Keys()),
        workflow_runner=projects_runner(),
    ).replay_workflow(history)


async def test_a_turn_tool_nothing_offers_is_refused_before_any_call(
    outward: Outward, turn_tools: tuple[TurnTools, str]
) -> None:
    served, queue = turn_tools
    run_id = await outward.run(
        script(
            "try:",
            "    await tool('gone__thing', {})",
            "except Exception as e:",
            "    return str(e)",
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert "there is no tool 'gone__thing': gone__thing is not offered any more" in (
        run.result or ""
    )
    assert served.tiered == ["gone__thing"]
    assert served.calls == []


async def test_tool_answers_text_and_tool_result_answers_the_images_as_pointers(
    outward: Outward, tools: tuple[Tools, str]
) -> None:
    """#2477: `tool()` keeps answering `str` for a call that returned images; `tool_result()`
    gives the images as pointers, which a flow can return."""
    _, queue = tools
    text_run = await outward.run(
        script("return (await tool('snapshot', {})).upper()"), tools_queue=queue
    )
    upper = f"A VIEW\n[IMAGE {str(IMAGE['name']).upper()}]"
    assert (await outward.finished(text_run)).result == f"result: {upper!r}"
    run_id = await outward.run(
        script(
            "r = await tool_result('snapshot', {})",
            "plain = await tool_result('get_model', {'slug': 'box'})",
            "return [r['images'], plain['images'], plain['text']]",
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert run.result == f"result: [[{IMAGE!r}], [], 'model box']"
    assert [s.fn for s in run.steps] == ["tool:snapshot", "tool:get_model"]
    # The run's history holds the pointer and nothing of an image: no PNG bytes, no base64.
    payloads = await history_payloads(outward.client, run.workflow_id)
    assert str(IMAGE["name"]).encode() in payloads
    assert b"\x89PNG" not in payloads
    assert b"iVBORw0KGgo" not in payloads
