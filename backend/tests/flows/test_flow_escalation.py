"""A flow-own call outside the run's grant, with the starter session as its parent (#2482,
spec 2026-10-11 §4.1-§4.4, §4.6; plan Tasks B3, B4, B6): its hop and timer in the
workflow, a parent's narrowing matched by the grant and run with the narrowed input,
bound to that input's hash, and a child's verdict saying where its call goes."""

import asyncio
import dataclasses
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.approval_hash import input_hash
from scadbuddy.flows.grants import GrantSpec
from scadbuddy.flows.manifest import use_manifest
from scadbuddy.flows.models import Decision, Run
from scadbuddy.flows.operations import FLOW_ESCALATE, flow_kinds, request_id
from scadbuddy.workflows.flow_grants import FlowGrants
from scadbuddy.workflows.flow_models import DecisionAsk, DecisionRead
from tests.flows import test_agent_host_functions as host
from tests.flows.fake_api import PLAN, FakeApi, Outward
from tests.flows.flows_support import HASH_KEY, Keys, script
from tests.flows.test_agent_host_functions import DURABLE, Sessions
from tests.flows.test_grant_children import Child
from tests.flows.test_grant_gate import api_client_of

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

sessions = host.sessions

STARTER = {"kind": "agent", "principal": "token:p1", "session": "s1"}
PRINT = f"o = await queue_print({{'output_id': 'o1'}}, {PLAN!r})"


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
    api.quantity = 1
    run_id = await outward.run(
        script(PRINT, "return [o['status'], o['narrowed_by']]"),
        grant=grant({"fn": "queue_print", "printers": [3], "max_prints": 2}),
        started_by=STARTER,
    )
    # Printer 4 is not in the grant: it goes to the starter session.
    api.printer_id = 4
    parked = await waiting(outward, run_id, "parent")
    call_id = parked.waiting_on[0].call_id
    narrowed = {**PLAN, "printer_id": 3, "copies": 2}
    answer = await escalations(run_id, call_id, "narrow", input=narrowed)
    assert answer["outcome"] == "approved"
    run = await outward.finished(run_id)
    assert run.result == "result: ['succeeded', 'session:s1']", run.result
    [(body, _)] = api.to("/api/v1/print/outputs/o1/run")
    assert (body["printer_id"], body["copies"]) == (3, 2)
    decision = await outward.store.get_decision(request_id(run, call_id))
    assert decision is not None and decision.responder.startswith("grant:")
    sent = {"source": {"output_id": "o1"}, "request": narrowed}
    assert decision.input_hash == input_hash(HASH_KEY, "queue_print", sent)
    assert decision.response == {"args": sent, "narrowed_by": "session:s1"}
    assert [h.action for h in decision.hops] == ["parked", "narrow"]
    [use] = await outward.store.grant_uses(run_id)
    assert use.prints == 2


async def test_a_narrow_the_grant_does_not_admit_goes_to_the_person(
    outward: Outward, api: FakeApi, escalations: Escalations
) -> None:
    run_id = await outward.run(
        script(PRINT, "return o['status']"),
        grant=grant({"fn": "queue_print", "printers": [3]}),
        started_by=STARTER,
    )
    api.printer_id = 4
    parked = await waiting(outward, run_id, "parent")
    call_id = parked.waiting_on[0].call_id
    answer = await escalations(run_id, call_id, "narrow", input={**PLAN, "printer_id": 5})
    assert answer["outcome"] == "with_person"
    moved = await waiting(outward, run_id, "person")
    [entry] = moved.waiting_on
    assert entry.proposed and entry.hops[-1].reason == "printer not in the grant"
    assert api.sent == []
    assert await outward.store.grant_uses(run_id) == []


async def test_a_parent_cannot_narrow_into_another_source(
    outward: Outward, api: FakeApi, escalations: Escalations
) -> None:
    """The narrowed input replaces the request; the source is the parked call's own."""
    api.printer_id = 4
    run_id = await outward.run(
        script(PRINT, "return o['status']"),
        grant=grant({"fn": "queue_print", "printers": [3]}),
        started_by=STARTER,
    )
    parked = await waiting(outward, run_id, "parent")
    call_id = parked.waiting_on[0].call_id
    with pytest.raises(ApiError):
        await escalations(
            run_id, call_id, "narrow", input={"source": {"output_id": "o2"}, "request": PLAN}
        )


class Tools:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def all(self) -> list[Any]:
        @activity.defn(name="send_to_bambuddy")
        async def send_to_bambuddy(args: dict[str, Any]) -> str:
            self.calls.append(args)
            return "sent"

        return [send_to_bambuddy]


@pytest.fixture(autouse=True)
def manifest() -> Iterator[None]:
    use_manifest(
        {"sessions_start": "write", "sessions_send": "write", "send_to_bambuddy": "outward"}
    )
    yield
    use_manifest({})


@pytest.fixture
async def tools(outward: Outward) -> AsyncIterator[tuple[Tools, str]]:
    served = Tools()
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    async with Worker(outward.client, task_queue=queue, activities=served.all()):
        yield served, queue


async def test_a_narrowed_tool_call_runs_its_narrowed_input_and_says_so(
    outward: Outward, escalations: Escalations, tools: tuple[Tools, str]
) -> None:
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
    narrowed = {"output_id": "o1", "printer_id": 3}
    assert (await escalations(run_id, call_id, "narrow", input=narrowed))["outcome"] == "approved"
    run = await outward.finished(run_id)
    assert served.calls == [narrowed]
    assert run.result == "result: 'Ran narrowed by session:s1.\\nsent'", run.result
    decision = await outward.store.get_decision(request_id(run, call_id))
    assert decision is not None
    assert decision.input_hash == input_hash(HASH_KEY, "send_to_bambuddy", narrowed)


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
