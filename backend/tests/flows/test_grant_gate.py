"""The flow-own gate checks the run's grant (#2481, plan 2026-10-11-flow-grants-and-git.md
Task A3): a call inside it runs with no entry for anyone to decide, approved by the
grant with the hash of the call as it runs; a call outside it parks as before."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporalio import activity
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.approval_hash import input_hash
from scadbuddy.flows.grants import GrantSpec
from scadbuddy.flows.manifest import use_manifest
from scadbuddy.flows.operations import FLOW_DECIDE, STALE_ENTRY, flow_kinds, request_id
from scadbuddy.workflows.flow_grants import FlowGrants
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
    await outward.row(run_id, lambda r: r.steps[0].status == "succeeded")
    assert served.calls == [{"output_id": "o1"}]
    decision = await outward.store.get_decision(request_id(run, released[0]))
    assert decision is not None and decision.responder.startswith("grant:")
    assert decision.input_hash == input_hash(HASH_KEY, "send_to_bambuddy", {"output_id": "o1"})
    # The print waits for its own approval, and releasing again releases nothing.
    assert await grants.release(outward.client, run) == []
    assert api.sent == []
