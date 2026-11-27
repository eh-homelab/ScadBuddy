"""`wait_for_human` opens a task as `prompt()` does (#8062), on a real Temporal and Postgres.
A run from before keeps the callback path: `test_project_replay.py` replays those.
`tests/api/test_flow_prompt.py` answers such a task through its route."""

from datetime import timedelta

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient

from scadbuddy.flows.models import TERMINAL
from scadbuddy.workflows.flow_tools import OPEN_WAIT
from tests.flows.flows_support import Flows, script
from tests.flows.test_prompt import activities, give, parked, run_tasks, task_of, timers

temporal = pytest.mark.requires_temporal
postgres = pytest.mark.requires_postgres

ANSWER = {
    "type": "object",
    "properties": {"answer": {"type": "string", "title": "Answer", "minLength": 1}},
    "required": ["answer"],
}


@temporal
@postgres
async def test_a_wait_opens_a_task_with_one_answer_field(flows: Flows) -> None:
    run_id = await flows.start(
        script("a = await wait_for_human('Swap to pink?')", "return a['answer']")
    )
    run = await parked(flows, run_id)
    [waiting] = run.waiting_on
    assert (waiting.kind, waiting.fn, waiting.prompt) == (
        "answer",
        "wait_for_human",
        "Swap to pink?",
    )
    assert waiting.people_only is False
    [pending] = await AgentClient(flows.client, run.workflow_id).get_pending_callbacks()
    assert (pending.tool_id, pending.tool_name) == (waiting.call_id, "task_answer")
    task = await task_of(flows, waiting.call_id)
    assert (task.state, task.title, task.schema_, task.expires_at, task.run_id) == (
        "open",
        "Swap to pink?",
        ANSWER,
        None,
        run_id,
    )
    assert min(await timers(flows, run_id)) == OPEN_WAIT
    await give(flows, run, waiting.call_id, {"answer": "yes"})
    done = await flows.finished(run_id)
    assert (done.status, done.result) == ("succeeded", "result: 'yes'")
    assert [(s.fn, s.status) for s in done.steps] == [("wait_for_human", "succeeded")]
    task = await task_of(flows, waiting.call_id)
    assert (task.state, task.answer) == ("answered", {"answer": "yes"})


@temporal
@postgres
async def test_the_script_gets_a_human_answer(flows: Flows) -> None:
    """The task's answer object, as a `HumanAnswer` from before."""
    run_id = await flows.start(script("return await wait_for_human('q?')"))
    run = await parked(flows, run_id)
    await give(flows, run, run.waiting_on[0].call_id, {"answer": "ok"})
    assert (await flows.finished(run_id)).result == "result: {'answer': 'ok'}"


@temporal
@postgres
@pytest.mark.parametrize("flag", [True, False])
async def test_people_only_reaches_the_wait(flows: Flows, flag: bool) -> None:
    run_id = await flows.start(script(f"await wait_for_human('q?', people_only={flag})"))
    run = await parked(flows, run_id)
    assert run.waiting_on[0].people_only is flag
    [task] = await run_tasks(flows, run_id)
    assert task.state == "open"


@temporal
@postgres
async def test_a_timeout_expires_the_task_and_raises(flows: Flows) -> None:
    run_id = await flows.start(
        script(
            "try:",
            "    await wait_for_human('q?', timeout_s=10)",
            "except TimeoutError:",
            "    return 'late'",
        )
    )
    run = await parked(flows, run_id)
    call_id = run.waiting_on[0].call_id
    task = await task_of(flows, call_id)
    assert task.expires_at is not None
    assert timedelta(seconds=10) in await timers(flows, run_id)
    done = await flows.row(run_id, lambda r: r.status in TERMINAL, timeout=60)
    assert (done.status, done.result) == ("succeeded", "result: 'late'")
    assert [(s.fn, s.status, s.error) for s in done.steps] == [
        ("wait_for_human", "failed", "TimeoutError")
    ]
    assert (await task_of(flows, call_id)).state == "expired"


@temporal
@postgres
async def test_a_timeout_too_short_is_refused_before_any_task(flows: Flows) -> None:
    run_id = await flows.start(
        script(
            "try:",
            "    await wait_for_human('q?', timeout_s=5 + 1)",
            "except ValueError as e:",
            "    return str(e)",
        )
    )
    done = await flows.finished(run_id)
    assert "at least 10 seconds" in (done.result or "")
    assert done.steps == [] and await run_tasks(flows, run_id) == []
    assert "flow_task_open" not in await activities(flows, run_id)


@temporal
@postgres
async def test_a_long_question_is_cut_in_the_title_and_whole_in_the_context(flows: Flows) -> None:
    question = "Which spool? " + "x" * 1187
    assert len(question) == 1200
    run_id = await flows.start(
        script(f"a = await wait_for_human({question!r})", "return a['answer']")
    )
    run = await parked(flows, run_id)
    task = await task_of(flows, run.waiting_on[0].call_id)
    assert len(task.title) == 500 and task.title.endswith("…")
    assert question.startswith(task.title[:-1])
    assert task.context == [{"text": question}]
    await give(flows, run, run.waiting_on[0].call_id, {"answer": "red"})
    assert (await flows.finished(run_id)).result == "result: 'red'"


@temporal
@postgres
async def test_a_question_too_long_for_its_context_is_refused(flows: Flows) -> None:
    run_id = await flows.start(
        script(
            "try:",
            "    await wait_for_human('x' * 4001)",
            "except ValueError as e:",
            "    return str(e)",
        )
    )
    done = await flows.finished(run_id)
    assert "4000 characters" in (done.result or ""), done.result
    assert done.steps == [] and await run_tasks(flows, run_id) == []
