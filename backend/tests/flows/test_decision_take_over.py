"""A person's decision on a parked call that never landed does not block another one
(#7588): the rule of answers (#7575, `test_flow_operations.py`) for `flow_decide`. A call
runs only on the approval on record, with the input on record."""

import asyncio
import uuid
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporalio.client import WorkflowUpdateHandle
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.models import Decision, Run
from scadbuddy.flows.operations import (
    ALREADY_RESOLVED,
    FLOW_DECIDE,
    REPLACED,
    STALE_ENTRY,
    decision_update_id,
    request_id,
)
from tests.flows.fake_api import PLAN, FakeApi, Outward
from tests.flows.flows_support import Keys, script
from tests.flows.test_flow_escalation import PRINT_FIVE, STARTER, Escalations, grant
from tests.flows.test_flow_operations import _no_answer, kinds, operate, started
from tests.flows.test_flow_reset import point_after, reset

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

PATH = "/api/v1/print/outputs/o1/run"
PRINT = f"o = await queue_print({{'output_id': 'o1'}}, {PLAN!r})"
DENY: dict[str, Any] = {"approved": False, "reason": "not now"}
APPROVE: dict[str, Any] = {"approved": True}
CHANGES = pytest.mark.parametrize(
    ("one", "two"), [(DENY, APPROVE), (APPROVE, DENY)], ids=["deny-approve", "approve-deny"]
)
TAGGED = pytest.mark.parametrize("tagged", [True, False], ids=["tagged", "untagged"])
LOST = RPCError("update deadline exceeded", RPCStatusCode.DEADLINE_EXCEEDED, b"")


def guarded(call: str, *after: str) -> str:
    """A script that goes on when its call was not run."""
    lines = ["try:", f"    {call}", "except Exception as e:", "    o = {'status': str(e)}"]
    return script(*lines, *after, "return o['status']")


async def parked(outward: Outward, *, ends: bool = False) -> tuple[Run, str]:
    """A run whose print waits for a decision; it asks a question next, unless it `ends`."""
    after = [] if ends else ["await wait_for_human('Done?', 600)"]
    run_id = await outward.run(guarded(PRINT, *after))
    call_id = await outward.approval(run_id)
    run = await outward.store.get_run(run_id)
    assert run is not None
    return run, call_id


def asking(run: Run, call_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": run.id, "call_id": call_id, **body}


def row_of(run: Run, call_id: str, body: dict[str, Any], **update: Any) -> Decision:
    row = Decision(
        request_id=request_id(run, call_id),
        run_id=run.id,
        workflow_run_id=run.workflow_run_id,
        call_id=call_id,
        kind="approval",
        outcome="approved" if body["approved"] else "denied",
        response={"reason": body["reason"]} if "reason" in body else {},
        responder="browser",
    )
    return row.model_copy(update=update)


async def stuck(outward: Outward, row: Decision, body: dict[str, Any]) -> Decision:
    """The row of a decision that never landed, its operation over."""
    request = {"run_id": row.run_id, "call_id": row.call_id, **body}
    operations = await started(outward, FLOW_DECIDE, "first", request)
    assert await outward.store.record_decision(row)
    await operations.finish("first", result={})
    return row


async def lost(outward: Outward, run: Run, call_id: str, body: dict[str, Any]) -> Decision:
    return await stuck(outward, row_of(run, call_id, body), body)


def as_record(row: Decision) -> tuple[Any, ...]:
    response = {k: v for k, v in row.response.items() if k != REPLACED}
    return (row.outcome, response, row.input_hash, row.responder)


async def on_record(outward: Outward, run: Run, call_id: str) -> tuple[Any, ...] | None:
    held = await outward.store.get_decision(request_id(run, call_id))
    return None if held is None else as_record(held)


async def settled(outward: Outward, run: Run, call_id: str) -> Run:
    """The call has ended one way or the other."""
    return await outward.row(
        run.id, lambda r: any(s.call_id == call_id and s.status != "running" for s in r.steps)
    )


async def ended_as(outward: Outward, api: FakeApi, run: Run, call_id: str, approved: bool) -> None:
    """The call ran once when `approved`, and not at all when not."""
    after = await settled(outward, run, call_id)
    [step] = [s for s in after.steps if s.call_id == call_id]
    assert step.status == ("succeeded" if approved else "failed")
    assert len(api.to(PATH)) == (1 if approved else 0)


async def lands(
    outward: Outward, monkeypatch: pytest.MonkeyPatch, row: Decision, *, tagged: bool = True
) -> None:
    """The Update of `row` reaches the run just before the next one sent."""
    original = AgentClient.approve_tool
    update_id = await decision_update_id(Keys(), row) if tagged else row.request_id

    async def beaten(self: AgentClient, tool_id: str, **kwargs: Any) -> Any:
        monkeypatch.setattr(AgentClient, "approve_tool", original)
        await original(
            self,
            tool_id,
            approved=row.outcome == "approved",
            reason=row.response.get("reason"),
            update_id=update_id,
        )
        return await original(self, tool_id, **kwargs)

    monkeypatch.setattr(AgentClient, "approve_tool", beaten)


@CHANGES
async def test_a_decision_takes_over_one_that_never_landed(
    outward: Outward, api: FakeApi, one: dict[str, Any], two: dict[str, Any]
) -> None:
    run, call_id = await parked(outward)
    await lost(outward, run, call_id, one)
    result = await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, two))
    mine = row_of(run, call_id, two)
    assert result["outcome"] == mine.outcome
    await ended_as(outward, api, run, call_id, two["approved"])
    assert await on_record(outward, run, call_id) == as_record(mine)


async def test_another_reason_takes_over_a_denial_that_never_landed(
    outward: Outward, api: FakeApi
) -> None:
    run, call_id = await parked(outward, ends=True)
    await lost(outward, run, call_id, DENY)
    other = {"approved": False, "reason": "wrong colour"}
    await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, other))
    assert "wrong colour" in str((await outward.finished(run.id)).result)
    assert await on_record(outward, run, call_id) == as_record(row_of(run, call_id, other))
    assert api.sent == []


@CHANGES
async def test_a_decision_whose_operation_still_runs_is_not_taken_over(
    outward: Outward, api: FakeApi, one: dict[str, Any], two: dict[str, Any]
) -> None:
    run, call_id = await parked(outward)
    first = row_of(run, call_id, one)
    operations = await started(outward, FLOW_DECIDE, "first", asking(run, call_id, one))
    assert await outward.store.record_decision(first)
    decide = kinds(outward)[FLOW_DECIDE]
    request = asking(run, call_id, two)
    await started(outward, FLOW_DECIDE, "second", request)
    with pytest.raises(ApiError) as err:
        await operate(outward, decide, request)
    assert (err.value.status, err.value.type) == (409, ALREADY_RESOLVED)
    assert await on_record(outward, run, call_id) == as_record(first)
    assert api.sent == []
    await operations.finish("first", result={})
    await operate(outward, decide, request)
    await ended_as(outward, api, run, call_id, two["approved"])


async def test_the_first_decision_again_by_a_running_operation_is_not_taken_over(
    outward: Outward,
) -> None:
    run, call_id = await parked(outward)
    first = await lost(outward, run, call_id, APPROVE)
    await started(outward, FLOW_DECIDE, "again", asking(run, call_id, APPROVE))
    with pytest.raises(ApiError) as err:
        await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, DENY))
    assert (err.value.status, err.value.type) == (409, ALREADY_RESOLVED)
    assert await on_record(outward, run, call_id) == as_record(first)


@CHANGES
async def test_a_decision_whose_fate_is_unknown_is_not_taken_over(
    outward: Outward,
    monkeypatch: pytest.MonkeyPatch,
    one: dict[str, Any],
    two: dict[str, Any],
) -> None:
    run, call_id = await parked(outward)
    first = await lost(outward, run, call_id, one)
    monkeypatch.setattr(WorkflowUpdateHandle, "result", _no_answer)
    with pytest.raises(ApiError) as err:
        await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, two))
    assert (err.value.status, err.value.type) == (409, ALREADY_RESOLVED)
    assert await on_record(outward, run, call_id) == as_record(first)


@TAGGED
@CHANGES
async def test_a_decision_that_landed_is_never_taken_over(
    outward: Outward, api: FakeApi, one: dict[str, Any], two: dict[str, Any], tagged: bool
) -> None:
    """`untagged`: sent before #7588, under the request id alone."""
    run, call_id = await parked(outward)
    decide = kinds(outward)[FLOW_DECIDE]
    env = ActivityEnvironment(client=outward.client)
    request = asking(run, call_id, two)
    # Its check found the call waiting.
    checked = await env.run(decide.check, request)
    first = await lost(outward, run, call_id, one)
    await AgentClient(outward.client, run.workflow_id).approve_tool(
        call_id,
        approved=one["approved"],
        reason=one.get("reason"),
        update_id=await decision_update_id(Keys(), first) if tagged else first.request_id,
    )
    with pytest.raises(ApiError) as err:
        await env.run(decide.run, request, checked)
    assert err.value.status == 409
    assert await on_record(outward, run, call_id) == as_record(first)
    await ended_as(outward, api, run, call_id, one["approved"])


@pytest.mark.parametrize("responder", ["grant:g1", "session:s1"])
async def test_a_row_that_is_not_a_persons_is_not_taken_over(
    outward: Outward, api: FakeApi, responder: str
) -> None:
    """Recorded after the check found the call undecided."""
    run, call_id = await parked(outward)
    decide = kinds(outward)[FLOW_DECIDE]
    env = ActivityEnvironment(client=outward.client)
    grants = responder.startswith("grant:")
    checked = await env.run(decide.check, asking(run, call_id, APPROVE))
    held = row_of(run, call_id, APPROVE if grants else DENY, responder=responder)
    assert await outward.store.record_decision(held)
    for sent in (APPROVE, DENY) if grants else (APPROVE,):
        with pytest.raises(ApiError) as err:
            await env.run(decide.run, asking(run, call_id, sent), checked)
        assert err.value.status == 409
        assert err.value.type == (STALE_ENTRY if grants else ALREADY_RESOLVED)
        assert await on_record(outward, run, call_id) == as_record(held)
    assert api.sent == []
    pending = await AgentClient(outward.client, run.workflow_id).get_pending_approvals()
    assert [p.tool_id for p in pending] == [call_id]


async def test_of_two_decisions_that_take_over_one_wins(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await parked(outward)
    await lost(outward, run, call_id, {"approved": False, "reason": "first"})
    decide = kinds(outward)[FLOW_DECIDE]
    bodies = [DENY, APPROVE]
    requests = [asking(run, call_id, body) for body in bodies]
    env = ActivityEnvironment(client=outward.client)
    checks = [await env.run(decide.check, request) for request in requests]
    for number, request in enumerate(requests):
        await started(outward, FLOW_DECIDE, f"taker{number}", request)
    original, sent = AgentClient.approve_tool, []

    async def counted(self: AgentClient, tool_id: str, **kwargs: Any) -> Any:
        sent.append(tool_id)
        return await original(self, tool_id, **kwargs)

    monkeypatch.setattr(AgentClient, "approve_tool", counted)
    results = await asyncio.gather(
        *(
            ActivityEnvironment(client=outward.client).run(decide.run, request, checked)
            for request, checked in zip(requests, checks, strict=True)
        ),
        return_exceptions=True,
    )
    [won] = [body for body, r in zip(bodies, results, strict=True) if isinstance(r, dict)]
    [failed] = [r for r in results if isinstance(r, ApiError)]
    assert failed.status == 409
    assert sent == [call_id]
    assert await on_record(outward, run, call_id) == as_record(row_of(run, call_id, won))
    await ended_as(outward, api, run, call_id, won["approved"])


@CHANGES
async def test_a_decision_sent_after_its_take_over_undoes_nothing(
    outward: Outward,
    api: FakeApi,
    monkeypatch: pytest.MonkeyPatch,
    one: dict[str, Any],
    two: dict[str, Any],
) -> None:
    """A worker that was paused: its operation is over, and it sends all the same."""
    run, call_id = await parked(outward)
    operations = await started(outward, FLOW_DECIDE, "first", asking(run, call_id, one))
    decide = kinds(outward)[FLOW_DECIDE]
    original = AgentClient.approve_tool
    paused, go = asyncio.Event(), asyncio.Event()

    async def pauses(self: AgentClient, tool_id: str, **kwargs: Any) -> Any:
        if not paused.is_set():
            paused.set()
            await go.wait()
        return await original(self, tool_id, **kwargs)

    monkeypatch.setattr(AgentClient, "approve_tool", pauses)
    late = asyncio.ensure_future(operate(outward, decide, asking(run, call_id, one)))
    await paused.wait()
    await operations.finish("first", result={})
    await operate(outward, decide, asking(run, call_id, two))
    go.set()
    with pytest.raises(ApiError):
        await late
    assert await on_record(outward, run, call_id) == as_record(row_of(run, call_id, two))
    await ended_as(outward, api, run, call_id, two["approved"])


@TAGGED
async def test_a_denial_that_lands_after_its_take_over_is_on_record_again(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch, tagged: bool
) -> None:
    run, call_id = await parked(outward)
    first = await lost(outward, run, call_id, DENY)
    await lands(outward, monkeypatch, first, tagged=tagged)
    with pytest.raises(ApiError) as err:
        await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, APPROVE))
    assert (err.value.status, err.value.type) == (409, ALREADY_RESOLVED)
    # An Update that names no decision does not say that this one was the denial.
    assert await on_record(outward, run, call_id) == (as_record(first) if tagged else None)
    await ended_as(outward, api, run, call_id, approved=False)


@TAGGED
async def test_an_approval_that_lands_after_a_denial_took_it_over_runs_nothing(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch, tagged: bool
) -> None:
    """The run has the approval, and the call reads the row: the denial stands."""
    run, call_id = await parked(outward)
    first = await lost(outward, run, call_id, APPROVE)
    await lands(outward, monkeypatch, first, tagged=tagged)
    result = await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, DENY))
    assert result["outcome"] == "denied"
    assert await on_record(outward, run, call_id) == as_record(row_of(run, call_id, DENY))
    await ended_as(outward, api, run, call_id, approved=False)


@pytest.mark.parametrize("two", [DENY, {**APPROVE, "reason": "now"}], ids=["deny", "approve"])
async def test_a_decision_that_stands_for_a_late_approval_is_the_same_when_sent_again(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch, two: dict[str, Any]
) -> None:
    """Its own Update never landed, and the call did what its row says."""
    run, call_id = await parked(outward)
    first = await lost(outward, run, call_id, APPROVE)
    await lands(outward, monkeypatch, first)
    decide = kinds(outward)[FLOW_DECIDE]
    for _ in range(2):
        result = await operate(outward, decide, asking(run, call_id, two))
        assert result["outcome"] == ("approved" if two["approved"] else "denied")
        await ended_as(outward, api, run, call_id, two["approved"])
        assert await on_record(outward, run, call_id) == as_record(row_of(run, call_id, two))


async def test_a_late_approval_lets_the_call_do_what_the_third_decision_says(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await parked(outward)
    first = await lost(outward, run, call_id, APPROVE)
    decide = kinds(outward)[FLOW_DECIDE]
    original = AgentClient.approve_tool

    async def stops(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(AgentClient, "approve_tool", stops)
    with pytest.raises(asyncio.CancelledError):
        await operate(outward, decide, asking(run, call_id, DENY))
    monkeypatch.setattr(AgentClient, "approve_tool", original)
    await lands(outward, monkeypatch, first)
    third = {**APPROVE, "reason": "now"}
    assert (await operate(outward, decide, asking(run, call_id, third)))["outcome"] == "approved"
    await ended_as(outward, api, run, call_id, approved=True)
    assert await on_record(outward, run, call_id) == as_record(row_of(run, call_id, third))


async def test_a_call_let_through_with_no_decision_on_record_runs_nothing(
    outward: Outward, api: FakeApi
) -> None:
    run, call_id = await parked(outward)
    await AgentClient(outward.client, run.workflow_id).approve_tool(call_id, approved=True)
    await ended_as(outward, api, run, call_id, approved=False)


@pytest.mark.parametrize("body", [DENY, APPROVE], ids=["deny", "approve"])
async def test_a_decision_that_an_old_worker_sent_first_landed(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """An attempt of a worker from before #7588 sends the same decision, under the request
    id alone, just before this one."""
    run, call_id = await parked(outward)
    row = row_of(run, call_id, body)
    await started(outward, FLOW_DECIDE, "first", asking(run, call_id, body))
    assert await outward.store.record_decision(row)
    await lands(outward, monkeypatch, row, tagged=False)
    result = await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, body))
    assert result["outcome"] == row.outcome
    assert await on_record(outward, run, call_id) == as_record(row)
    await ended_as(outward, api, run, call_id, body["approved"])


@CHANGES
async def test_a_replaced_decision_that_lands_first_and_ends_the_run(
    outward: Outward,
    api: FakeApi,
    monkeypatch: pytest.MonkeyPatch,
    one: dict[str, Any],
    two: dict[str, Any],
) -> None:
    """The second send finds the run closed, not the call resolved."""
    run, call_id = await parked(outward, ends=True)
    first = await lost(outward, run, call_id, one)
    original = AgentClient.approve_tool
    update_id = await decision_update_id(Keys(), first)

    async def beaten(self: AgentClient, tool_id: str, **kwargs: Any) -> Any:
        await original(self, tool_id, approved=one["approved"], update_id=update_id)
        await outward.client.get_workflow_handle(run.workflow_id).result()
        return await original(self, tool_id, **kwargs)

    monkeypatch.setattr(AgentClient, "approve_tool", beaten)
    decide = kinds(outward)[FLOW_DECIDE]
    if one["approved"]:
        # The denial on record stopped the call.
        assert (await operate(outward, decide, asking(run, call_id, two)))["outcome"] == "denied"
        stands = row_of(run, call_id, two)
    else:
        with pytest.raises(RPCError):
            await operate(outward, decide, asking(run, call_id, two))
        stands = first
    assert await on_record(outward, run, call_id) == as_record(stands)
    assert api.sent == []


async def test_a_take_over_that_never_lands_leaves_no_row(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither decision reached the run, so a later one is the first again."""
    run, call_id = await parked(outward)
    await lost(outward, run, call_id, APPROVE)
    original = AgentClient.approve_tool

    async def fails(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        raise LOST

    decide = kinds(outward)[FLOW_DECIDE]
    monkeypatch.setattr(AgentClient, "approve_tool", fails)
    with pytest.raises(RPCError):
        await operate(outward, decide, asking(run, call_id, DENY))
    assert await on_record(outward, run, call_id) is None
    monkeypatch.setattr(AgentClient, "approve_tool", original)
    await operate(outward, decide, asking(run, call_id, APPROVE))
    await ended_as(outward, api, run, call_id, approved=True)


async def test_a_reset_leaves_the_old_row_and_the_call_is_decided_again(
    outward: Outward, api: FakeApi
) -> None:
    run, call_id = await parked(outward)
    first = await lost(outward, run, call_id, APPROVE)
    decide = kinds(outward)[FLOW_DECIDE]
    env = ActivityEnvironment(client=outward.client)
    request = asking(run, call_id, DENY)
    checked = await env.run(decide.check, request)
    waited = await point_after(outward, run, run.steps[0].history_length)
    # Decided another way, and that undone: the call waits in the new execution too.
    client = AgentClient(outward.client, run.workflow_id)
    await client.approve_tool(call_id, approved=False, update_id="other")
    await outward.row(run.id, lambda r: [w.kind for w in r.waiting_on] == ["answer"])
    await reset(outward, run, await point_after(outward, run, waited))
    after = await outward.row(run.id, lambda r: r.workflow_run_id != run.workflow_run_id)
    assert [w.call_id for w in after.waiting_on] == [call_id]
    with pytest.raises(ApiError) as err:
        await env.run(decide.run, request, checked)
    assert (err.value.status, err.value.type) == (409, STALE_ENTRY)
    assert await on_record(outward, run, call_id) == as_record(first)
    # The first decision was the old execution's: this one is the new one's first.
    await operate(outward, decide, request)
    await ended_as(outward, api, after, call_id, approved=False)
    assert await on_record(outward, after, call_id) == as_record(row_of(after, call_id, DENY))
    assert await on_record(outward, run, call_id) == as_record(first)


# -- another input of the same call --------------------------------------------------


async def proposed(outward: Outward, api: FakeApi) -> tuple[Run, str, dict[str, Any], Decision]:
    """A run whose print of five copies carries a proposal of three: the approval of the
    proposal, and its row."""
    escalations = Escalations(outward, api)
    run_id = await outward.run(
        guarded(PRINT_FIVE, "await wait_for_human('Done?', 600)"),
        grant=grant({"fn": "queue_print", "max_prints": 2}),
        started_by=STARTER,
    )
    call_id = await outward.approval(run_id)
    await escalations(run_id, call_id, "narrow", input={**PLAN, "copies": 3})
    run = await outward.row(run_id, lambda r: [w.hop for w in r.waiting_on] == ["person"])
    held = await outward.store.get_proposal(request_id(run, call_id))
    assert held is not None
    body = {"approved": True, "proposal": True, "proposal_hash": held.input_hash}
    row = row_of(
        run,
        call_id,
        APPROVE,
        response={"args": held.args, "narrowed_by": held.proposed_by},
        input_hash=held.input_hash,
    )
    return run, call_id, body, row


async def copies(outward: Outward, api: FakeApi, run: Run, call_id: str) -> list[int]:
    """What the call printed."""
    await settled(outward, run, call_id)
    return [body["copies"] for body, _ in api.to(PATH)]


async def test_a_narrowed_approval_that_never_landed_is_given_again(
    outward: Outward, api: FakeApi
) -> None:
    run, call_id, body, row = await proposed(outward, api)
    await stuck(outward, row, body)
    await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, body))
    assert await copies(outward, api, run, call_id) == [3]
    assert await on_record(outward, run, call_id) == as_record(row)


@pytest.mark.parametrize("late", [False, True], ids=["lost", "late"])
async def test_a_narrowed_approval_takes_over_one_of_the_call_as_parked(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch, late: bool
) -> None:
    run, call_id, body, row = await proposed(outward, api)
    first = await lost(outward, run, call_id, APPROVE)
    if late:
        await lands(outward, monkeypatch, first)
    result = await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, body))
    assert result["outcome"] == "approved"
    # What ran is what is on record, whichever Update let the call through.
    assert await copies(outward, api, run, call_id) == [3]
    assert await on_record(outward, run, call_id) == as_record(row)


@pytest.mark.parametrize("late", [False, True], ids=["lost", "late"])
async def test_an_approval_of_the_call_as_parked_takes_over_a_narrowed_one(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch, late: bool
) -> None:
    run, call_id, body, row = await proposed(outward, api)
    await stuck(outward, row, body)
    if late:
        await lands(outward, monkeypatch, row)
    result = await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, APPROVE))
    assert result["outcome"] == "approved"
    assert await copies(outward, api, run, call_id) == [5]
    assert await on_record(outward, run, call_id) == as_record(row_of(run, call_id, APPROVE))


async def test_a_narrowed_approval_that_lands_after_a_denial_took_it_over_runs_nothing(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id, body, row = await proposed(outward, api)
    await stuck(outward, row, body)
    await lands(outward, monkeypatch, row)
    await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, DENY))
    assert await copies(outward, api, run, call_id) == []
    assert await on_record(outward, run, call_id) == as_record(row_of(run, call_id, DENY))


async def test_a_narrowed_approval_whose_row_was_undone_runs_nothing(
    outward: Outward, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its Update was on its way when Temporal said that it had none."""
    run, call_id, body, row = await proposed(outward, api)
    original = AgentClient.approve_tool

    async def fails(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        raise LOST

    monkeypatch.setattr(AgentClient, "approve_tool", fails)
    with pytest.raises(RPCError):
        await operate(outward, kinds(outward)[FLOW_DECIDE], asking(run, call_id, body))
    assert await on_record(outward, run, call_id) is None
    await original(
        AgentClient(outward.client, run.workflow_id),
        call_id,
        approved=True,
        update_id=await decision_update_id(Keys(), row),
    )
    assert await copies(outward, api, run, call_id) == []


async def test_two_decisions_share_no_update(outward: Outward) -> None:
    run, call_id = await parked(outward)
    rows = [
        row_of(run, call_id, APPROVE),
        row_of(run, call_id, APPROVE, input_hash="h1"),
        row_of(run, call_id, APPROVE, input_hash="h2"),
        row_of(run, call_id, APPROVE, input_hash="h2", response={"args": {"n": 1}}),
        row_of(run, call_id, {"approved": False}),
        row_of(run, call_id, DENY),
        row_of(run, call_id, DENY, responder=f"session:{uuid.uuid4()}"),
    ]
    ids = [await decision_update_id(Keys(), row) for row in rows]
    assert len(set(ids)) == len(rows)
    assert all(i.startswith(request_id(run, call_id) + ":") for i in ids)
    # As jsonb gives it back.
    as_sent = rows[3].model_copy(update={"response": {"args": {"n": 1.0}}})
    assert await decision_update_id(Keys(), as_sent) == ids[3]
