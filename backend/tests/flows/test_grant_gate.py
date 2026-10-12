"""The flow-own gate checks the run's grant (#2481, plan 2026-10-11-flow-grants-and-git.md
Task A3): a call inside it runs with no entry for anyone to decide, approved by the
grant with the hash of the call as it runs; a call outside it parks as before."""

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import httpx
import pytest
from pydantic import ValidationError
from temporal_agent_harness.harness.agent_client import AgentClient
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.approval_hash import input_hash
from scadbuddy.flows.grants import GrantSpec
from scadbuddy.flows.manifest import use_manifest
from scadbuddy.flows.models import Decision
from scadbuddy.flows.operations import FLOW_DECIDE, STALE_ENTRY, flow_kinds, request_id
from scadbuddy.workflows.flow_grants import FlowGrants
from scadbuddy.workflows.flow_models import GrantApprove, GrantCheck, GrantVerdict
from tests.flows.fake_api import PLAN, FakeApi, Outward
from tests.flows.flows_support import HASH_KEY, Keys, script

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]


def grant(*rules: dict[str, Any]) -> GrantSpec:
    return GrantSpec.model_validate(
        {"expires_at": datetime.now(UTC) + timedelta(hours=1), "rules": list(rules)}
    )


PRINT = f"o = await queue_print({{'output_id': 'o1'}}, {PLAN!r})"


async def test_a_print_inside_the_grant_runs_unasked_with_its_defaults_pinned(
    outward: Outward, api: FakeApi
) -> None:
    api.printer_id = 3
    api.quantity = 2
    run_id = await outward.run(
        script(PRINT, "return o['status']"),
        grant=grant({"fn": "queue_print", "printers": [3], "models": ["box"], "max_prints": 2}),
    )
    run = await outward.finished(run_id)
    assert run.result == "result: 'succeeded'", run.result
    [(body, _)] = api.to("/api/v1/print/outputs/o1/run")
    # The configured printer and the remembered quantity, as checked, are what is sent.
    assert (body["printer_id"], body["copies"]) == (3, 2)
    [step] = run.steps
    assert (step.fn, step.status) == ("queue_print", "succeeded") and step.grant_id
    # Never parked: nobody was asked.
    with outward.store._pool.connection() as conn:
        row = conn.execute(
            "SELECT waiting_done FROM workflow_runs WHERE id = %s", (run_id,)
        ).fetchone()
    assert row is not None and row["waiting_done"] == [] and run.waiting_on == []
    decision = await outward.store.get_decision(request_id(run, step.call_id))
    assert decision is not None
    assert (decision.outcome, decision.responder) == ("approved", f"grant:{step.grant_id}")
    args = {"source": {"output_id": "o1"}, "request": {**PLAN, "printer_id": 3, "copies": 2}}
    assert decision.input_hash == input_hash(HASH_KEY, "queue_print", args)
    [use] = await outward.store.grant_uses(run_id)
    assert (use.fn, use.prints, use.request_id) == ("queue_print", 2, decision.request_id)


async def test_a_print_outside_the_grant_parks_as_before(outward: Outward, api: FakeApi) -> None:
    api.printer_id = 4
    run_id = await outward.run(
        script(PRINT, "return o['status']"), grant=grant({"fn": "queue_print", "printers": [3]})
    )
    await outward.approval(run_id)
    parked = await outward.store.get_run(run_id)
    assert parked is not None and [w.fn for w in parked.waiting_on] == ["queue_print"]
    assert api.sent == []
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: 'succeeded'"
    assert run.steps[0].grant_id is None
    assert await outward.store.grant_uses(run_id) == []


async def test_max_calls_lets_one_through_and_parks_the_next(
    outward: Outward, api: FakeApi
) -> None:
    run_id = await outward.run(
        script(
            "a = await arrange({'objects': [{'output_id': 'o1'}]})",
            "b = await arrange({'objects': [{'output_id': 'o1'}]})",
            "return [a['status'], b['status']]",
        ),
        grant=grant({"fn": "arrange", "max_calls": 1}),
    )
    await outward.approval(run_id)
    parked = await outward.store.get_run(run_id)
    assert parked is not None
    assert [s.grant_id is not None for s in parked.steps] == [True, False]
    assert len(api.to("/api/v1/outputs/arrange")) == 1
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: ['done', 'done']"


async def test_a_person_cannot_decide_a_call_the_grant_approved(
    outward: Outward, api: FakeApi
) -> None:
    run_id = await outward.run(
        script(PRINT, "await wait_for_human('Done?', 600)"),
        grant=grant({"fn": "queue_print"}),
    )
    run = await outward.row(run_id, lambda r: any(w.kind == "answer" for w in r.waiting_on))
    call_id = run.steps[0].call_id
    decide = next(k for k in flow_kinds(outward.store, Keys(), HASH_KEY) if k.name == FLOW_DECIDE)
    env = ActivityEnvironment(client=outward.client)
    with pytest.raises(ApiError) as err:
        await env.run(decide.check, {"run_id": run_id, "call_id": call_id, "approved": False})
    assert err.value.type == STALE_ENTRY


async def test_a_call_whose_grant_lapses_before_its_approval_is_listed_for_a_person(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check matched, then the grant was revoked before the approval: the approval
    refuses (record_grant_decision), and the call waits for a person instead."""

    async def lapsed(decision: Any) -> bool:
        return False

    monkeypatch.setattr(outward.store, "record_grant_decision", lapsed)
    run_id = await outward.run(
        script("a = await arrange({'objects': [{'output_id': 'o1'}]})", "return a['status']"),
        grant=grant({"fn": "arrange"}),
    )
    run = await outward.row(run_id, lambda r: [w.fn for w in r.waiting_on] == ["arrange"])
    assert run.steps[0].grant_id is None
    assert api.to("/api/v1/outputs/arrange") == []
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: 'done'"
    assert run.waiting_on == [] and run.steps[0].grant_id is None


async def test_a_lookup_that_fails_asks(outward: Outward, api: FakeApi) -> None:
    api.outputs_fail = True
    run_id = await outward.run(script(PRINT, "return 1"), grant=grant({"fn": "queue_print"}))
    await outward.approval(run_id)
    assert api.sent == []
    assert await outward.store.grant_uses(run_id) == []


async def test_a_revoked_grant_asks(outward: Outward, api: FakeApi) -> None:
    async def revoke(run_id: str) -> None:
        await outward.store.revoke_grant(run_id, revoked_by="browser")

    run_id = await outward.run(
        script(PRINT, "return 1"), grant=grant({"fn": "queue_print"}), before=revoke
    )
    await outward.approval(run_id)
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


@pytest.fixture(autouse=True)
def manifest() -> Iterator[None]:
    use_manifest({"send_to_bambuddy": "outward"})
    yield
    use_manifest({})


@pytest.fixture
async def tools(outward: Outward) -> AsyncIterator[tuple[Tools, str]]:
    served = Tools()
    queue = f"agent-tools-{uuid.uuid4().hex[:8]}"
    async with Worker(outward.client, task_queue=queue, activities=served.all()):
        yield served, queue


async def test_an_outward_tool_inside_the_grant_is_approved_for_its_hash(
    outward: Outward, tools: tuple[Tools, str]
) -> None:
    served, queue = tools
    args = {"output_id": "o1", "printer_id": 3}
    run_id = await outward.run(
        script(f"return await tool('send_to_bambuddy', {args!r})"),
        tools_queue=queue,
        grant=grant(
            {
                "fn": "tool",
                "tool": "send_to_bambuddy",
                "args": [{"path": "/printer_id", "in": [3, 4]}],
            }
        ),
    )
    run = await outward.finished(run_id)
    assert run.result == "result: 'sent'", run.result
    assert served.calls == [args]
    [step] = run.steps
    decision = await outward.store.get_decision(request_id(run, step.call_id))
    assert decision is not None and decision.responder == f"grant:{step.grant_id}"
    # What the agent hashes again from the call it runs (#2318), so it runs it.
    assert decision.input_hash == input_hash(HASH_KEY, "send_to_bambuddy", args)
    status = await AgentClient(outward.client, f"flow-{run_id}").get_status()
    assert status.pending_approvals == []


class Parked(NamedTuple):
    run_id: str
    call_id: str
    request_id: str
    env: ActivityEnvironment
    grants: FlowGrants


SEND = {"output_id": "o1"}


async def parked_send(
    outward: Outward, api: FakeApi, queue: str, *rules: dict[str, Any], calls: int = 1
) -> list[Parked]:
    """`calls` sends parked in a run whose grant was requested (so they asked), then the
    grant approved: activities run as the run's own workflow would run them."""
    spec = grant(*rules) if rules else grant({"fn": "tool", "tool": "send_to_bambuddy"})

    async def insert(run_id: str) -> None:
        await outward.store.insert_grant(
            run_id, spec, status="requested", requested_by={"kind": "agent"}
        )

    sends = ", ".join(f"tool('send_to_bambuddy', {SEND!r})" for _ in range(calls))
    run_id = await outward.run(
        script(f"return await asyncio.gather({sends})"), tools_queue=queue, before=insert
    )
    run = await outward.row(run_id, lambda r: len(r.waiting_on) == calls)
    client = AgentClient(outward.client, run.workflow_id)
    for _ in range(100):
        if len(await client.get_pending_approvals()) == calls:
            break
        await asyncio.sleep(0.1)
    await outward.store.approve_grant(run_id, None, approved_by="browser")
    env = ActivityEnvironment(client=outward.client)
    env.info = dataclasses.replace(
        env.info, workflow_id=run.workflow_id, workflow_run_id=run.workflow_run_id
    )
    grants = FlowGrants(outward.store, api_client_of(api), HASH_KEY)
    return [
        Parked(run_id, w.call_id, request_id(run, w.call_id), env, grants) for w in run.waiting_on
    ]


async def check(p: Parked, args: dict[str, Any] = SEND, rid: str | None = None) -> GrantVerdict:
    return await p.env.run(
        p.grants.check,
        GrantCheck(
            run_id=p.run_id,
            request_id=rid or p.request_id,
            fn="tool",
            tool="send_to_bambuddy",
            args=args,
        ),
    )


async def approve(p: Parked) -> str:
    return await p.env.run(p.grants.approve, GrantApprove(call_id=p.call_id))


async def test_approve_lets_through_only_what_the_check_matched(
    outward: Outward, api: FakeApi, tools: tuple[Tools, str]
) -> None:
    served, queue = tools
    [p] = await parked_send(outward, api, queue)
    assert (await check(p)).verdict == "inside"
    assert await approve(p) == "approved"
    decision = await outward.store.get_decision(p.request_id)
    assert decision is not None and decision.responder.startswith("grant:")
    # The hash of the call as it is parked, which the agent checks again (#2318).
    assert decision.input_hash == input_hash(HASH_KEY, "send_to_bambuddy", SEND)
    await outward.row(p.run_id, lambda r: r.status == "succeeded")
    assert served.calls == [SEND]


async def test_a_grant_revoked_between_check_and_approve_lets_nothing_through(
    outward: Outward, api: FakeApi, tools: tuple[Tools, str]
) -> None:
    served, queue = tools
    [p] = await parked_send(outward, api, queue)
    assert (await check(p)).verdict == "inside"
    await outward.store.revoke_grant(p.run_id, revoked_by="browser")
    assert await approve(p) == "ask"
    assert await outward.store.get_decision(p.request_id) is None
    pending = await AgentClient(outward.client, f"flow-{p.run_id}").get_pending_approvals()
    assert [c.tool_id for c in pending] == [p.call_id]
    assert served.calls == []


async def test_a_grant_expired_between_check_and_approve_lets_nothing_through(
    outward: Outward, api: FakeApi, tools: tuple[Tools, str]
) -> None:
    _, queue = tools
    [p] = await parked_send(outward, api, queue)
    assert (await check(p)).verdict == "inside"
    with outward.store._pool.connection() as conn:
        conn.execute(
            "UPDATE flow_grants SET expires_at = now() - interval '1 second' WHERE run_id = %s",
            (p.run_id,),
        )
    assert await approve(p) == "ask"
    assert await outward.store.get_decision(p.request_id) is None


async def test_approve_trusts_no_verdict_it_did_not_record(
    outward: Outward, api: FakeApi, tools: tuple[Tools, str]
) -> None:
    """Approve names only the call: nothing checked for it (or checked for other input)
    approves it, whatever the payload says."""
    served, queue = tools
    [p, q] = await parked_send(outward, api, queue, calls=2)
    # Never checked.
    assert await approve(p) == "ask"
    # Checked for other input than it is parked with: the hash does not match.
    assert (await check(q, {"output_id": "o2"})).verdict == "inside"
    assert await approve(q) == "ask"
    for parked in (p, q):
        assert await outward.store.get_decision(parked.request_id) is None
    # A payload carrying a grant id or a hash is refused outright.
    with pytest.raises(ValidationError):
        GrantApprove.model_validate({"call_id": p.call_id, "grant_id": "g", "input_hash": "h"})
    assert served.calls == []


async def test_a_check_for_another_runs_call_is_outside(
    outward: Outward, api: FakeApi, tools: tuple[Tools, str]
) -> None:
    _, queue = tools
    [p] = await parked_send(outward, api, queue)
    other = f"flow:{uuid.uuid4()}:wr:{p.call_id}"
    assert (await check(p, rid=other)).verdict == "outside"
    assert (await check(p, rid=p.request_id.replace(p.call_id, "x"))).verdict == "inside"
    wrong_run = await p.env.run(
        p.grants.check,
        GrantCheck(run_id=str(uuid.uuid4()), request_id=p.request_id, fn="tool", tool="x"),
    )
    assert wrong_run.verdict == "outside"


async def test_two_checks_racing_for_the_last_call_let_one_through(
    outward: Outward, api: FakeApi, tools: tuple[Tools, str]
) -> None:
    _, queue = tools
    [p, q] = await parked_send(
        outward, api, queue, {"fn": "tool", "tool": "send_to_bambuddy", "max_calls": 1}, calls=2
    )
    verdicts = await asyncio.gather(check(p), check(q))
    assert sorted(v.verdict for v in verdicts) == ["inside", "outside"]
    assert len(await outward.store.grant_uses(p.run_id)) == 1


def api_client_of(api: FakeApi) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://api")


async def test_approving_a_requested_grant_releases_the_calls_waiting_inside_it(
    outward: Outward, api: FakeApi, tools: tuple[Tools, str]
) -> None:
    """The user's ruling: an agent-started run starts at once, and its outward calls wait
    until the grant, or each call, is approved."""
    served, queue = tools
    spec = grant({"fn": "tool", "tool": "send_to_bambuddy"}, {"fn": "queue_print"})

    async def insert(run_id: str) -> None:
        await outward.store.insert_grant(
            run_id, spec, status="requested", requested_by={"kind": "agent"}
        )

    run_id = await outward.run(
        script(
            "a, o = await asyncio.gather(",
            "    tool('send_to_bambuddy', {'output_id': 'o1'}),",
            # Names no printer: once parked it cannot be pinned, so it still asks.
            f"    queue_print({{'output_id': 'o1'}}, {PLAN!r}),",
            ")",
            "return a",
        ),
        tools_queue=queue,
        before=insert,
    )
    parked = await outward.row(run_id, lambda r: len(r.waiting_on) == 2)
    client = AgentClient(outward.client, f"flow-{run_id}")
    for _ in range(100):
        if len(await client.get_pending_approvals()) == 2:
            break
        await asyncio.sleep(0.1)
    assert served.calls == []
    await outward.store.approve_grant(run_id, None, approved_by="browser")
    grants = FlowGrants(outward.store, api_client_of(api), HASH_KEY)
    released = await grants.release(outward.client, parked)
    [tool_call] = [w.call_id for w in parked.waiting_on if w.fn == "tool:send_to_bambuddy"]
    assert released == [tool_call]
    run = await outward.row(run_id, lambda r: [w.fn for w in r.waiting_on] == ["queue_print"])
    await outward.row(
        run_id, lambda r: any(s.call_id == tool_call and s.status == "succeeded" for s in r.steps)
    )
    assert served.calls == [{"output_id": "o1"}]
    decision = await outward.store.get_decision(request_id(run, released[0]))
    assert decision is not None and decision.responder.startswith("grant:")
    assert decision.input_hash == input_hash(HASH_KEY, "send_to_bambuddy", {"output_id": "o1"})
    # The print waits for its own approval, and releasing again releases nothing.
    assert await grants.release(outward.client, run) == []
    assert api.sent == []


def harness_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness Update fails as a transport error would (not ToolApprovalError), for
    the grant's approvals only: a person's still reach it."""
    real = AgentClient.approve_tool

    async def approve_tool(self: AgentClient, call_id: str, *args: Any, **kwargs: Any) -> Any:
        if str(kwargs.get("update_id", "")).startswith("grant-"):
            raise RuntimeError("the Update never arrived")
        return await real(self, call_id, *args, **kwargs)

    monkeypatch.setattr(AgentClient, "approve_tool", approve_tool)


async def person_can_decide(outward: Outward, p: Parked) -> None:
    decide = next(k for k in flow_kinds(outward.store, Keys(), HASH_KEY) if k.name == FLOW_DECIDE)
    env = ActivityEnvironment(client=outward.client)
    await env.run(decide.check, {"run_id": p.run_id, "call_id": p.call_id, "approved": True})


async def test_an_approval_the_harness_never_took_leaves_the_call_to_a_person(
    outward: Outward, api: FakeApi, tools: tuple[Tools, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #2645: a `grant:` row left behind by an Update that failed made the call,
    listed for a person once the retries were spent, answer stale to every decision."""
    _, queue = tools
    [p] = await parked_send(outward, api, queue)
    assert (await check(p)).verdict == "inside"
    harness_down(monkeypatch)
    with pytest.raises(RuntimeError):
        await approve(p)
    assert await outward.store.get_decision(p.request_id) is None
    await person_can_decide(outward, p)


async def test_a_release_the_harness_never_took_leaves_the_call_to_a_person(
    outward: Outward, api: FakeApi, tools: tuple[Tools, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, queue = tools
    [p] = await parked_send(outward, api, queue)
    harness_down(monkeypatch)
    run = await outward.store.get_run(p.run_id)
    assert run is not None
    assert await p.grants.release(outward.client, run) == []
    assert await outward.store.get_decision(p.request_id) is None
    await person_can_decide(outward, p)


async def test_withdraw_removes_only_the_grants_own_decision(
    outward: Outward, api: FakeApi, tools: tuple[Tools, str]
) -> None:
    """What the workflow runs before it lists a call whose approval never completed (the
    worker died after the row was written, or the retries ran out)."""
    _, queue = tools
    [p, q] = await parked_send(outward, api, queue, calls=2)
    verdict = await check(p)
    assert verdict.verdict == "inside" and verdict.grant_id is not None
    run = await outward.store.get_run(p.run_id)
    assert run is not None
    base = Decision(
        request_id=p.request_id,
        run_id=p.run_id,
        workflow_run_id=run.workflow_run_id,
        call_id=p.call_id,
        kind="approval",
        outcome="approved",
        responder=f"grant:{verdict.grant_id}",
        input_hash=verdict.input_hash,
    )
    assert await outward.store.record_grant_decision(base)
    await p.env.run(p.grants.withdraw, GrantApprove(call_id=p.call_id))
    assert await outward.store.get_decision(p.request_id) is None
    await person_can_decide(outward, p)
    # A person's decision is never the grant's to withdraw.
    person = base.model_copy(
        update={"request_id": q.request_id, "call_id": q.call_id, "responder": "browser"}
    )
    await outward.store.record_decision(person)
    await q.env.run(q.grants.withdraw, GrantApprove(call_id=q.call_id))
    assert await outward.store.get_decision(q.request_id) is not None


async def test_a_call_whose_approval_failed_after_its_row_is_decided_by_a_person(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #2645: the approval wrote its row and then failed for good (here at
    once; in production when its retries run out). The workflow withdraws the row
    before it lists the call, so the person it is listed for can decide it."""
    record = outward.store.record_grant_decision

    async def recorded_then_lost(decision: Any) -> bool:
        assert await record(decision)
        raise ApplicationError("the worker was lost", non_retryable=True)

    monkeypatch.setattr(outward.store, "record_grant_decision", recorded_then_lost)
    run_id = await outward.run(
        script("a = await arrange({'objects': [{'output_id': 'o1'}]})", "return a['status']"),
        grant=grant({"fn": "arrange"}),
    )
    run = await outward.row(run_id, lambda r: [w.fn for w in r.waiting_on] == ["arrange"])
    call_id = run.steps[0].call_id
    assert await outward.store.get_decision(request_id(run, call_id)) is None
    decide = next(k for k in flow_kinds(outward.store, Keys(), HASH_KEY) if k.name == FLOW_DECIDE)
    env = ActivityEnvironment(client=outward.client)
    await env.run(decide.check, {"run_id": run_id, "call_id": call_id, "approved": True})
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: 'done'"
