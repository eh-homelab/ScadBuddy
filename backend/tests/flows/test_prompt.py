"""`prompt()` and a `wait_for_human` with no limit of its own (#7364), on a real Temporal and
Postgres. `tests/api/test_flow_prompt.py` answers a prompt through its route."""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient, CallbackResultError
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError
from temporalio.testing import ActivityEnvironment

from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.tasks import RemoteTasks
from scadbuddy.flows.models import HOST_FN, TERMINAL, Run
from scadbuddy.flows.sweep import sweep_flow_runs
from scadbuddy.flows.typecheck import check_script
from scadbuddy.render.projection import JobProjection
from scadbuddy.tasks.models import Task, TaskOpen
from scadbuddy.tasks.store import RunBehindError, RunEndedError, RunNotFoundError, TaskStore
from scadbuddy.workflows import projects_worker as projects_worker_module
from scadbuddy.workflows.flow_activities import FlowTaskActivities
from scadbuddy.workflows.flow_entries import FLOW_ENTRY_TIMEOUT, EntryTimeout
from scadbuddy.workflows.flow_tools import _INNER, FLOW_TOOLS, OPEN_WAIT
from tests.flows.conftest import serve_flows
from tests.flows.fake_api import Outward
from tests.flows.flows_support import Flows, backdate, script
from tests.flows.test_flow_reset import point_after, point_before, reset

temporal = pytest.mark.requires_temporal
postgres = pytest.mark.requires_postgres

EMAIL: dict[str, Any] = {
    "type": "object",
    "required": ["email"],
    "properties": {"email": {"type": "string", "format": "email"}},
}
JANE = {"email": "jane@example.com"}
ASK = f"a = await prompt('Where to?', {EMAIL!r})"


def tasks_of(flows: Flows) -> TaskStore:
    return TaskStore(flows.store._pool)


async def task_of(flows: Flows, call_id: str) -> Task:
    held = await asyncio.to_thread(tasks_of(flows).get, call_id)
    assert held is not None
    return held


async def run_tasks(flows: Flows, run_id: str) -> list[Task]:
    listed = await asyncio.to_thread(tasks_of(flows).list, state=None)
    return [t for t in listed if t.run_id == run_id]


async def parked(flows: Flows, run_id: str, count: int = 1) -> Run:
    """The run once its row and the harness both have `count` questions."""
    run = await flows.row(run_id, lambda r: len(r.waiting_on) == count)
    client = AgentClient(flows.client, run.workflow_id)
    for _ in range(100):
        if len(await client.get_pending_callbacks()) == count:
            return run
        await asyncio.sleep(0.1)
    raise AssertionError("the harness never parked the call")


async def give(flows: Flows, run: Run, call_id: str, answer: Any, **kwargs: Any) -> None:
    client = AgentClient(flows.client, run.workflow_id)
    await client.provide_callback_result(call_id, result={"answer": answer}, **kwargs)


async def events(flows: Flows, run_id: str) -> list[Any]:
    history = await flows.client.get_workflow_handle(f"flow-{run_id}").fetch_history()
    return list(history.events)


async def timers(flows: Flows, run_id: str) -> list[timedelta]:
    return [
        e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
        for e in await events(flows, run_id)
        if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
    ]


async def activities(flows: Flows, run_id: str) -> list[str]:
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in await events(flows, run_id)
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]


# -- the wait -----------------------------------------------------------------------


@temporal
@postgres
async def test_a_prompt_without_a_timeout_starts_no_timer(flows: Flows) -> None:
    run_id = await flows.start(script(ASK, "return a['email']"))
    run = await parked(flows, run_id)
    started = await timers(flows, run_id)
    assert started and min(started) == OPEN_WAIT
    [waiting] = run.waiting_on
    assert (waiting.kind, waiting.fn, waiting.prompt) == ("answer", "prompt", "Where to?")
    [pending] = await AgentClient(flows.client, run.workflow_id).get_pending_callbacks()
    assert (pending.tool_id, pending.tool_name) == (waiting.call_id, "task_answer")
    task = await task_of(flows, waiting.call_id)
    assert (task.state, task.title, task.schema_, task.expires_at) == (
        "open",
        "Where to?",
        EMAIL,
        None,
    )


@temporal
@postgres
async def test_a_prompt_keeps_its_context_place_and_ui(flows: Flows) -> None:
    run_id = await flows.start(
        script(
            f"await prompt('q', {EMAIL!r}, context=[{{'text': 'hi'}}], place='bench', ui='m.js',"
            " timeout_s=200_000)"
        )
    )
    run = await parked(flows, run_id)
    task = await task_of(flows, run.waiting_on[0].call_id)
    assert (task.context, task.place, task.ui) == ([{"text": "hi"}], "bench", "m.js")
    assert task.expires_at is not None
    assert task.expires_at - task.created_at > timedelta(seconds=199_000)
    assert timedelta(seconds=200_000) in await timers(flows, run_id)


@temporal
@postgres
async def test_the_run_gets_its_answer_and_marks_its_task(flows: Flows) -> None:
    """No operation marks it here: the run does."""
    run_id = await flows.start(script(ASK, "return a"))
    run = await parked(flows, run_id)
    call_id = run.waiting_on[0].call_id
    await give(flows, run, call_id, JANE)
    done = await flows.finished(run_id)
    assert (done.status, done.result) == ("succeeded", f"result: {JANE!r}")
    assert [(s.fn, s.status, s.outward) for s in done.steps] == [("prompt", "succeeded", False)]
    task = await task_of(flows, call_id)
    assert (task.state, task.answer, task.answered_by) == ("answered", JANE, None)


@temporal
@postgres
async def test_two_prompts_in_parallel_are_two_tasks(flows: Flows) -> None:
    run_id = await flows.start(
        script(
            f"a, b = await asyncio.gather(prompt('A?', {EMAIL!r}), prompt('B?', {EMAIL!r}))",
            "return [a['email'], b['email']]",
        )
    )
    run = await parked(flows, run_id, 2)
    by_title = {w.prompt: w.call_id for w in run.waiting_on}
    assert sorted(t.id for t in await run_tasks(flows, run_id)) == sorted(by_title.values())
    await give(flows, run, by_title["B?"], {"email": "b@example.com"})
    half = await flows.row(run_id, lambda r: [w.prompt for w in r.waiting_on] == ["A?"])
    assert half.status == "waiting"
    assert (await task_of(flows, by_title["A?"])).state == "open"
    await give(flows, run, by_title["A?"], {"email": "a@example.com"})
    done = await flows.finished(run_id)
    assert done.result == "result: ['a@example.com', 'b@example.com']"
    assert {t.title: t.answer for t in await run_tasks(flows, run_id)} == {
        "A?": {"email": "a@example.com"},
        "B?": {"email": "b@example.com"},
    }


@temporal
@postgres
async def test_a_prompt_with_timeout_expires_its_task_and_raises(flows: Flows) -> None:
    run_id = await flows.start(
        script(
            "try:",
            f"    await prompt('q', {EMAIL!r}, timeout_s=10)",
            "except TimeoutError:",
            "    return 'late'",
        )
    )
    run = await parked(flows, run_id)
    call_id = run.waiting_on[0].call_id
    done = await flows.row(run_id, lambda r: r.status in TERMINAL, timeout=60)
    assert (done.status, done.result, done.waiting_on) == ("succeeded", "result: 'late'", [])
    assert [(s.fn, s.status, s.error) for s in done.steps] == [("prompt", "failed", "TimeoutError")]
    task = await task_of(flows, call_id)
    assert (task.state, task.answer) == ("expired", None)
    with pytest.raises((CallbackResultError, RPCError)):
        await give(flows, run, call_id, JANE)


@temporal
@postgres
async def test_an_answer_the_run_never_took_leaves_its_task_expired(
    temporal_address: str, jobs: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness never ended the entry, so it took a late answer, which was marked. The
    run had timed out all the same."""

    @activity.defn(name=FLOW_ENTRY_TIMEOUT)
    async def answered_then_refused(entry: EntryTimeout) -> bool:
        await asyncio.to_thread(
            TaskStore(jobs.pool).mark_answered,
            entry.call_id,
            JANE,
            "jane",
            activity.info().workflow_run_id,
        )
        raise ApplicationError("refused", type="EntryRefused", non_retryable=True)

    monkeypatch.setattr(projects_worker_module, "flow_entry_timeout", answered_then_refused)
    async with serve_flows(temporal_address, jobs) as flows:
        run_id = await flows.start(
            script(
                "try:",
                f"    await prompt('q', {EMAIL!r}, timeout_s=10)",
                "except TimeoutError:",
                "    return 'late'",
            )
        )
        done = await flows.row(run_id, lambda r: r.status in TERMINAL, timeout=60)
        assert done.result == "result: 'late'"
        [task] = await run_tasks(flows, run_id)
        assert (task.state, task.answer, task.answered_by) == ("expired", None, None)


@temporal
@postgres
async def test_a_prompt_ended_with_an_error_cancels_its_task(flows: Flows) -> None:
    run_id = await flows.start(
        script(
            "try:",
            f"    await prompt('q', {EMAIL!r})",
            "except Exception:",
            "    return 'no answer'",
        )
    )
    run = await parked(flows, run_id)
    call_id = run.waiting_on[0].call_id
    client = AgentClient(flows.client, run.workflow_id)
    await client.provide_callback_result(call_id, error="nobody")
    done = await flows.finished(run_id)
    assert done.result == "result: 'no answer'"
    assert (await task_of(flows, call_id)).state == "cancelled"
    assert "flow_task_close" in await activities(flows, run_id)


# -- what it refuses ----------------------------------------------------------------


REF = {"type": "object", "properties": {"x": {"type": "string", "$ref": "#/a"}}}


@temporal
@postgres
@pytest.mark.parametrize(
    ("call", "said"),
    [
        (f"prompt('q', {REF!r})", "'$ref' is not supported for string"),
        (f"prompt('', {EMAIL!r})", "title"),
        (f"prompt('x' * 501, {EMAIL!r})", "at most 500 characters"),
        (f"prompt('q', {EMAIL!r}, context=[{{'t': 'x' * 17_000}}])", "16384 bytes"),
        (f"prompt('q', {EMAIL!r}, context=[{{'t': {'[' * 31 + ']' * 31}}}])", "too deeply"),
        (f"prompt('q', {EMAIL!r}, context=[{{'t': {'(' * 31 + '1' + ',)' * 31}}}])", "too deeply"),
        (f"prompt('q', {EMAIL!r}, place='a\\nb')", "place"),
        (f"prompt('q', {EMAIL!r}, ui='x' * 201)", "200 characters"),
        (f"prompt('q', {EMAIL!r}, timeout_s=5 + 1)", "at least 10 seconds"),
    ],
    ids=[
        "schema",
        "no-title",
        "long-title",
        "context",
        "deep-lists",
        "deep-tuples",
        "place",
        "ui",
        "timeout",
    ],
)
async def test_a_refused_prompt_raises_before_any_task_row(
    flows: Flows, call: str, said: str
) -> None:
    run_id = await flows.start(
        script("try:", f"    await {call}", "except ValueError as e:", "    return str(e)")
    )
    done = await flows.finished(run_id)
    assert done.status == "succeeded" and said in (done.result or ""), done.result
    assert done.steps == []
    assert await run_tasks(flows, run_id) == []
    assert "flow_task_open" not in await activities(flows, run_id)


BAD_DEFAULT = {
    "type": "object",
    "properties": {
        "x": {"type": "string", "pattern": "^a$", "maxLength": 5, "default": "b"},
    },
}


@temporal
@postgres
async def test_a_default_that_does_not_fit_is_refused_by_the_open(flows: Flows) -> None:
    """Its check runs a pattern, so the workflow leaves it to the activity."""
    run_id = await flows.start(
        script(
            "try:",
            f"    await prompt('q', {BAD_DEFAULT!r})",
            "except ValueError as e:",
            "    return str(e)",
        )
    )
    done = await flows.finished(run_id)
    assert "default does not fit" in (done.result or ""), done.result
    assert done.steps == [] and await run_tasks(flows, run_id) == []
    assert (await activities(flows, run_id)).count("flow_task_open") == 1


@temporal
@postgres
async def test_a_prompt_of_a_run_that_ended_fails_and_is_not_tried_again(
    flows: Flows, jobs: JobProjection
) -> None:
    run_id = await flows.start(script("await sleep(3)", ASK))
    await flows.row(run_id, lambda r: len(r.steps) == 1)
    assert await flows.store.mark_terminated(run_id) is not None
    handle = flows.client.get_workflow_handle(f"flow-{run_id}")
    async with asyncio.timeout(30):
        while (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
            await asyncio.sleep(0.2)
    assert (await activities(flows, run_id)).count("flow_task_open") == 1
    assert await run_tasks(flows, run_id) == []


class Tasks:
    """Where the activities write, as a test says it answers."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Any, ...]] = []

    async def open(self, task: TaskOpen, *, workflow_run_id: str | None = None) -> Any:
        self.calls.append(("open", task.id, workflow_run_id))
        if self.error is not None:
            raise self.error

    async def close(self, task_id: str, state: Any, *, workflow_run_id: str | None = None) -> Any:
        self.calls.append(("close", task_id, state, workflow_run_id))
        if self.error is not None:
            raise self.error

    async def mark_answered(
        self,
        task_id: str,
        answer: Any,
        by: str | None,
        workflow_run_id: str,
        *,
        final: bool = False,
    ) -> Any:
        self.calls.append(("answered", task_id, answer, by, workflow_run_id, final))
        if self.error is not None:
            raise self.error


OPEN = {"id": "t", "run_id": "r", "title": "q", "schema": EMAIL}


@pytest.mark.parametrize(
    ("error", "type_", "said"),
    [
        (RunEndedError("r"), "TaskRunOver", "the run has ended"),
        (RunNotFoundError("r"), "TaskRunOver", "no such run"),
        (
            CrudRefusedError(422, {"detail": "no", "errors": [{"msg": "a bad thing"}]}),
            "TaskRefused",
            "a bad thing",
        ),
        (CrudRefusedError(422, {"detail": "not a uuid"}), "TaskRefused", "not a uuid"),
    ],
    ids=["ended", "unknown", "refused", "refused-detail"],
)
async def test_the_open_activity_says_a_refusal_for_good(
    error: Exception, type_: str, said: str
) -> None:
    with pytest.raises(ApplicationError) as err:
        await ActivityEnvironment().run(FlowTaskActivities(Tasks(error)).open, OPEN)
    assert (err.value.type, err.value.message, err.value.non_retryable) == (type_, said, True)


async def test_the_open_activity_checks_the_task_itself() -> None:
    tasks = Tasks()
    with pytest.raises(ApplicationError) as err:
        await ActivityEnvironment().run(
            FlowTaskActivities(tasks).open, {**OPEN, "schema": BAD_DEFAULT}
        )
    assert err.value.type == "TaskRefused" and "default does not fit" in err.value.message
    assert err.value.non_retryable and tasks.calls == []


@pytest.mark.parametrize(
    "error", [CrudRefusedError(403, {}), RunBehindError("t"), RuntimeError("down")]
)
async def test_a_task_activity_is_tried_again_after_any_other_failure(error: Exception) -> None:
    for call in (
        lambda a: (a.open, OPEN),
        lambda a: (a.answered, "t", JANE),
        lambda a: (a.close, "t", "expired"),
    ):
        with pytest.raises(type(error)):
            await ActivityEnvironment().run(*call(FlowTaskActivities(Tasks(error))))


async def test_a_task_activity_names_its_own_execution() -> None:
    """Not one from its input, which a Reset gives to the new execution as it was."""
    env, tasks = ActivityEnvironment(), Tasks()
    held = FlowTaskActivities(tasks)
    await env.run(held.open, OPEN)
    await env.run(held.answered, "t", JANE)
    await env.run(held.close, "t", "expired")
    own = env.info.workflow_run_id
    assert tasks.calls == [
        ("open", "t", own),
        ("answered", "t", JANE, None, own, True),
        ("close", "t", "expired", own),
    ]


async def test_the_mark_of_a_task_that_is_gone_is_not_an_error() -> None:
    await ActivityEnvironment().run(FlowTaskActivities(Tasks(KeyError("t"))).answered, "t", JANE)


# -- the run's end ------------------------------------------------------------------


@temporal
@postgres
@pytest.mark.parametrize(
    ("last", "status"), [("return 1", "succeeded"), ("raise ValueError('no')", "failed")]
)
async def test_a_run_that_ends_cancels_a_task_still_open(
    flows: Flows, last: str, status: str
) -> None:
    """Code mode waits for every call, so a script's end leaves no prompt: the task is put
    there."""
    run_id = await flows.start(script("await sleep(2)", last))
    await flows.row(run_id, lambda r: len(r.steps) == 1)
    put = TaskOpen.model_validate({**OPEN, "id": str(uuid.uuid4()), "run_id": run_id})
    await asyncio.to_thread(tasks_of(flows).open, put)
    done = await flows.finished(run_id)
    assert done.status == status
    assert (await task_of(flows, put.id)).state == "cancelled"


@temporal
@postgres
async def test_a_prompt_that_is_not_awaited_is_still_waited_for(flows: Flows) -> None:
    run_id = await flows.start(script(f"p = prompt('q', {EMAIL!r})", "await sleep(1)", "return 1"))
    run = await parked(flows, run_id)
    call_id = run.waiting_on[0].call_id
    await flows.row(run_id, lambda r: r.steps[0].status == "succeeded")
    await asyncio.sleep(1)
    assert (await task_of(flows, call_id)).state == "open"
    await give(flows, run, call_id, JANE)
    assert (await flows.finished(run_id)).status == "succeeded"
    assert (await task_of(flows, call_id)).state == "answered"


@temporal
@postgres
@pytest.mark.parametrize("how", ["cancel", "terminate"])
async def test_a_closed_run_cancels_its_open_task(
    flows: Flows, jobs: JobProjection, how: str
) -> None:
    run_id = await flows.start(script(ASK))
    run = await parked(flows, run_id)
    handle = flows.client.get_workflow_handle(run.workflow_id)
    before = await activities(flows, run_id)
    if how == "cancel":
        await handle.cancel()
        async with asyncio.timeout(20):
            while (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
                await asyncio.sleep(0.1)
        # No task command stands in its way.
        assert await activities(flows, run_id) == before
    else:
        await handle.terminate(reason="operator")
    await backdate(jobs.pool, "workflow_runs.updated_at", run_id)
    await sweep_flow_runs(flows.store, flows.client, quiet=timedelta(0))
    ended = await flows.store.get_run(run_id)
    assert ended is not None and ended.status in TERMINAL
    assert (await task_of(flows, run.waiting_on[0].call_id)).state == "cancelled"


@temporal
@postgres
@pytest.mark.parametrize(
    ("write", "name"), [("open", "flow_task_open"), ("mark_answered", "flow_task_answered")]
)
@pytest.mark.parametrize("stuck", [False, True], ids=["refused", "stuck"])
async def test_a_run_cancelled_while_its_task_is_written_again_ends_at_once(
    flows: Flows,
    jobs: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
    write: str,
    name: str,
    stuck: bool,
) -> None:
    """The API is away, so the write is tried again without limit, or is still waiting for
    its answer."""
    tries = 0
    released = asyncio.Event()

    async def away(*_: Any, **__: Any) -> Any:
        nonlocal tries
        tries += 1
        if stuck:
            await released.wait()
        raise CrudUnavailableError("away", "unreached")

    monkeypatch.setattr(RemoteTasks, write, away)
    run_id = await flows.start(script(ASK, "await sleep(0.1)", "return 1"))
    if write == "mark_answered":
        run = await parked(flows, run_id)
        await give(flows, run, run.waiting_on[0].call_id, JANE)
    async with asyncio.timeout(30):
        while tries < (1 if stuck else 2):
            await asyncio.sleep(0.1)
    handle = flows.client.get_workflow_handle(f"flow-{run_id}")
    await handle.cancel()
    try:
        async with asyncio.timeout(10):
            while (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
                await asyncio.sleep(0.1)
    finally:
        released.set()
    assert (await handle.describe()).status == WorkflowExecutionStatus.CANCELED
    assert (await activities(flows, run_id)).count(name) == 1
    await backdate(jobs.pool, "workflow_runs.updated_at", run_id)
    await sweep_flow_runs(flows.store, flows.client, quiet=timedelta(0))
    ended = await flows.store.get_run(run_id)
    assert ended is not None and ended.status == "terminated"
    assert [t.state for t in await run_tasks(flows, run_id)] == (
        [] if write == "open" else ["cancelled"]
    )


# -- Resets -------------------------------------------------------------------------


@temporal
@postgres
async def test_a_reset_before_the_answer_opens_the_task_again(outward: Outward) -> None:
    run_id = await outward.run(
        script(ASK, "b = await wait_for_human('Second?')", "return [a['email'], b['answer']]")
    )
    run = await parked(outward, run_id)
    call_id = run.waiting_on[0].call_id
    waits = await point_after(outward, run, run.steps[0].history_length)
    await give(outward, run, call_id, {"email": "first@example.com"})
    second = await outward.row(run_id, lambda r: [w.prompt for w in r.waiting_on] == ["Second?"])
    assert (await task_of(outward, call_id)).state == "answered"
    await reset(outward, second, await point_after(outward, second, waits))
    again = await outward.row(
        run_id,
        lambda r: (
            r.workflow_run_id != run.workflow_run_id
            and [w.call_id for w in r.waiting_on] == [call_id]
        ),
    )
    task = await task_of(outward, call_id)
    assert (task.state, task.answer) == ("open", None)
    again = await parked(outward, run_id)
    await give(outward, again, call_id, JANE)
    second = await outward.row(run_id, lambda r: [w.prompt for w in r.waiting_on] == ["Second?"])
    await parked(outward, run_id)
    await give(outward, second, second.waiting_on[0].call_id, "go")
    done = await outward.finished(run_id)
    assert done.result == "result: ['jane@example.com', 'go']"
    task = await task_of(outward, call_id)
    assert (task.state, task.answer) == ("answered", JANE)


@temporal
@postgres
async def test_a_reset_before_the_prompt_cancels_its_task(outward: Outward) -> None:
    run_id = await outward.run(script("await sleep(0.2)", ASK, "return a['email']"))
    run = await parked(outward, run_id)
    old = run.waiting_on[0].call_id
    slept = run.steps[0].ended_history_length
    assert slept is not None
    await reset(outward, run, await point_before(outward, run, slept + 2))
    again = await outward.row(
        run_id,
        lambda r: r.workflow_run_id != run.workflow_run_id and len(r.waiting_on) == 1,
    )
    new = again.waiting_on[0].call_id
    assert new != old
    assert (await task_of(outward, old)).state == "cancelled"
    assert (await task_of(outward, new)).state == "open"
    again = await parked(outward, run_id)
    await give(outward, again, new, JANE)
    assert (await outward.finished(run_id)).result == "result: 'jane@example.com'"
    assert (await task_of(outward, old)).state == "cancelled"


@temporal
@postgres
async def test_a_reset_between_the_open_and_the_park_leaves_the_task_open(
    outward: Outward,
) -> None:
    """The open is in the history, so the call keeps its id and nothing opens it again."""
    run_id = await outward.run(script(ASK, "return a['email']"))
    run = await parked(outward, run_id)
    call_id = run.waiting_on[0].call_id
    await reset(outward, run, await point_after(outward, run, run.steps[0].history_length))
    again = await outward.row(
        run_id,
        lambda r: r.workflow_run_id != run.workflow_run_id and len(r.waiting_on) == 1,
    )
    assert again.waiting_on[0].call_id == call_id
    assert (await activities(outward, run_id)).count("flow_task_open") == 1
    assert (await task_of(outward, call_id)).state == "open"
    again = await parked(outward, run_id)
    await give(outward, again, call_id, JANE)
    assert (await outward.finished(run_id)).result == "result: 'jane@example.com'"
    assert (await task_of(outward, call_id)).state == "answered"


@temporal
@postgres
async def test_a_reset_after_the_answer_leaves_the_task_answered(outward: Outward) -> None:
    run_id = await outward.run(script(ASK, "await sleep(0.2)", "await wait_for_human('Second?')"))
    run = await parked(outward, run_id)
    call_id = run.waiting_on[0].call_id
    await give(outward, run, call_id, JANE)
    second = await outward.row(run_id, lambda r: [w.prompt for w in r.waiting_on] == ["Second?"])
    await parked(outward, run_id)
    slept = second.steps[1]
    await reset(outward, second, await point_after(outward, second, slept.history_length))
    again = await outward.row(
        run_id,
        lambda r: (
            r.workflow_run_id != run.workflow_run_id
            and [w.prompt for w in r.waiting_on] == ["Second?"]
        ),
    )
    assert again.steps[0].status == "succeeded"
    task = await task_of(outward, call_id)
    assert (task.state, task.answer) == ("answered", JANE)


# -- wait_for_human -----------------------------------------------------------------


@temporal
@postgres
async def test_wait_for_human_has_no_default_limit(flows: Flows) -> None:
    body = script("a = await wait_for_human('Swap to pink?')", "return a['answer']")
    assert await check_script(body) == []
    run_id = await flows.start(body)
    run = await parked(flows, run_id)
    started = await timers(flows, run_id)
    assert started and min(started) == OPEN_WAIT
    [pending] = await AgentClient(flows.client, run.workflow_id).get_pending_callbacks()
    assert pending.tool_name == "human_answer_open"
    assert await run_tasks(flows, run_id) == []
    await give(flows, run, run.waiting_on[0].call_id, "ok")
    assert (await flows.finished(run_id)).result == "result: 'ok'"


@temporal
@postgres
async def test_wait_for_human_takes_a_timeout_past_a_day(flows: Flows) -> None:
    body = script("a = await wait_for_human('q?', timeout_s=200_000)", "return a['answer']")
    assert await check_script(body) == []
    run_id = await flows.start(body)
    run = await parked(flows, run_id)
    assert sorted(await timers(flows, run_id)) == [timedelta(seconds=200_000), OPEN_WAIT]
    await give(flows, run, run.waiting_on[0].call_id, "ok")
    assert (await flows.finished(run_id)).result == "result: 'ok'"


@temporal
@postgres
@pytest.mark.parametrize(
    "call",
    ["wait_for_human('q?', 10**15)", f"prompt('q?', {EMAIL!r}, timeout_s=10**15)"],
    ids=["wait", "prompt"],
)
async def test_a_timeout_past_open_wait_is_open_wait(flows: Flows, call: str) -> None:
    """Temporal and a datetime take no such time."""
    run_id = await flows.start(script(f"await {call}", "return 1"))
    run = await parked(flows, run_id)
    assert max(await timers(flows, run_id)) == OPEN_WAIT
    answer: Any = "ok" if call.startswith("wait") else JANE
    await give(flows, run, run.waiting_on[0].call_id, answer)
    assert (await flows.finished(run_id)).result == "result: 1"


def test_every_callback_tool_has_its_host_function() -> None:
    callbacks = {t.__name__ for t in _INNER if not t.__name__.startswith("approved_")}
    assert callbacks == {"human_answer", "human_answer_open", "task_answer"}
    assert {t.__name__ for t in _INNER} <= set(HOST_FN)
    offered = {t.__name__ for t in FLOW_TOOLS} - {t.__name__ for t in _INNER}
    assert set(HOST_FN.values()) <= offered
