"""A flow-own call outside the run's grant, with the starter session as its parent (#2482,
spec 2026-10-11 §4.1-§4.4, §4.6; plan Tasks B3, B4, B6): its hop and timer in the
workflow, a parent's narrowing matched by the grant and run with the narrowed input,
bound to that input's hash, and a child's verdict saying where its call goes."""

import asyncio
import dataclasses
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.approval_hash import input_hash
from scadbuddy.flows.grants import GrantSpec
from scadbuddy.flows.models import Decision, Run
from scadbuddy.flows.operations import FLOW_ESCALATE, flow_kinds, request_id
from scadbuddy.workflows.flow_grants import FlowGrants
from scadbuddy.workflows.flow_models import DecisionAsk, DecisionRead
from tests.flows import test_agent_host_functions as host
from tests.flows.fake_api import PLAN, FakeApi, Outward
from tests.flows.flows_support import HASH_KEY, AgentTiers, Keys, parent_session, script
from tests.flows.test_agent_host_functions import DURABLE, Sessions
from tests.flows.test_grant_children import Child
from tests.flows.test_grant_gate import api_client_of

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

sessions = host.sessions

PARENT_SESSION = "5e551011-0000-4000-8000-000000000001"
STARTER = {
    "kind": "agent",
    "principal": "token:0f0e0d0c-0b0a-4908-8706-050403020100",
    "session": PARENT_SESSION,
}
ACTOR = f"session:{PARENT_SESSION}"
PRINT = f"o = await queue_print({{'output_id': 'o1'}}, {PLAN!r})"
#: Five copies named: outside a grant of two prints, and narrowable down to it.
FIVE = {**PLAN, "copies": 5}
PRINT_FIVE = f"o = await queue_print({{'output_id': 'o1'}}, {FIVE!r})"


def grant(*rules: dict[str, Any], **fields: Any) -> GrantSpec:
    return GrantSpec.model_validate(
        {"expires_at": datetime.now(UTC) + timedelta(hours=1), "rules": list(rules), **fields}
    )


async def waiting(outward: Outward, run_id: str, hop: str, timeout: float = 30) -> Run:
    return await outward.row(
        run_id, lambda r: [w.hop for w in r.waiting_on] == [hop], timeout=timeout
    )


class Escalations:
    """The `flow_escalate` operation, as the backend's route runs it for the parent."""

    def __init__(self, outward: Outward, api: FakeApi) -> None:
        with outward.store._pool.connection() as conn:
            parent_session(conn, PARENT_SESSION, STARTER["principal"])
        kinds = flow_kinds(outward.store, Keys(), HASH_KEY, api=lambda: api_client_of(api))
        self.kind = next(k for k in kinds if k.name == FLOW_ESCALATE)
        self.env = ActivityEnvironment(client=outward.client)

    async def __call__(self, run_id: str, call_id: str, action: str, **body: Any) -> Any:
        request = {
            "run_id": run_id,
            "call_id": call_id,
            "action": action,
            "session": STARTER["session"],
            "principal": STARTER["principal"],
            **body,
        }
        checked = await self.env.run(self.kind.check, request)
        return await self.env.run(self.kind.run, request, checked)


@pytest.fixture
def escalations(outward: Outward, api: FakeApi) -> Escalations:
    return Escalations(outward, api)


async def test_a_parent_that_never_answers_passes_up_then_the_person_timer_denies(
    outward: Outward, api: FakeApi
) -> None:
    run_id = await outward.run(
        script("try:", f"    {PRINT}", "except Exception as e:", "    return str(e)"),
        approval_timeout_s=3,
        started_by=STARTER,
    )
    parked = await waiting(outward, run_id, "parent")
    [entry] = parked.waiting_on
    assert entry.hop_until is not None
    assert entry.hop_until - entry.since == timedelta(seconds=3)
    # Passed up by the timer, never denied by it.
    moved = await waiting(outward, run_id, "person", timeout=15)
    assert [(h.action, h.actor) for h in moved.waiting_on[0].hops] == [
        ("parked", "flow"),
        ("timeout", "system"),
    ]
    run = await outward.finished(run_id)
    assert run.result is not None and "timed out" in run.result
    assert api.sent == []
    # The person's timer ran from the end of the parent hop: one timeout each.
    [step] = run.steps
    assert step.ended_at is not None
    assert step.ended_at - step.started_at >= timedelta(seconds=6)


async def test_with_no_run_timeout_the_parent_hop_still_ends_and_nothing_denies(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def expiry() -> int:
        return 2

    monkeypatch.setattr(outward.store, "approval_expiry_s", expiry)
    run_id = await outward.run(script(PRINT, "return o['status']"), started_by=STARTER)
    await waiting(outward, run_id, "parent")
    await waiting(outward, run_id, "person", timeout=15)
    await asyncio.sleep(3)
    still = await outward.store.get_run(run_id)
    assert still is not None and still.status == "waiting"
    await outward.decide(run_id, approved=True)
    assert (await outward.finished(run_id)).result == "result: 'succeeded'"


async def test_a_parent_narrows_a_print_into_the_grant_and_it_runs_narrowed(
    outward: Outward, api: FakeApi, escalations: Escalations
) -> None:
    api.printer_id = 3
    run_id = await outward.run(
        script(PRINT_FIVE, "return [o['status'], o['narrowed_by']]"),
        grant=grant({"fn": "queue_print", "max_prints": 2}),
        started_by=STARTER,
    )
    # Five prints is past the grant's two: it goes to the starter session.
    parked = await waiting(outward, run_id, "parent")
    call_id = parked.waiting_on[0].call_id
    narrowed = {**PLAN, "copies": 2}
    answer = await escalations(run_id, call_id, "narrow", input=narrowed)
    assert answer["outcome"] == "approved"
    run = await outward.finished(run_id)
    assert run.result == f"result: ['succeeded', {ACTOR!r}]", run.result
    [(body, _)] = api.to("/api/v1/print/outputs/o1/run")
    assert (body["printer_id"], body["copies"]) == (3, 2)
    decision = await outward.store.get_decision(request_id(run, call_id))
    assert decision is not None and decision.responder.startswith("grant:")
    # As checked: the configured printer pinned in, and the hash over that.
    sent = {"source": {"output_id": "o1"}, "request": {**narrowed, "printer_id": 3}}
    assert decision.input_hash == input_hash(HASH_KEY, "queue_print", sent)
    assert decision.response == {"args": sent, "narrowed_by": ACTOR}
    assert [h.action for h in decision.hops] == ["parked", "narrow"]
    [use] = await outward.store.grant_uses(run_id)
    assert use.prints == 2


async def test_a_narrow_the_grant_does_not_admit_goes_to_the_person(
    outward: Outward, api: FakeApi, escalations: Escalations
) -> None:
    run_id = await outward.run(
        script(PRINT_FIVE, "return o['status']"),
        grant=grant({"fn": "queue_print", "max_prints": 2}),
        started_by=STARTER,
    )
    parked = await waiting(outward, run_id, "parent")
    call_id = parked.waiting_on[0].call_id
    answer = await escalations(run_id, call_id, "narrow", input={**PLAN, "copies": 3})
    assert answer["outcome"] == "with_person"
    moved = await waiting(outward, run_id, "person")
    [entry] = moved.waiting_on
    assert entry.proposed and entry.hops[-1].reason == "print limit reached"
    assert api.sent == []
    assert await outward.store.grant_uses(run_id) == []


@pytest.mark.parametrize(
    "given",
    [
        {"source": {"output_id": "o2"}, "request": FIVE},
        {**FIVE, "printer_id": 3},
        {**PLAN, "copies": 6},
        {**PLAN, "copies": 2, "all_plates": True},
        PLAN,
    ],
)
async def test_a_parent_cannot_widen_or_substitute_a_print(
    outward: Outward, api: FakeApi, escalations: Escalations, given: dict[str, Any]
) -> None:
    """A narrowing keeps the call: its source, its printer, its every choice; it may only
    lower what the call named. Refused before the grant is asked, so nothing is used."""
    api.printer_id = 3
    run_id = await outward.run(
        script(PRINT_FIVE, "return o['status']"),
        grant=grant({"fn": "queue_print", "max_prints": 2}),
        started_by=STARTER,
    )
    parked = await waiting(outward, run_id, "parent")
    call_id = parked.waiting_on[0].call_id
    with pytest.raises(ApiError) as err:
        await escalations(run_id, call_id, "narrow", input=given)
    assert err.value.status == 422
    assert await outward.store.grant_uses(run_id) == []
    still = await outward.store.get_run(run_id)
    assert still is not None and [w.hop for w in still.waiting_on] == ["parent"]
    assert api.sent == []


class Tools:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def all(self) -> list[Any]:
        @activity.defn(name="send_to_bambuddy")
        async def send_to_bambuddy(args: dict[str, Any]) -> str:
            self.calls.append(args)
            return "sent"

        return [send_to_bambuddy]


@pytest.fixture
async def tools(outward: Outward) -> AsyncIterator[tuple[Tools, str]]:
    served = Tools()
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    agent = AgentTiers({"send_to_bambuddy": "outward"})
    async with Worker(
        outward.client, task_queue=queue, activities=[*served.all(), agent.activity()]
    ):
        yield served, queue


async def test_a_tool_call_is_never_narrowed(
    outward: Outward, escalations: Escalations, tools: tuple[Tools, str]
) -> None:
    """An agent tool's arguments mean nothing ScadBuddy could judge smaller: its parent
    denies it or passes it up."""
    served, queue = tools
    rule = {"fn": "tool", "tool": "send_to_bambuddy", "args": [{"path": "/printer_id", "in": [3]}]}
    run_id = await outward.run(
        script("return await tool('send_to_bambuddy', {'output_id': 'o1', 'printer_id': 4})"),
        tools_queue=queue,
        grant=grant(rule),
        started_by=STARTER,
    )
    parked = await waiting(outward, run_id, "parent")
    call_id = parked.waiting_on[0].call_id
    with pytest.raises(ApiError) as err:
        await escalations(run_id, call_id, "narrow", input={"output_id": "o1", "printer_id": 3})
    assert err.value.status == 422
    assert served.calls == []
    assert await outward.store.grant_uses(run_id) == []


async def test_a_narrowed_decision_whose_input_does_not_match_its_hash_runs_nothing(
    outward: Outward, api: FakeApi
) -> None:
    """`flow_decision` hands the gated body a narrowed input only when it hashes to the
    decision's own hash; otherwise the body refuses rather than run anything."""
    run_id = await outward.run(script(PRINT, "return o['status']"), started_by=STARTER)
    parked = await waiting(outward, run_id, "parent")
    call_id = parked.waiting_on[0].call_id
    rid = request_id(parked, call_id)
    good = {"source": {"output_id": "o1"}, "request": {**PLAN, "copies": 1}}
    await outward.store.record_decision(
        Decision(
            request_id=rid,
            run_id=run_id,
            workflow_run_id=parked.workflow_run_id,
            call_id=call_id,
            kind="approval",
            outcome="approved",
            response={"args": {**good, "request": {**PLAN, "copies": 9}}, "narrowed_by": "x"},
            responder="browser",
            input_hash=input_hash(HASH_KEY, "queue_print", good),
        )
    )
    env = ActivityEnvironment(client=outward.client)
    env.info = dataclasses.replace(
        env.info, workflow_id=parked.workflow_id, workflow_run_id=parked.workflow_run_id
    )
    grants = FlowGrants(outward.store, None, HASH_KEY)
    ask = DecisionAsk(call_id=call_id, fn="queue_print")
    read: DecisionRead = await env.run(grants.decision, ask)
    assert (read.narrowed, read.args) == (True, None)
    # And for another run's workflow, nothing at all.
    other = ActivityEnvironment(client=outward.client)
    other.info = dataclasses.replace(
        other.info, workflow_id=f"flow-{uuid.uuid4()}", workflow_run_id="x"
    )
    assert (await other.run(grants.decision, ask)).narrowed is False


async def test_a_starter_session_that_is_also_the_child_has_no_parent_hop(
    outward: Outward, api: FakeApi, sessions: tuple[Sessions, str]
) -> None:
    """A script that `agent`s its way into... its own starter session: its calls would
    escalate to itself mid-turn, so they go to the person (spec §4.4)."""
    served, queue = sessions
    served.hold = asyncio.Event()
    starter = {**STARTER, "session": DURABLE}
    for started_by, parent in ((starter, False), (STARTER, True)):
        run_id = await outward.run(
            script("return await agent('go', [], None, on_outside='deny')"),
            tools_queue=queue,
            grant=grant({"fn": "queue_print"}, sessions=True),
            started_by=started_by,
            approval_timeout_s=30,
        )
        run = await outward.row(
            run_id, lambda r: [(s.session_id, s.status) for s in r.steps] == [(DURABLE, "running")]
        )
        verdict = await Child(outward, api, run_id, run.steps[0].call_id).check(
            f"u-{time.monotonic_ns()}"
        )
        assert (verdict.verdict, verdict.parent_hop) == ("outside", parent)
        assert verdict.on_outside == "deny"
        assert verdict.hop_s == (30 if parent else 0)
        await outward.client.get_workflow_handle(f"flow-{run_id}").terminate()
