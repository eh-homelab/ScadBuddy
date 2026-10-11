"""A flow's `agent(...)` and `ask_session(...)` (#1057, plan 2026-10-09-durable-phase-6-flows.md
§6g): a durable session started or sent to with the agent's `sessions_start` and
`sessions_send` tools, and its turn waited for with `flow_session_turn`, all on
`agent-tools`. Here stand-ins registered under the agent's names on a test queue."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from scadbuddy.flows.history import outward_since
from scadbuddy.flows.manifest import use_manifest
from tests.flows.fake_api import Outward
from tests.flows.flows_support import script

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

DURABLE = "11111111-1111-4111-8111-111111111111"
CLASSIC = "22222222-2222-4222-8222-222222222222"


def _envelope(tool: str, content: dict[str, Any]) -> str:
    """A tool's text as the agent returns it (`agent/src/safety/untrusted.ts`)."""
    return json.dumps(
        {"untrusted_data": {"tool": tool, "source": "transcript", "content": content}}
    )


class Sessions:
    """The agent's side: the calls it got, and the turn each session answers with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.answer = "plain words"
        self.outward_calls = 0
        self.fail_turn: str | None = None

    def _record(self, name: str, args: dict[str, Any]) -> None:
        self.calls.append((name, activity.info().activity_id, args))

    def all(self) -> list[Any]:
        @activity.defn(name="sessions_start")
        async def sessions_start(args: dict[str, Any]) -> str:
            self._record("sessions_start", args)
            return _envelope(
                "sessions_start",
                {"session": {"id": DURABLE, "mode": "durable"}, "turn_id": "t1", "after_seq": 0},
            )

        @activity.defn(name="sessions_send")
        async def sessions_send(args: dict[str, Any]) -> str:
            self._record("sessions_send", args)
            return _envelope(
                "sessions_send", {"session": {"id": args["session_id"]}, "turn_id": "t2"}
            )

        @activity.defn(name="flow_session_check")
        async def flow_session_check(args: dict[str, Any]) -> dict[str, Any]:
            self._record("flow_session_check", args)
            if args["session_id"] != DURABLE:
                raise ApplicationError(
                    f"session {args['session_id']} is classic; a flow talks only to a durable one",
                    type="SessionRefused",
                    non_retryable=True,
                )
            return {"session_id": DURABLE, "mode": "durable"}

        @activity.defn(name="flow_session_turn")
        async def flow_session_turn(args: dict[str, Any]) -> dict[str, Any]:
            self._record("flow_session_turn", args)
            if self.fail_turn is not None:
                raise ApplicationError(self.fail_turn, type="TurnFailed", non_retryable=True)
            return {
                "session_id": args["session_id"],
                "turn_id": args["turn_id"],
                "text": self.answer,
                "outward_calls": self.outward_calls,
            }

        return [sessions_start, sessions_send, flow_session_check, flow_session_turn]


@pytest.fixture(autouse=True)
def manifest() -> Iterator[None]:
    use_manifest({"sessions_start": "write", "sessions_send": "write"})
    yield
    use_manifest({})


@pytest.fixture
async def sessions(outward: Outward) -> AsyncIterator[tuple[Sessions, str]]:
    served = Sessions()
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    async with Worker(outward.client, task_queue=queue, activities=served.all()):
        yield served, queue


async def test_agent_starts_a_durable_session_and_answers_with_its_turn(
    outward: Outward, sessions: tuple[Sessions, str]
) -> None:
    served, queue = sessions
    run_id = await outward.run(
        script("return await agent('Size the door', ['authoring'], None)"), tools_queue=queue
    )
    run = await outward.finished(run_id)
    assert run.result == "result: 'plain words'"
    [step] = run.steps
    assert (step.fn, step.outward, step.status, step.session_id) == (
        "agent",
        False,
        "succeeded",
        DURABLE,
    )
    [(start, start_id, start_args), (wait, wait_id, wait_args)] = served.calls
    assert (start, start_id) == ("sessions_start", f"tool-{step.call_id}")
    assert start_args["mode"] == "durable"
    assert start_args["prompt"].startswith("Size the door")
    assert "authoring" in start_args["prompt"]
    assert (wait, wait_id, wait_args) == (
        "flow_session_turn",
        f"turn-agent-{step.call_id}",
        {"session_id": DURABLE, "turn_id": "t1"},
    )


async def test_agent_returns_the_answer_its_schema_asks_for(
    outward: Outward, sessions: tuple[Sessions, str]
) -> None:
    served, queue = sessions
    served.answer = '```json\n{"width": 40}\n```'
    schema = {"type": "object", "properties": {"width": {"type": "number"}}, "required": ["width"]}
    run_id = await outward.run(
        script(f"answer = await agent('Size the door', [], {schema!r})", "return answer['width']"),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert run.result == "result: 40"
    prompt = served.calls[0][2]["prompt"]
    assert json.dumps(schema) in prompt


async def test_an_answer_that_misses_its_schema_is_a_script_error(
    outward: Outward, sessions: tuple[Sessions, str]
) -> None:
    served, queue = sessions
    served.answer = '{"height": 40}'
    schema = {"type": "object", "required": ["width"]}
    run_id = await outward.run(
        script(
            "try:",
            f"    await agent('Size the door', [], {schema!r})",
            "except Exception as e:",
            "    return str(e)",
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert "'width' is a required property" in (run.result or "")
    assert [(s.fn, s.status) for s in run.steps] == [("agent", "failed")]


async def test_ask_session_sends_to_a_durable_session_and_waits_for_its_turn(
    outward: Outward, sessions: tuple[Sessions, str]
) -> None:
    served, queue = sessions
    run_id = await outward.run(
        script(f"return await ask_session({DURABLE!r}, 'And the window?')"), tools_queue=queue
    )
    run = await outward.finished(run_id)
    assert run.result == "result: 'plain words'"
    [step] = run.steps
    assert (step.fn, step.session_id) == ("ask_session", DURABLE)
    assert [(name, args) for name, _, args in served.calls] == [
        ("flow_session_check", {"session_id": DURABLE}),
        ("sessions_send", {"session_id": DURABLE, "text": "And the window?"}),
        ("flow_session_turn", {"session_id": DURABLE, "turn_id": "t2"}),
    ]


async def test_ask_session_refuses_a_classic_session_and_sends_nothing(
    outward: Outward, sessions: tuple[Sessions, str]
) -> None:
    served, queue = sessions
    run_id = await outward.run(
        script(
            "try:",
            f"    await ask_session({CLASSIC!r}, 'hi')",
            "except Exception as e:",
            "    return str(e)",
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert "is classic" in (run.result or "")
    assert [name for name, _, _ in served.calls] == ["flow_session_check"]


async def test_a_turn_that_failed_is_a_script_error(
    outward: Outward, sessions: tuple[Sessions, str]
) -> None:
    served, queue = sessions
    served.fail_turn = "the turn was interrupted"
    run_id = await outward.run(
        script(
            "try:",
            "    await agent('go', [], None)",
            "except Exception as e:",
            "    return str(e)",
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert "the turn was interrupted" in (run.result or "")


@pytest.mark.parametrize("outward_calls", [0, 2])
async def test_a_reset_preview_counts_a_turn_that_made_an_outward_call(
    outward: Outward, sessions: tuple[Sessions, str], outward_calls: int
) -> None:
    served, queue = sessions
    served.outward_calls = outward_calls
    run_id = await outward.run(script("return await agent('go', [], None)"), tools_queue=queue)
    run = await outward.finished(run_id)
    [step] = run.steps
    history = await outward.client.get_workflow_handle(run.workflow_id).fetch_history()
    started = next(
        e.event_id
        for e in history.events
        if e.activity_task_scheduled_event_attributes.activity_id == f"tool-{step.call_id}"
    )
    point = max(
        e.event_id
        for e in history.events
        if e.HasField("workflow_task_completed_event_attributes") and e.event_id < started
    )
    preview = await outward_since(outward.client, run, point)
    expected = [("agent", step.call_id)] if outward_calls else []
    assert [(c.fn, c.call_id) for c in preview.calls] == expected
