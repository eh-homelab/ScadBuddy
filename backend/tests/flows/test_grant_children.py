"""A run grant reaches the sessions the run starts (#2481, spec 2026-10-11 §3.4, §4.3,
plan Task A5): `agent`/`ask_session` take a narrowing (`grant`) and `on_outside`,
recorded with the step; `flow_grant_check` checks a child's call against the run's own
record of that step, from the session's own workflow, never from what it is told."""

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.flows.grants import GrantSpec
from scadbuddy.workflows.flow_grants import FlowGrants
from scadbuddy.workflows.flow_models import GrantCheck, GrantVerdict, StepPolicy
from tests.flows import test_agent_host_functions as host
from tests.flows.fake_api import FakeApi, Outward
from tests.flows.flows_support import HASH_KEY, script
from tests.flows.test_agent_host_functions import DURABLE, Sessions
from tests.flows.test_grant_gate import api_client_of

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

# The agent's stand-ins (sessions_start, sessions_send, flow_session_turn) on a test queue.
sessions = host.sessions

SEND = {"output_id": "o1", "printer_id": 3}


def grant(*rules: dict[str, Any], extends: bool = True) -> GrantSpec:
    return GrantSpec.model_validate(
        {
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "sessions": extends,
            "rules": list(rules),
        }
    )


SEND_RULE = {"fn": "tool", "tool": "send_to_bambuddy"}


async def test_agent_records_its_narrowing_and_on_outside_with_the_step(
    outward: Outward, sessions: tuple[Sessions, str]
) -> None:
    _, queue = sessions
    narrowing = {"rules": [{**SEND_RULE, "max_calls": 1}]}
    run_id = await outward.run(
        script(
            f"a = await agent('go', [], None, grant={narrowing!r}, on_outside='deny')",
            f"b = await ask_session({DURABLE!r}, 'more')",
            "return [a, b]",
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert run.status == "succeeded", run.result
    first, second = run.steps
    assert first.policy == StepPolicy.model_validate({"grant": narrowing, "on_outside": "deny"})
    # Left out: no narrowing, and escalate (amendment Ruling 9).
    assert second.policy == StepPolicy(grant=None, on_outside="escalate")


@pytest.mark.parametrize(
    "arguments",
    [
        "grant={'rules': [{'fn': 'tool', 'tool': 'send_to_bambuddy', 'max_calls': -1}]}",
        "grant={'rules': [], 'expires_at': '2026-10-13T00:00:00Z'}",
        "on_outside='maybe'",
    ],
)
async def test_a_bad_narrowing_or_on_outside_is_a_script_error_and_starts_nothing(
    outward: Outward, sessions: tuple[Sessions, str], arguments: str
) -> None:
    served, queue = sessions
    run_id = await outward.run(
        script(
            "try:",
            f"    await agent('go', [], None, {arguments})",
            "except Exception as e:",
            "    return type(e).__name__",
        ),
        tools_queue=queue,
    )
    run = await outward.finished(run_id)
    assert run.result is not None and "Error" in run.result
    assert served.calls == []


class Child:
    """A run whose `agent(...)` step talked to session DURABLE, and the session's own
    workflow's view of the check."""

    def __init__(self, outward: Outward, api: FakeApi, run_id: str, call_id: str) -> None:
        self.outward = outward
        self.run_id = run_id
        self.call_id = call_id
        self.env = ActivityEnvironment(client=outward.client)
        self.env.info = dataclasses.replace(
            self.env.info, workflow_id=f"session-{DURABLE}", workflow_run_id="swr"
        )
        self.grants = FlowGrants(outward.store, api_client_of(api), HASH_KEY)

    async def check(self, use: str, args: dict[str, Any] = SEND, **over: Any) -> GrantVerdict:
        body: dict[str, Any] = {
            "run_id": self.run_id,
            "request_id": f"durable:{DURABLE}:swr:{use}",
            "fn": "tool",
            "tool": "send_to_bambuddy",
            "args": args,
            "session_id": DURABLE,
            "flow_call_id": self.call_id,
        }
        return await self.env.run(self.grants.check, GrantCheck(**(body | over)))


async def child(
    outward: Outward,
    api: FakeApi,
    sessions: tuple[Sessions, str],
    spec: GrantSpec,
    narrowing: Any = None,
) -> Child:
    """A run whose `agent(...)` turn in session DURABLE is still running: a flow turn."""
    served, queue = sessions
    if served.hold is None:
        served.hold = asyncio.Event()
    run_id = await outward.run(
        script(f"return await agent('go', [], None, grant={narrowing!r})"),
        tools_queue=queue,
        grant=spec,
    )
    run = await outward.row(
        run_id, lambda r: [(s.session_id, s.status) for s in r.steps] == [(DURABLE, "running")]
    )
    return Child(outward, api, run_id, run.steps[0].call_id)


async def test_a_child_call_in_a_flow_turn_inside_the_grant_is_inside(
    outward: Outward, api: FakeApi, sessions: tuple[Sessions, str]
) -> None:
    c = await child(outward, api, sessions, grant(SEND_RULE))
    verdict = await c.check("toolu_1")
    assert verdict.verdict == "inside", verdict
    [use] = await outward.store.grant_uses(c.run_id)
    assert (use.session_id, use.request_id) == (DURABLE, f"durable:{DURABLE}:swr:toolu_1")


async def test_a_grant_not_extended_to_sessions_admits_no_child_call(
    outward: Outward, api: FakeApi, sessions: tuple[Sessions, str]
) -> None:
    c = await child(outward, api, sessions, grant(SEND_RULE, extends=False))
    assert (await c.check("toolu_1")).verdict == "outside"


async def test_the_scripts_narrowing_is_read_from_the_step_and_only_narrows(
    outward: Outward, api: FakeApi, sessions: tuple[Sessions, str]
) -> None:
    narrowing = {"rules": [{**SEND_RULE, "args": [{"path": "/printer_id", "in": [3, 4]}]}]}
    c = await child(
        outward,
        api,
        sessions,
        grant({**SEND_RULE, "args": [{"path": "/printer_id", "in": [3]}]}),
        narrowing,
    )
    assert (await c.check("toolu_1")).verdict == "inside"
    # Wider than the grant: printer 4 is in the narrowing, not in the grant.
    assert (await c.check("toolu_2", {**SEND, "printer_id": 4})).verdict == "outside"
    narrower = await child(
        outward,
        api,
        sessions,
        grant(SEND_RULE),
        {"rules": [{**SEND_RULE, "args": [{"path": "/printer_id", "in": [5]}]}]},
    )
    assert (await narrower.check("toolu_9")).verdict == "outside"
    # A request id another run's grant let through is that run's, never this one's.
    wide_open = await child(outward, api, sessions, grant(SEND_RULE))
    assert (await wide_open.check("toolu_1")).verdict == "outside"
    assert await outward.store.grant_uses(wide_open.run_id) == []


async def test_a_check_names_only_its_own_session_and_a_step_of_the_run(
    outward: Outward, api: FakeApi, sessions: tuple[Sessions, str]
) -> None:
    c = await child(outward, api, sessions, grant(SEND_RULE))
    other = "33333333-3333-4333-8333-333333333333"
    # Another step of the run, or a step that is not this session's.
    assert (await c.check("toolu_1", flow_call_id="nope")).verdict == "outside"
    # A session naming another session, or another session's request id.
    assert (await c.check("toolu_2", session_id=other)).verdict == "outside"
    assert (
        await c.check("toolu_3", request_id=f"durable:{other}:swr:toolu_3")
    ).verdict == "outside"
    # A check with no session from a session's workflow is no flow-own call.
    assert (await c.check("toolu_4", session_id=None, flow_call_id=None)).verdict == "outside"
    assert await outward.store.grant_uses(c.run_id) == []


async def test_a_session_cannot_borrow_another_runs_grant(
    outward: Outward, api: FakeApi, sessions: tuple[Sessions, str]
) -> None:
    """The run is the one whose step talks to this session, never the payload's."""
    closed = await child(outward, api, sessions, grant(SEND_RULE, extends=False))
    wide = await child(outward, api, sessions, grant(SEND_RULE))
    # This session's turn of `closed`, claiming `wide`'s run id.
    assert (await closed.check("toolu_1", run_id=wide.run_id)).verdict == "outside"
    # `wide`'s own step, claiming `closed`'s run id.
    assert (await wide.check("toolu_2", run_id=closed.run_id)).verdict == "outside"
    assert await outward.store.grant_uses(wide.run_id) == []


async def test_another_session_cannot_use_a_step_that_talks_to_this_one(
    outward: Outward, api: FakeApi, sessions: tuple[Sessions, str]
) -> None:
    c = await child(outward, api, sessions, grant(SEND_RULE))
    other = "33333333-3333-4333-8333-333333333333"
    c.env.info = dataclasses.replace(c.env.info, workflow_id=f"session-{other}")
    verdict = await c.check("toolu_1", session_id=other, request_id=f"durable:{other}:swr:toolu_1")
    assert verdict.verdict == "outside"
    assert await outward.store.grant_uses(c.run_id) == []


async def test_a_step_with_no_policy_admits_nothing(
    outward: Outward, api: FakeApi, sessions: tuple[Sessions, str]
) -> None:
    """Fail closed: a step whose narrowing cannot be read is no flow turn."""
    c = await child(outward, api, sessions, grant(SEND_RULE))
    with outward.store._pool.connection() as conn:
        conn.execute(
            "UPDATE workflow_runs SET steps ="
            " (SELECT jsonb_agg(s - 'policy') FROM jsonb_array_elements(steps) s)"
            " WHERE id = %s",
            (c.run_id,),
        )
    assert (await c.check("toolu_1")).verdict == "outside"


async def test_a_finished_turn_is_no_flow_turn(
    outward: Outward, api: FakeApi, sessions: tuple[Sessions, str]
) -> None:
    """A later turn of the same session (the person's, after the step ended) is not covered."""
    served, _ = sessions
    c = await child(outward, api, sessions, grant(SEND_RULE))
    assert served.hold is not None
    served.hold.set()
    await outward.finished(c.run_id)
    assert (await c.check("toolu_1")).verdict == "outside"
    assert await outward.store.grant_uses(c.run_id) == []
