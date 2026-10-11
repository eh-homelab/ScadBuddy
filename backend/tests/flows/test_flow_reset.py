"""Reset a flow run (#1057, plan 2026-10-09-durable-phase-6-flows.md Task E2): the
`flow_reset` operation, its check pinned to a preview, and the row it leaves."""

import asyncio
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient
from temporalio.api.enums.v1 import EventType
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.history import outward_since
from scadbuddy.flows.models import Run
from scadbuddy.flows.operations import (
    FLOW_ANSWER,
    FLOW_RESET,
    RESET_CHANGED,
    RESET_POINT,
    STALE_ENTRY,
    flow_kinds,
)
from scadbuddy.operations.kinds import OperationKind
from tests.flows.fake_api import PLAN, FakeApi, Outward
from tests.flows.flows_support import script

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]


def kind(flows: Outward, name: str) -> OperationKind:
    return next(k for k in flow_kinds(flows.store) if k.name == name)


async def operate(flows: Outward, name: str, request: dict[str, Any]) -> dict[str, Any]:
    env = ActivityEnvironment(client=flows.client)
    operation = kind(flows, name)
    checked: dict[str, Any] = await env.run(operation.check, request)
    result: dict[str, Any] = await env.run(operation.run, request, checked)
    return result


async def point_before(flows: Outward, run: Run, history_length: int) -> int:
    """The last completed workflow task before `history_length`: where a Reset to
    before that step goes."""
    history = await flows.client.get_workflow_handle(run.workflow_id).fetch_history()
    return max(
        e.event_id
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
        and e.event_id < history_length
    )


async def point_after(flows: Outward, run: Run, history_length: int) -> int:
    """The first completed workflow task after `history_length`, once it is written:
    where a Reset to while that step waits goes."""
    handle = flows.client.get_workflow_handle(run.workflow_id)
    for _ in range(100):
        history = await handle.fetch_history()
        later = [
            e.event_id
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
            and e.event_id > history_length
        ]
        if later:
            return min(later)
        await asyncio.sleep(0.1)
    raise AssertionError("no workflow task completed after the step")


async def reset(flows: Outward, run: Run, point: int) -> dict[str, Any]:
    preview = await outward_since(flows.client, run, point)
    return await operate(
        flows,
        FLOW_RESET,
        {"run_id": run.id, "event_id": point, "as_of_event_id": preview.as_of_event_id},
    )


async def test_a_failed_print_runs_again_and_the_render_does_not(
    outward: Outward, api: FakeApi
) -> None:
    api.refuse_prints = True
    run_id = await outward.run(
        script(
            "r = await render('box', {'width': 20})",
            f"p = await queue_print({{'output_id': 'o1'}}, {PLAN!r})",
            "return p['status']",
        )
    )
    await outward.decide(run_id, approved=True)
    failed = await outward.finished(run_id)
    assert failed.status == "failed"
    _, printed = failed.steps
    point = await point_before(outward, failed, printed.history_length)
    api.refuse_prints = False
    result = await reset(outward, failed, point)
    after = await outward.row(run_id, lambda r: r.workflow_run_id == result["workflow_run_id"])
    assert after.workflow_run_id != failed.workflow_run_id
    assert [s.fn for s in after.steps] == ["render"]
    await outward.decide(run_id, approved=True)
    done = await outward.finished(run_id)
    assert done.result == "result: 'succeeded'"
    assert [(s.fn, s.status) for s in done.steps] == [
        ("render", "succeeded"),
        ("queue_print", "succeeded"),
    ]
    assert len(api.to("/api/v1/models/box/render")) == 1


async def test_a_reset_past_a_stale_preview_is_refused_with_the_new_one(
    outward: Outward, api: FakeApi
) -> None:
    run_id = await outward.run(
        script(
            "await render('box', {})",
            "await wait_for_human('Print?', 600)",
            f"await queue_print({{'output_id': 'o1'}}, {PLAN!r})",
            "await wait_for_human('Done?', 600)",
        )
    )
    run = await outward.row(run_id, lambda r: r.status == "waiting")
    point = await point_before(outward, run, run.steps[-1].history_length)
    stale = await outward_since(outward.client, run, point)
    assert stale.calls == []
    client = AgentClient(outward.client, run.workflow_id)
    await client.provide_callback_result(run.waiting_on[0].call_id, result={"answer": "y"})
    await outward.decide(run_id, approved=True)
    await outward.row(run_id, lambda r: len(r.steps) == 4 and r.status == "waiting")
    request = {"run_id": run_id, "event_id": point, "as_of_event_id": stale.as_of_event_id}
    with pytest.raises(ApiError) as err:
        await operate(outward, FLOW_RESET, request)
    assert (err.value.status, err.value.type) == (409, RESET_CHANGED)
    assert [c["fn"] for c in err.value.extensions["preview"]["calls"]] == ["queue_print"]
    fresh = await outward_since(outward.client, run, point)
    await operate(outward, FLOW_RESET, {**request, "as_of_event_id": fresh.as_of_event_id})
    # The point is before 'Print?' was asked: the new execution asks it again.
    after = await outward.row(
        run_id,
        lambda r: (
            r.workflow_run_id != run.workflow_run_id
            and [w.prompt for w in r.waiting_on] == ["Print?"]
        ),
    )
    assert [s.fn for s in after.steps] == ["render", "wait_for_human"]


async def test_after_a_reset_a_dropped_call_is_stale_and_the_reparked_one_answers(
    outward: Outward,
) -> None:
    run_id = await outward.run(
        script(
            "a = await wait_for_human('First?', 600)",
            "b = await wait_for_human('Second?', 600)",
            "return [a['answer'], b['answer']]",
        )
    )
    first = await outward.row(run_id, lambda r: r.status == "waiting")
    parked = await point_after(outward, first, first.steps[0].history_length)
    first_call = first.waiting_on[0].call_id
    await operate(outward, FLOW_ANSWER, {"run_id": run_id, "call_id": first_call, "answer": "1"})
    second = await outward.row(run_id, lambda r: [w.prompt for w in r.waiting_on] == ["Second?"])
    # The task that took the answer: a Reset to it does it again without the answer.
    point = await point_after(outward, second, parked)
    second_call = second.waiting_on[0].call_id
    await reset(outward, second, point)
    after = await outward.row(run_id, lambda r: r.workflow_run_id != second.workflow_run_id)
    assert [(w.call_id, w.prompt) for w in after.waiting_on] == [(first_call, "First?")]
    assert [s.status for s in after.steps] == ["running"]
    with pytest.raises(ApiError) as err:
        await operate(
            outward, FLOW_ANSWER, {"run_id": run_id, "call_id": second_call, "answer": "x"}
        )
    assert (err.value.status, err.value.type) == (409, STALE_ENTRY)
    await operate(outward, FLOW_ANSWER, {"run_id": run_id, "call_id": first_call, "answer": "2"})
    again = await outward.row(run_id, lambda r: [w.prompt for w in r.waiting_on] == ["Second?"])
    await operate(
        outward,
        FLOW_ANSWER,
        {"run_id": run_id, "call_id": again.waiting_on[0].call_id, "answer": "3"},
    )
    assert (await outward.finished(run_id)).result == "result: ['2', '3']"


async def test_a_point_that_is_not_a_completed_task_is_refused(outward: Outward) -> None:
    run_id = await outward.run(script("await wait_for_human('q?', 600)"))
    run = await outward.row(run_id, lambda r: r.status == "waiting")
    with pytest.raises(ApiError) as err:
        await operate(outward, FLOW_RESET, {"run_id": run_id, "event_id": 1, "as_of_event_id": 0})
    assert (err.value.status, err.value.type) == (422, RESET_POINT)
    assert (await outward.store.get_run(run_id)) == run
