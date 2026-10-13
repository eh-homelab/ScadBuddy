"""A flow's `tool(name, args)` (#1057, plan 2026-10-09-durable-phase-6-flows.md Task
D2): the agent's tools as activities on `agent-tools`, here stand-ins registered under
the agent's names on a test queue. An outward tool waits for a person's approval."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporalio import activity
from temporalio.common import RawValue
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.flows.approval_hash import input_hash
from scadbuddy.flows.history import outward_since
from scadbuddy.flows.operations import FLOW_DECIDE, flow_kinds, request_id
from tests.flows.fake_api import Outward
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


async def test_the_agents_tier_never_lowers_the_runs_own(
    outward: Outward, tools: tuple[Tools, str], agent: AgentTiers
) -> None:
    """An agent that answers lower than the tiers the run started with (an older replica,
    a tier lowered since) only raises: the call still waits for approval."""
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
    parked = await outward.row(run_id, lambda r: any(w.kind == "approval" for w in r.waiting_on))
    assert [w.fn for w in parked.waiting_on] == ["tool:send_to_bambuddy"]
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
        self.tiered: list[str] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def tier(self) -> Any:
        @activity.defn(name="turn_tool_tier")
        async def turn_tool_tier(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args["tool"])
            self.tiered.append(name)
            if not name.startswith("scadbuddy_http__"):
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
    if RECORD:
        # `turned.json` (test_project_replay.py), recorded on the workflow before #2937.
        handle = outward.client.get_workflow_handle(f"flow-{run_id}")
        (HISTORIES / "turned.json").write_text((await handle.fetch_history()).to_json())


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
        decide = next(
            k for k in flow_kinds(outward.store, Keys(), HASH_KEY) if k.name == FLOW_DECIDE
        )
        request = {"run_id": run_id, "call_id": call_id, "approved": True}
        env = ActivityEnvironment(client=outward.client)
        await env.run(decide.run, request, await env.run(decide.check, request))
        run = await outward.finished(run_id)
        if RECORD:
            handle = outward.client.get_workflow_handle(f"flow-{run_id}")
            (HISTORIES / "requeued.json").write_text((await handle.fetch_history()).to_json())
    assert run.result == "result: 'POST done'"
    assert served.tiered == ["scadbuddy_http__http_request"] * 2
    assert served.calls == [
        ("turn:scadbuddy_http__http_request", f"tool-{call_id}", {"method": "POST"})
    ]


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
    assert "the agent replica that holds its state is gone" in (run.result or "")
    assert served.calls == []


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
