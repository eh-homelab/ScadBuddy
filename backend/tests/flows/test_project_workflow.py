"""`ProjectWorkflow` (#1057, plan 2026-10-09-durable-phase-6-flows.md Task B3): a flow
run on a real Temporal and Postgres, its row written only by the run itself."""

import pytest
from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    CallbackResultError,
    MidTurnRejectedError,
)
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError

from scadbuddy.flows.models import RESULT_MAX, TERMINAL
from tests.flows.flows_support import Flows, script

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]


async def test_a_run_records_its_steps_and_completes(flows: Flows) -> None:
    run_id = await flows.start(script("await sleep(1)", "await sleep(0.1)", "return 7"))
    run = await flows.finished(run_id)
    assert (run.status, run.result, run.waiting_on) == ("succeeded", "result: 7", [])
    assert [(s.seq, s.fn, s.status) for s in run.steps] == [
        (1, "sleep", "succeeded"),
        (2, "sleep", "succeeded"),
    ]
    assert run.workflow_id == f"flow-{run_id}"


async def test_wait_for_human_parks_until_answered(flows: Flows) -> None:
    run_id = await flows.start(
        script("a = await wait_for_human('Swap to pink?', 600)", "return a['answer']")
    )
    run = await flows.row(run_id, lambda r: r.status == "waiting")
    [waiting] = run.waiting_on
    assert (waiting.kind, waiting.fn, waiting.prompt) == (
        "answer",
        "wait_for_human",
        "Swap to pink?",
    )
    client = AgentClient(flows.client, f"flow-{run_id}")
    status = await client.get_status()
    assert [(p.tool_id, p.tool_name) for p in status.pending_callbacks] == [
        (waiting.call_id, "human_answer")
    ]
    await client.provide_callback_result(waiting.call_id, result={"answer": "ok"})
    run = await flows.finished(run_id)
    assert (run.status, run.result, run.waiting_on) == ("succeeded", "result: 'ok'", [])


async def test_an_unanswered_wait_times_out_and_refuses_a_late_answer(flows: Flows) -> None:
    run_id = await flows.start(script("await wait_for_human('q', timeout_s=10)"))
    run = await flows.row(run_id, lambda r: r.status == "waiting")
    call_id = run.waiting_on[0].call_id
    run = await flows.row(run_id, lambda r: r.status in TERMINAL, timeout=40)
    assert run.status == "failed" and (run.result or "").startswith("Script error (")
    assert "TimeoutError" in (run.result or "")
    assert run.waiting_on == []
    assert [(s.fn, s.status, s.error) for s in run.steps] == [
        ("wait_for_human", "failed", "TimeoutError")
    ]
    # The run closed once its script failed, so the workflow itself is gone.
    client = AgentClient(flows.client, f"flow-{run_id}")
    with pytest.raises((CallbackResultError, RPCError)):
        await client.provide_callback_result(call_id, result={"answer": "late"})


async def test_a_script_error_fails_the_run(flows: Flows) -> None:
    run_id = await flows.start(script("raise ValueError('no')"))
    run = await flows.finished(run_id)
    assert run.status == "failed"
    assert (run.result or "").startswith("Script error (") and "ValueError" in (run.result or "")


async def test_a_long_result_is_stored_cut(flows: Flows) -> None:
    run_id = await flows.start(script(f"return 'x' * {RESULT_MAX * 2}"))
    run = await flows.finished(run_id)
    assert run.result_truncated and len(run.result or "") == RESULT_MAX


async def test_a_second_execute_under_another_update_id_is_refused(flows: Flows) -> None:
    body = script("await sleep(2)", "return 1")
    run_id = await flows.start(body, update_id="first")
    run = await flows.row(run_id, lambda r: True)
    # While the script runs the harness refuses it (MidTurn.REJECT); after, execute's
    # own AlreadyExecuted does (Ruling 3).
    with pytest.raises((MidTurnRejectedError, WorkflowUpdateFailedError)):
        await flows.send(run_id, run.definition_id, body, update_id="second")
    run = await flows.finished(run_id)
    assert run.status == "succeeded" and len(run.steps) == 1


async def test_a_script_that_never_yields_fails_and_completes(flows: Flows) -> None:
    run_id = await flows.start(script("n = 0", "while True:", "    n += 1"))
    run = await flows.finished(run_id)
    assert run.status == "failed" and "TimeoutError" in (run.result or "")
