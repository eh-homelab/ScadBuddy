"""A person's answer and approval as `projects` operations (#1057, plan
2026-10-09-durable-phase-6-flows.md Task C1): the decision is recorded, then the
harness is told. A decision the harness refuses, or whose Update Temporal never had, is
deleted again; one whose Update landed stands, even when its reply was lost (#2247).
An answer that never landed does not block another one (#7575); nor does an approval or
a denial (#7588, `test_decision_take_over.py`)."""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient, CallbackResultError
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporalio.client import WorkflowUpdateHandle, WorkflowUpdateRPCTimeoutOrCancelledError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.models import Decision, Run
from scadbuddy.flows.operations import (
    ALREADY_RESOLVED,
    FLOW_ANSWER,
    FLOW_DECIDE,
    STALE_ENTRY,
    TASK_ANSWER,
    TASK_NOT_OPEN,
    decision_update_id,
    flow_kinds,
    request_id,
    seal_answer,
)
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.operations.store import OperationStore
from scadbuddy.tasks.models import TaskOpen
from scadbuddy.tasks.store import TaskNotOpenError, TaskStore
from scadbuddy.workflows.flow_models import FlowRecord, FlowWaiting, ProjectionWrite
from scadbuddy.workflows.flows_client import harness_plugins
from tests.flows.fake_api import Outward
from tests.flows.flows_support import HASH_KEY, Flows, Keys, define, script
from tests.flows.harness_probe import (
    PROBE_TOOLS,
    PROBE_WORKFLOW,
    ProbeChild,
    ProbeWorkflow,
    probe_close,
)
from tests.flows.test_flow_reset import point_after, reset

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]


def kinds(flows: Flows, tasks: Any = None) -> dict[str, OperationKind]:
    return {kind.name: kind for kind in flow_kinds(flows.remote, Keys(), HASH_KEY, tasks=tasks)}


async def operate(flows: Flows, kind: OperationKind, request: dict[str, Any]) -> dict[str, Any]:
    """The kind's check, then its run, each as its activity would run it."""
    env = ActivityEnvironment(client=flows.client)
    checked: dict[str, Any] = await env.run(kind.check, request)
    result: dict[str, Any] = await env.run(kind.run, request, checked)
    return result


async def parked(flows: Flows) -> tuple[Run, str]:
    run_id = await flows.start(
        script("a = await wait_for_human('Swap to pink?', 600)", "return a['answer']")
    )
    run = await flows.row(run_id, lambda r: r.status == "waiting")
    return run, run.waiting_on[0].call_id


async def test_an_answer_is_recorded_then_reaches_the_run(flows: Flows) -> None:
    run, call_id = await parked(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    result = await operate(flows, answer, {"run_id": run.id, "call_id": call_id, "answer": "ok"})
    assert result["outcome"] == "answered"
    done = await flows.finished(run.id)
    assert done.result == "result: 'ok'"
    decision = await flows.store.get_decision(request_id(run, call_id))
    assert decision is not None and decision.response == {"answer": "ok"}


async def test_a_sealed_answer_is_opened_and_reaches_the_run(flows: Flows) -> None:
    """#2221: the route sends `answer_sealed`; the operation opens it with the run's key."""
    run, call_id = await parked(flows)
    sealed = await seal_answer(Keys(), run.id, "ok")
    # A resent request seals to the same bytes, so it keeps its operation key.
    assert await seal_answer(Keys(), run.id, "ok") == sealed
    assert await seal_answer(Keys(), run.id, "no") != sealed
    request = {"run_id": run.id, "call_id": call_id, "answer_sealed": sealed}
    result = await operate(flows, kinds(flows)[FLOW_ANSWER], request)
    assert result["outcome"] == "answered"
    assert (await flows.finished(run.id)).result == "result: 'ok'"
    decision = await flows.store.get_decision(request_id(run, call_id))
    assert decision is not None and decision.response == {"answer": "ok"}


async def test_a_second_answer_is_refused_by_the_check(flows: Flows) -> None:
    run, call_id = await parked(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    env = ActivityEnvironment(client=flows.client)
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    checked = await env.run(answer.check, request)
    await env.run(answer.run, request, checked)
    with pytest.raises(ApiError) as err:
        await env.run(answer.check, request)
    # The run moved on, or the decision is on record: either way, nothing is sent.
    assert err.value.status == 409


async def test_an_unknown_call_is_stale_and_leaves_no_decision(flows: Flows) -> None:
    run, call_id = await parked(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    checked = await ActivityEnvironment(client=flows.client).run(answer.check, request)
    # The harness knows no such call (as after a Reset): the decision is deleted.
    ghost = {**request, "call_id": str(uuid.uuid4())}
    ghost_checked = {**checked, "request_id": request_id(run, ghost["call_id"])}
    with pytest.raises(ApiError) as err:
        await ActivityEnvironment(client=flows.client).run(answer.run, ghost, ghost_checked)
    assert (err.value.status, err.value.type) == (409, STALE_ENTRY)
    assert await flows.store.get_decision(ghost_checked["request_id"]) is None
    # The real entry is untouched and can still be answered.
    await operate(flows, answer, request)
    assert (await flows.finished(run.id)).status == "succeeded"


async def test_a_call_not_waiting_is_refused_by_the_check(flows: Flows) -> None:
    run, _ = await parked(flows)
    with pytest.raises(ApiError) as err:
        await operate(
            flows,
            kinds(flows)[FLOW_ANSWER],
            {"run_id": run.id, "call_id": "nope", "answer": "x"},
        )
    assert (err.value.status, err.value.type) == (409, STALE_ENTRY)


async def _gated(flows: Flows) -> tuple[Run, str]:
    """A probe run parked on its gated `outward` tool, with a row naming the approval
    (ProjectWorkflow's own gated host functions are `queue_print` and `arrange`)."""
    queue = f"probe-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        flows.client,
        task_queue=queue,
        workflows=[ProbeWorkflow, ProbeChild],
        activities=[probe_close],
        plugins=harness_plugins(PROBE_TOOLS),
    )
    task = asyncio.create_task(worker.run())
    flows.cleanup.append((worker, task))
    run_id = str(uuid.uuid4())
    wf_id = f"flow-{run_id}"
    await AgentClient(flows.client, wf_id).start_and_submit_message(
        "execute",
        {"script": script("return await outward('pink')")},
        workflow_name=PROBE_WORKFLOW,
        task_queue=queue,
        start_config=AgentConfig(),
    )
    client = AgentClient(flows.client, wf_id)
    for _ in range(200):
        if pending := await client.get_pending_approvals():
            break
        await asyncio.sleep(0.1)
    call_id = pending[0].tool_id
    described = await flows.client.get_workflow_handle(wf_id).describe()
    definition = await define(flows.store, "probe")
    await flows.store.insert_run(
        FlowRecord(
            run_id=run_id,
            definition_id=definition.id,
            version=1,
            name="probe",
            workflow_id=wf_id,
            workflow_run_id=described.run_id,
            started_by={},
        )
    )
    run = await flows.store.project(
        ProjectionWrite(
            run_id=run_id,
            workflow_run_id=described.run_id,
            waiting_add=FlowWaiting(
                call_id=call_id, kind="approval", fn="outward", since=datetime.now(UTC)
            ),
        )
    )
    assert run is not None
    return run, call_id


async def test_an_approval_lets_the_call_run(flows: Flows) -> None:
    run, call_id = await _gated(flows)
    decide = kinds(flows)[FLOW_DECIDE]
    result = await operate(flows, decide, {"run_id": run.id, "call_id": call_id, "approved": True})
    assert result["outcome"] == "approved"
    status = await AgentClient(flows.client, run.workflow_id).get_status()
    assert status.pending_approvals == []
    # Not an agent tool (`approved_tool`): the workflow runs it, so nothing is bound.
    decision = await flows.store.get_decision(request_id(run, call_id))
    assert decision is not None and decision.input_hash is None


async def test_a_resolved_approval_deletes_the_decision(flows: Flows) -> None:
    run, call_id = await _gated(flows)
    decide = kinds(flows)[FLOW_DECIDE]
    request = {"run_id": run.id, "call_id": call_id, "approved": False, "reason": "not now"}
    checked = await ActivityEnvironment(client=flows.client).run(decide.check, request)
    # Someone else decided first, through the harness itself.
    await AgentClient(flows.client, run.workflow_id).approve_tool(call_id, approved=True)
    with pytest.raises(ApiError) as err:
        await ActivityEnvironment(client=flows.client).run(decide.run, request, checked)
    assert (err.value.status, err.value.type) == (409, ALREADY_RESOLVED)
    assert await flows.store.get_decision(checked["request_id"]) is None


def _fails_once(monkeypatch: pytest.MonkeyPatch, method: str) -> list[int]:
    """`AgentClient.<method>` fails its first call as an Update past its deadline would,
    then works; returns the call count."""
    original = getattr(AgentClient, method)
    calls: list[int] = []

    async def flaky(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RPCError("update deadline exceeded", RPCStatusCode.DEADLINE_EXCEEDED, b"")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AgentClient, method, flaky)
    return calls


async def test_a_decision_whose_update_failed_leaves_no_row_and_can_be_made_again(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await _gated(flows)
    decide = kinds(flows)[FLOW_DECIDE]
    request = {"run_id": run.id, "call_id": call_id, "approved": True}
    calls = _fails_once(monkeypatch, "approve_tool")
    with pytest.raises(RPCError):
        await operate(flows, decide, request)
    # Nothing says approved for a call the harness was never told about.
    assert await flows.store.get_decision(request_id(run, call_id)) is None
    # The next attempt (or a new request) records it again and the call runs.
    assert (await operate(flows, decide, request))["outcome"] == "approved"
    assert len(calls) == 2
    assert (await AgentClient(flows.client, run.workflow_id).get_status()).pending_approvals == []
    decision = await flows.store.get_decision(request_id(run, call_id))
    assert decision is not None and decision.outcome == "approved"


async def test_an_answer_whose_update_failed_leaves_no_row_and_can_be_given_again(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await parked(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    _fails_once(monkeypatch, "provide_callback_result")
    with pytest.raises(RPCError):
        await operate(flows, answer, request)
    assert await flows.store.get_decision(request_id(run, call_id)) is None
    assert (await operate(flows, answer, request))["outcome"] == "answered"
    assert (await flows.finished(run.id)).result == "result: 'ok'"


async def test_a_row_left_unsent_lets_the_same_decision_resend(flows: Flows) -> None:
    run, call_id = await _gated(flows)
    decide = kinds(flows)[FLOW_DECIDE]
    rid = request_id(run, call_id)
    # A worker that died between recording the decision and sending its Update.
    left = Decision(
        request_id=rid,
        run_id=run.id,
        workflow_run_id=run.workflow_run_id,
        call_id=call_id,
        kind="approval",
        outcome="approved",
        responder="browser",
    )
    assert await flows.store.record_decision(left)
    same = {"run_id": run.id, "call_id": call_id, "approved": True}
    assert (await operate(flows, decide, same))["outcome"] == "approved"
    assert (await AgentClient(flows.client, run.workflow_id).get_status()).pending_approvals == []


async def two_questions(flows: Flows) -> tuple[Run, str]:
    """A run asking twice: answering the first leaves it waiting, on the second."""
    run_id = await flows.start(
        script(
            "a = await wait_for_human('Swap to pink?', 600)",
            "b = await wait_for_human('And the base?', 600)",
            "return a['answer'] + b['answer']",
        )
    )
    run = await flows.row(run_id, lambda r: r.status == "waiting" and len(r.waiting_on) == 1)
    return run, run.waiting_on[0].call_id


def _lands_then_fails(
    monkeypatch: pytest.MonkeyPatch, method: str, error: BaseException
) -> list[int]:
    """`AgentClient.<method>` delivers its first Update, then raises `error` as if the
    reply were lost (a deadline, the activity cancelled); later calls work."""
    original = getattr(AgentClient, method)
    calls: list[int] = []

    async def lossy(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        result = await original(self, *args, **kwargs)
        if len(calls) == 1:
            raise error
        return result

    monkeypatch.setattr(AgentClient, method, lossy)
    return calls


async def _moved_on(flows: Flows, run: Run, call_id: str) -> Run:
    return await flows.row(run.id, lambda r: all(w.call_id != call_id for w in r.waiting_on))


async def test_an_answer_that_landed_but_whose_reply_was_lost_stands_and_is_not_resent(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await two_questions(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    lost = RPCError("update deadline exceeded", RPCStatusCode.DEADLINE_EXCEEDED, b"")
    calls = _lands_then_fails(monkeypatch, "provide_callback_result", lost)
    # Temporal has the Update: the operation succeeds and the row stays.
    assert (await operate(flows, answer, request))["outcome"] == "answered"
    assert await flows.store.get_decision(request_id(run, call_id)) is not None
    await _moved_on(flows, run, call_id)
    # A retry of the same answer, once the call is no longer waiting, is told that it
    # landed, not a 409. It is not sent again.
    assert (await operate(flows, answer, request))["outcome"] == "answered"
    assert len(calls) == 1
    decision = await flows.store.get_decision(request_id(run, call_id))
    assert decision is not None and decision.response == {"answer": "ok"}


async def test_a_send_that_meets_the_run_closing_is_stale_and_keeps_no_row(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7822: a send that reaches the run while it closes, or after, never landed. Its
    row is withdrawn and the caller is told the call moved on, not a raw RPC error."""
    run, call_id = await two_questions(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}

    async def closing(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        raise RPCError(
            "workflow update was aborted by closing workflow", RPCStatusCode.NOT_FOUND, b""
        )

    monkeypatch.setattr(AgentClient, "provide_callback_result", closing)
    with pytest.raises(ApiError) as err:
        await operate(flows, answer, request)
    assert (err.value.status, err.value.type) == (409, STALE_ENTRY)
    assert await flows.store.get_decision(request_id(run, call_id)) is None


async def test_a_send_that_meets_the_run_closing_with_an_unknown_fate_keeps_its_row(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7883: when Temporal cannot say whether the Update landed, the row stays and the
    send's own error is raised, not the stale refusal (which says it never landed)."""
    run, call_id = await two_questions(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    closed = RPCError(
        "workflow update was aborted by closing workflow", RPCStatusCode.NOT_FOUND, b""
    )

    async def closing(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        raise closed

    monkeypatch.setattr(AgentClient, "provide_callback_result", closing)
    monkeypatch.setattr(WorkflowUpdateHandle, "result", _no_answer)
    with pytest.raises(RPCError) as err:
        await operate(flows, answer, request)
    assert err.value is closed
    assert await flows.store.get_decision(request_id(run, call_id)) is not None


async def test_an_approval_that_meets_the_run_closing_with_an_unknown_fate_keeps_its_row(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#7900: approvals share `tell`. An unknown fate keeps the row and raises the send's
    own error, and a warning says why, so the 5xx is told apart from the stale case."""
    run, call_id = await _gated(flows)
    decide = kinds(flows)[FLOW_DECIDE]
    request = {"run_id": run.id, "call_id": call_id, "approved": True}
    closed = RPCError(
        "workflow update was aborted by closing workflow", RPCStatusCode.NOT_FOUND, b""
    )

    async def closing(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        raise closed

    monkeypatch.setattr(AgentClient, "approve_tool", closing)
    monkeypatch.setattr(WorkflowUpdateHandle, "result", _no_answer)
    with caplog.at_level(logging.WARNING), pytest.raises(RPCError) as err:
        await operate(flows, decide, request)
    assert err.value is closed
    assert await flows.store.get_decision(request_id(run, call_id)) is not None
    assert any("whether it landed is unknown" in r.getMessage() for r in caplog.records)


async def test_an_answer_cancelled_after_it_landed_keeps_its_row_and_resends(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await two_questions(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    _lands_then_fails(monkeypatch, "provide_callback_result", asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await operate(flows, answer, request)
    # Nothing is asked while cancelling: the row stays for the next attempt.
    assert await flows.store.get_decision(request_id(run, call_id)) is not None
    await _moved_on(flows, run, call_id)
    assert (await operate(flows, answer, request))["outcome"] == "answered"


async def test_another_answer_after_one_landed_is_refused(flows: Flows) -> None:
    run, call_id = await two_questions(flows)
    answer = kinds(flows)[FLOW_ANSWER]
    await operate(flows, answer, {"run_id": run.id, "call_id": call_id, "answer": "ok"})
    await _moved_on(flows, run, call_id)
    with pytest.raises(ApiError) as err:
        await operate(flows, answer, {"run_id": run.id, "call_id": call_id, "answer": "no"})
    assert (err.value.status, err.value.type) == (409, ALREADY_RESOLVED)


async def test_an_approval_that_landed_but_whose_reply_was_lost_stands(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await _gated(flows)
    decide = kinds(flows)[FLOW_DECIDE]
    lost = RPCError("update deadline exceeded", RPCStatusCode.DEADLINE_EXCEEDED, b"")
    _lands_then_fails(monkeypatch, "approve_tool", lost)
    request = {"run_id": run.id, "call_id": call_id, "approved": True}
    assert (await operate(flows, decide, request))["outcome"] == "approved"
    decision = await flows.store.get_decision(request_id(run, call_id))
    assert decision is not None and decision.outcome == "approved"


class Marks:
    """`RemoteTasks.mark_answered` as the task operation calls it (#7364)."""

    def __init__(self, fails: Exception | None = None) -> None:
        self.fails = fails
        self.made: list[tuple[str, Any, str | None, str]] = []

    async def mark_answered(
        self, task_id: str, answer: Any, by: str | None, workflow_run_id: str
    ) -> Any:
        self.made.append((task_id, answer, by, workflow_run_id))
        if self.fails is not None:
            raise self.fails


class StoreMarks:
    """The real store's marks, as `TasksComponent` makes them."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    async def mark_answered(
        self, task_id: str, answer: Any, by: str | None, workflow_run_id: str
    ) -> Any:
        return await asyncio.to_thread(
            self.store.mark_answered, task_id, answer, by, workflow_run_id
        )


async def task_request(run: Run, call_id: str, answer: Any) -> dict[str, Any]:
    sealed = await seal_answer(Keys(), run.id, json.dumps(answer))
    return {"run_id": run.id, "task_id": call_id, "answer_sealed": sealed, "by": "jane"}


async def _no_answer(self: WorkflowUpdateHandle[Any], **kwargs: Any) -> Any:
    """Temporal cannot say whether an Update landed."""
    raise WorkflowUpdateRPCTimeoutOrCancelledError() from RPCError(
        "deadline", RPCStatusCode.DEADLINE_EXCEEDED, b""
    )


def _as_text(monkeypatch: pytest.MonkeyPatch, error: BaseException | None = None) -> list[str]:
    """The parked `wait_for_human` takes a text, so a typed answer reaches it as one; then
    `error`, as if the reply were lost. Returns the calls sent to."""
    original = AgentClient.provide_callback_result
    sent: list[str] = []

    async def typed(self: AgentClient, tool_id: str, *, result: Any, update_id: Any) -> Any:
        sent.append(tool_id)
        answer = result["answer"]
        text = {"answer": answer if isinstance(answer, str) else json.dumps(answer)}
        ack = await original(self, tool_id, result=text, update_id=update_id)
        if error is not None:
            raise error
        return ack

    monkeypatch.setattr(AgentClient, "provide_callback_result", typed)
    return sent


def _resolved_before(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolved(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        raise CallbackResultError("no", error_type="CallbackAlreadyResolved")

    monkeypatch.setattr(AgentClient, "provide_callback_result", resolved)


async def test_a_task_answer_the_run_cannot_take_is_422_and_marks_nothing(flows: Flows) -> None:
    run, call_id = await parked(flows)
    marks = Marks()
    request = await task_request(run, call_id, {"a": 1})
    with pytest.raises(ApiError) as err:
        await operate(flows, kinds(flows, marks)[TASK_ANSWER], request)
    assert err.value.status == 422
    assert marks.made == []
    assert await flows.store.get_decision(request_id(run, call_id)) is None


async def test_a_task_answer_that_landed_with_its_reply_lost_is_marked(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await two_questions(flows)
    lost = RPCError("update deadline exceeded", RPCStatusCode.DEADLINE_EXCEEDED, b"")
    _as_text(monkeypatch, lost)
    marks = Marks()
    request = await task_request(run, call_id, {"a": 1})
    result = await operate(flows, kinds(flows, marks)[TASK_ANSWER], request)
    # Never the answer: the result is in the operation's history and its record.
    assert result == {"run_id": run.id, "task_id": call_id, "outcome": "answered"}
    assert marks.made == [(call_id, {"a": 1}, "jane", run.workflow_run_id)]


@pytest.mark.parametrize("ended", [False, True])
async def test_a_task_answer_that_landed_is_marked_by_the_retry_and_not_sent_again(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, ended: bool
) -> None:
    run, call_id = await (parked(flows) if ended else two_questions(flows))
    sent = _as_text(monkeypatch, asyncio.CancelledError())
    marks = Marks()
    task_answer = kinds(flows, marks)[TASK_ANSWER]
    # A number jsonb writes another way is still the same answer.
    request = await task_request(run, call_id, {"a": 1e23})
    env = ActivityEnvironment(client=flows.client)
    checked = await env.run(task_answer.check, request)
    with pytest.raises(asyncio.CancelledError):
        await env.run(task_answer.run, request, checked)
    assert marks.made == []
    await (flows.finished(run.id) if ended else _moved_on(flows, run, call_id))
    assert (await env.run(task_answer.run, request, checked))["outcome"] == "answered"
    assert sent == [call_id]
    assert marks.made == [(call_id, {"a": 1e23}, "jane", run.workflow_run_id)]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("the answer is s3cret"),
        KeyError("gone"),
        TaskNotOpenError("expired"),
        TaskNotOpenError("answered"),
    ],
    ids=["away", "missing", "expired", "answered"],
)
async def test_a_task_answer_that_landed_stands_when_its_mark_fails(
    flows: Flows,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    run, call_id = await two_questions(flows)
    _as_text(monkeypatch)
    marks = Marks(error)
    caplog.set_level("DEBUG")
    request = await task_request(run, call_id, {"a": "s3cret"})
    result = await operate(flows, kinds(flows, marks)[TASK_ANSWER], request)
    assert result == {"run_id": run.id, "task_id": call_id, "outcome": "answered"}
    assert len(marks.made) == 1
    assert f"could not mark the task {call_id}" in caplog.text
    assert "s3cret" not in caplog.text


async def _task_of(flows: Flows, run_id: str, call_id: str) -> TaskStore:
    tasks = TaskStore(flows.store._pool)
    await asyncio.to_thread(
        tasks.open, TaskOpen(id=call_id, run_id=run_id, title="q", schema={"type": "object"})
    )
    return tasks


def _answer_request(kind: str, run: Run, call_id: str) -> dict[str, Any]:
    if kind == FLOW_ANSWER:
        return {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    return {"run_id": run.id, "task_id": call_id, "by": "jane"}


@pytest.mark.parametrize("name", [TASK_ANSWER, FLOW_ANSWER])
async def test_an_answer_that_a_reset_undid_is_neither_sent_nor_marked_again(
    outward: Outward, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run_id = await outward.run(
        script("await wait_for_human('First?', 600)", "await wait_for_human('Second?', 600)")
    )
    first = await outward.row(run_id, lambda r: r.status == "waiting")
    call_id = first.waiting_on[0].call_id
    waited = await point_after(outward, first, first.steps[0].history_length)
    tasks = await _task_of(outward, run_id, call_id)
    operation = kinds(outward, StoreMarks(tasks))[name]
    request = _answer_request(name, first, call_id)
    if name == TASK_ANSWER:
        request["answer_sealed"] = await seal_answer(Keys(), run_id, '{"a": 1}')
    env = ActivityEnvironment(client=outward.client)
    checked = await env.run(operation.check, request)
    sent = _as_text(monkeypatch, asyncio.CancelledError())
    # The attempt fails once its answer landed.
    with pytest.raises(asyncio.CancelledError):
        await env.run(operation.run, request, checked)
    second = await outward.row(run_id, lambda r: [w.prompt for w in r.waiting_on] == ["Second?"])
    await reset(outward, second, await point_after(outward, second, waited))
    after = await outward.row(run_id, lambda r: r.workflow_run_id != second.workflow_run_id)
    assert [w.call_id for w in after.waiting_on] == [call_id]
    await env.run(operation.run, request, checked)
    assert sent == [call_id]
    held = await asyncio.to_thread(tasks.get, call_id)
    assert held is not None and (held.state, held.answer) == ("open", None)
    status = await AgentClient(outward.client, after.workflow_id).get_status()
    assert [p.tool_id for p in status.pending_callbacks] == [call_id]


@pytest.mark.parametrize("recorded", [True, False])
async def test_an_answer_that_never_landed_is_not_sent_past_a_reset(
    outward: Outward, monkeypatch: pytest.MonkeyPatch, recorded: bool
) -> None:
    """An attempt that stopped before it sent, on record already or not."""
    run_id = await outward.run(
        script("await wait_for_human('First?', 600)", "await wait_for_human('Second?', 600)")
    )
    run = await outward.row(run_id, lambda r: r.status == "waiting")
    call_id = run.waiting_on[0].call_id
    marks = Marks()
    operation = kinds(outward, marks)[TASK_ANSWER]
    request = await task_request(run, call_id, {"a": 1})
    env = ActivityEnvironment(client=outward.client)
    checked = await env.run(operation.check, request)
    left = Decision(
        request_id=request_id(run, call_id),
        run_id=run_id,
        workflow_run_id=run.workflow_run_id,
        call_id=call_id,
        kind="answer",
        outcome="answered",
        response={"answer": {"a": 1}},
        responder="browser",
    )
    if recorded:
        assert await outward.store.record_decision(left)
    sent = _as_text(monkeypatch)
    # Answered another way, and that undone: the call waits in the new execution too.
    waited = await point_after(outward, run, run.steps[0].history_length)
    client = AgentClient(outward.client, run.workflow_id)
    await client.provide_callback_result(call_id, result={"answer": "y"}, update_id="other")
    await outward.row(run_id, lambda r: [w.prompt for w in r.waiting_on] == ["Second?"])
    sent.clear()
    await reset(outward, run, await point_after(outward, run, waited))
    after = await outward.row(run_id, lambda r: r.workflow_run_id != run.workflow_run_id)
    assert [w.call_id for w in after.waiting_on] == [call_id]
    with pytest.raises(ApiError) as err:
        await env.run(operation.run, request, checked)
    assert (err.value.status, err.value.type) == (409, STALE_ENTRY)
    assert sent == []
    assert marks.made == []


@pytest.mark.parametrize("cancelled", [False, True])
async def test_a_probe_that_times_out_says_nothing_about_the_answer(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    run, call_id = await parked(flows)
    lost = RPCError("update deadline exceeded", RPCStatusCode.DEADLINE_EXCEEDED, b"")

    async def lose(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        raise lost

    async def slow(self: WorkflowUpdateHandle[Any], **kwargs: Any) -> Any:
        # As the SDK raises both a deadline and a cancel (`_poll_until_outcome`).
        cause = asyncio.CancelledError() if cancelled else lost
        raise WorkflowUpdateRPCTimeoutOrCancelledError() from cause

    monkeypatch.setattr(AgentClient, "provide_callback_result", lose)
    monkeypatch.setattr(WorkflowUpdateHandle, "result", slow)
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    with pytest.raises(asyncio.CancelledError if cancelled else RPCError):
        await operate(flows, kinds(flows)[FLOW_ANSWER], request)
    # Its fate is unknown: the row stays for the next attempt.
    assert await flows.store.get_decision(request_id(run, call_id)) is not None


async def test_a_task_answer_that_lost_to_one_that_landed_marks_nothing(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await two_questions(flows)
    _as_text(monkeypatch)
    failing = kinds(flows, Marks(RuntimeError("the API is away")))[TASK_ANSWER]
    await operate(flows, failing, await task_request(run, call_id, {"a": 1}))
    marks = Marks()
    other = await task_request(run, call_id, {"a": 2})
    with pytest.raises(ApiError) as err:
        await operate(flows, kinds(flows, marks)[TASK_ANSWER], other)
    assert (err.value.status, err.value.type) == (409, TASK_NOT_OPEN)
    assert marks.made == []
    decision = await flows.store.get_decision(request_id(run, call_id))
    assert decision is not None and decision.response == {"answer": {"a": 1}}


async def test_a_task_answer_the_call_was_resolved_before_marks_nothing(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await parked(flows)
    _resolved_before(monkeypatch)
    marks = Marks()
    request = await task_request(run, call_id, {"a": 1})
    with pytest.raises(ApiError) as err:
        await operate(flows, kinds(flows, marks)[TASK_ANSWER], request)
    assert (err.value.status, err.value.type) == (409, TASK_NOT_OPEN)
    assert marks.made == []
    assert await flows.store.get_decision(request_id(run, call_id)) is None


# -- another answer's row (#7575) ------------------------------------------------

BOTH = pytest.mark.parametrize("name", [TASK_ANSWER, FLOW_ANSWER])
REFUSED = {TASK_ANSWER: TASK_NOT_OPEN, FLOW_ANSWER: ALREADY_RESOLVED}


async def answering(name: str, run: Run, call_id: str, text: str) -> dict[str, Any]:
    """A request of either kind, its answer sealed as its route seals it."""
    if name == TASK_ANSWER:
        return await task_request(run, call_id, {"a": text})
    sealed = await seal_answer(Keys(), run.id, text)
    return {"run_id": run.id, "call_id": call_id, "answer_sealed": sealed}


def row_of(name: str, run: Run, call_id: str, text: str) -> Decision:
    return Decision(
        request_id=request_id(run, call_id),
        run_id=run.id,
        workflow_run_id=run.workflow_run_id,
        call_id=call_id,
        kind="answer",
        outcome="answered",
        response={"answer": {"a": text} if name == TASK_ANSWER else text},
        responder="browser",
    )


def given(name: str, text: str) -> str:
    """What the script got of an answer, as `_as_text` gave it."""
    return json.dumps({"a": text}) if name == TASK_ANSWER else text


async def started(flows: Flows, name: str, op_id: str, request: dict[str, Any]) -> OperationStore:
    """The record of a running operation."""
    operations = OperationStore(flows.store._pool)
    await operations.insert(
        op_id,
        kind=name,
        subject=f"flow:{request['run_id']}",
        operation_key=op_id,
        request=request,
        workflow_id=f"operation-{op_id}",
        workflow_run_id=op_id,
        retention=None,
    )
    return operations


async def stuck(flows: Flows, name: str, run: Run, call_id: str) -> Decision:
    """The row of an answer that never landed, its operation over."""
    operations = await started(flows, name, "first", await answering(name, run, call_id, "one"))
    row = row_of(name, run, call_id, "one")
    assert await flows.store.record_decision(row)
    await operations.finish("first", result={})
    return row


async def responses(flows: Flows, run: Run, call_id: str) -> Any:
    decision = await flows.store.get_decision(request_id(run, call_id))
    return None if decision is None else decision.response


@BOTH
async def test_an_answer_takes_over_one_that_never_landed(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run, call_id = await parked(flows)
    await stuck(flows, name, run, call_id)
    tasks = await _task_of(flows, run.id, call_id)
    _as_text(monkeypatch)
    request = await answering(name, run, call_id, "two")
    result = await operate(flows, kinds(flows, StoreMarks(tasks))[name], request)
    assert result["outcome"] == "answered"
    # The script's answer is the one on the audit row and on the task.
    assert (await flows.finished(run.id)).result == f"result: {given(name, 'two')!r}"
    assert await responses(flows, run, call_id) == row_of(name, run, call_id, "two").response
    if name == TASK_ANSWER:
        held = await asyncio.to_thread(tasks.get, call_id)
        assert held is not None
        assert (held.state, held.answer, held.answered_by) == ("answered", {"a": "two"}, "jane")


@BOTH
async def test_an_answer_whose_operation_still_runs_refuses_another_until_it_ends(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run, call_id = await parked(flows)
    first = row_of(name, run, call_id, "one")
    operations = await started(flows, name, "first", await answering(name, run, call_id, "one"))
    assert await flows.store.record_decision(first)
    sent = _as_text(monkeypatch)
    answer = kinds(flows, Marks())[name]
    request = await answering(name, run, call_id, "two")
    await started(flows, name, "second", request)
    with pytest.raises(ApiError) as err:
        await operate(flows, answer, request)
    assert (err.value.status, err.value.type) == (409, REFUSED[name])
    assert sent == []
    assert await responses(flows, run, call_id) == first.response
    await operations.finish("first", result={})
    assert (await operate(flows, answer, request))["outcome"] == "answered"
    assert (await flows.finished(run.id)).result == f"result: {given(name, 'two')!r}"


@BOTH
async def test_the_first_answer_again_by_a_running_operation_refuses_another(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run, call_id = await parked(flows)
    first = await stuck(flows, name, run, call_id)
    await started(flows, name, "again", await answering(name, run, call_id, "one"))
    _as_text(monkeypatch)
    marks = Marks()
    with pytest.raises(ApiError) as err:
        await operate(flows, kinds(flows, marks)[name], await answering(name, run, call_id, "two"))
    assert (err.value.status, err.value.type) == (409, REFUSED[name])
    assert marks.made == []
    assert await responses(flows, run, call_id) == first.response


@BOTH
@pytest.mark.parametrize("before", [False, True], ids=["tagged", "untagged"])
async def test_an_answer_that_landed_is_never_taken_over(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str, before: bool
) -> None:
    """`untagged`: sent before #7575, under the request id alone."""
    run, call_id = await two_questions(flows)
    answer = kinds(flows, Marks())[name]
    request = await answering(name, run, call_id, "two")
    env = ActivityEnvironment(client=flows.client)
    # Its check found the call waiting.
    checked = await env.run(answer.check, request)
    first = await stuck(flows, name, run, call_id)
    update_id = first.request_id if before else await decision_update_id(Keys(), first)
    client = AgentClient(flows.client, run.workflow_id)
    await client.provide_callback_result(call_id, result={"answer": "one"}, update_id=update_id)
    sent = _as_text(monkeypatch)
    with pytest.raises(ApiError) as err:
        await env.run(answer.run, request, checked)
    assert (err.value.status, err.value.type) == (409, REFUSED[name])
    assert sent == []
    assert await responses(flows, run, call_id) == first.response


@BOTH
async def test_an_answer_whose_fate_is_unknown_is_not_taken_over(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run, call_id = await parked(flows)
    first = await stuck(flows, name, run, call_id)
    sent = _as_text(monkeypatch)

    monkeypatch.setattr(WorkflowUpdateHandle, "result", _no_answer)
    request = await answering(name, run, call_id, "two")
    with pytest.raises(ApiError) as err:
        await operate(flows, kinds(flows, Marks())[name], request)
    assert (err.value.status, err.value.type) == (409, REFUSED[name])
    assert sent == []
    assert await responses(flows, run, call_id) == first.response


@BOTH
async def test_of_two_answers_that_take_over_one_wins(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run, call_id = await parked(flows)
    await stuck(flows, name, run, call_id)
    sent = _as_text(monkeypatch)
    answer = kinds(flows, Marks())[name]
    texts = ["two", "three"]
    requests = [await answering(name, run, call_id, text) for text in texts]
    env = ActivityEnvironment(client=flows.client)
    checks = [await env.run(answer.check, request) for request in requests]
    for text, request in zip(texts, requests, strict=True):
        await started(flows, name, text, request)
    results = await asyncio.gather(
        *(
            ActivityEnvironment(client=flows.client).run(answer.run, request, checked)
            for request, checked in zip(requests, checks, strict=True)
        ),
        return_exceptions=True,
    )
    [won] = [text for text, r in zip(texts, results, strict=True) if isinstance(r, dict)]
    [lost] = [r for r in results if isinstance(r, ApiError)]
    assert (lost.status, lost.type) == (409, REFUSED[name])
    assert sent == [call_id]
    assert (await flows.finished(run.id)).result == f"result: {given(name, won)!r}"
    assert await responses(flows, run, call_id) == row_of(name, run, call_id, won).response


@BOTH
async def test_a_send_that_comes_after_the_take_over_undoes_nothing(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """A worker that was paused: its operation is over, and it sends all the same."""
    run, call_id = await parked(flows)
    requests = {t: await answering(name, run, call_id, t) for t in ("one", "two")}
    operations = await started(flows, name, "first", requests["one"])
    answer = kinds(flows, Marks())[name]
    _as_text(monkeypatch)
    typed = AgentClient.provide_callback_result
    paused, go = asyncio.Event(), asyncio.Event()

    async def pauses(self: AgentClient, *args: Any, **kwargs: Any) -> Any:
        if not paused.is_set():
            paused.set()
            await go.wait()
        return await typed(self, *args, **kwargs)

    monkeypatch.setattr(AgentClient, "provide_callback_result", pauses)
    late = asyncio.ensure_future(operate(flows, answer, requests["one"]))
    await paused.wait()
    await operations.finish("first", result={})
    await operate(flows, answer, requests["two"])
    go.set()
    with pytest.raises(ApiError):
        await late
    assert (await flows.finished(run.id)).result == f"result: {given(name, 'two')!r}"
    assert await responses(flows, run, call_id) == row_of(name, run, call_id, "two").response


@BOTH
@pytest.mark.parametrize("how", ["tagged", "untagged", "third"])
async def test_an_answer_that_lands_after_its_take_over_is_on_record_again(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str, how: str
) -> None:
    """`untagged`: sent before #7575. `third`: while another answer's operation runs."""
    run, call_id = await two_questions(flows)
    first = await stuck(flows, name, run, call_id)
    original = AgentClient.provide_callback_result
    third = await answering(name, run, call_id, "three")

    async def beaten(self: AgentClient, tool_id: str, *, result: Any, update_id: Any) -> Any:
        one = {"answer": given(name, "one")}
        tagged = await decision_update_id(Keys(), first)
        await original(
            self, tool_id, result=one, update_id=first.request_id if how == "untagged" else tagged
        )
        if how == "third":
            await started(flows, name, "third", third)
        text = {"answer": given(name, "two")}
        return await original(self, tool_id, result=text, update_id=update_id)

    monkeypatch.setattr(AgentClient, "provide_callback_result", beaten)
    marks = Marks()
    with pytest.raises(ApiError) as err:
        await operate(flows, kinds(flows, marks)[name], await answering(name, run, call_id, "two"))
    assert (err.value.status, err.value.type) == (409, REFUSED[name])
    assert marks.made == []
    assert await responses(flows, run, call_id) == first.response


@BOTH
async def test_an_answer_that_never_landed_is_not_reported_for_the_one_that_did(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run, call_id = await two_questions(flows)
    marks = Marks()
    answer = kinds(flows, marks)[name]
    env = ActivityEnvironment(client=flows.client)
    request = await answering(name, run, call_id, "two")
    checked = await env.run(answer.check, request)
    first = await stuck(flows, name, run, call_id)
    await AgentClient(flows.client, run.workflow_id).provide_callback_result(
        call_id, result={"answer": "one"}, update_id=await decision_update_id(Keys(), first)
    )
    _as_text(monkeypatch)
    record = flows.remote.record_decision

    async def withdrawn_first(decision: Decision, **kwargs: Any) -> bool:
        await flows.store.withdraw_decision(first)
        return await record(decision, **kwargs)

    monkeypatch.setattr(flows.remote, "record_decision", withdrawn_first)
    with pytest.raises(ApiError) as err:
        await env.run(answer.run, request, checked)
    assert (err.value.status, err.value.type) == (409, REFUSED[name])
    assert marks.made == []


@BOTH
async def test_an_answer_that_an_old_worker_sent_first_landed(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """An attempt of a worker from before #7575 sends the same answer, under the request id
    alone, just before this one."""
    run, call_id = await two_questions(flows)
    row = row_of(name, run, call_id, "one")
    request = await answering(name, run, call_id, "one")
    await started(flows, name, "first", request)
    assert await flows.store.record_decision(row)
    _as_text(monkeypatch)
    typed = AgentClient.provide_callback_result

    async def beaten(self: AgentClient, tool_id: str, *, result: Any, update_id: Any) -> Any:
        await typed(self, tool_id, result=result, update_id=row.request_id)
        return await typed(self, tool_id, result=result, update_id=update_id)

    monkeypatch.setattr(AgentClient, "provide_callback_result", beaten)
    marks = Marks()
    result = await operate(flows, kinds(flows, marks)[name], request)
    assert result["outcome"] == "answered"
    assert len(marks.made) == (name == TASK_ANSWER)
    assert await responses(flows, run, call_id) == row.response


@BOTH
@pytest.mark.parametrize("sent_by", ["old worker", "replaced answer"])
async def test_an_answer_that_lands_first_and_ends_the_run_is_on_record(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str, sent_by: str
) -> None:
    """The second send finds the run closed, not the call resolved."""
    run, call_id = await parked(flows)
    first = row_of(name, run, call_id, "one")
    if sent_by == "old worker":
        request = await answering(name, run, call_id, "one")
        await started(flows, name, "first", request)
        assert await flows.store.record_decision(first)
        update_id = first.request_id
    else:
        await stuck(flows, name, run, call_id)
        request = await answering(name, run, call_id, "two")
        update_id = await decision_update_id(Keys(), first)
    _as_text(monkeypatch)
    typed = AgentClient.provide_callback_result

    async def beaten(self: AgentClient, tool_id: str, *, result: Any, **kwargs: Any) -> Any:
        await typed(self, tool_id, result={"answer": "one"}, update_id=update_id)
        await flows.client.get_workflow_handle(run.workflow_id).result()
        return await typed(self, tool_id, result=result, **kwargs)

    monkeypatch.setattr(AgentClient, "provide_callback_result", beaten)
    answer = kinds(flows, Marks())[name]
    if sent_by == "old worker":
        assert (await operate(flows, answer, request))["outcome"] == "answered"
    else:
        # The run closed under the send: the call moved on (#7822), not a raw RPC error.
        with pytest.raises(ApiError) as err:
            await operate(flows, answer, request)
        assert (err.value.status, err.value.type) == (409, STALE_ENTRY)
    assert await responses(flows, run, call_id) == first.response


@BOTH
async def test_a_refused_answer_whose_fate_is_unknown_stays_on_record(
    flows: Flows, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run, call_id = await two_questions(flows)
    row = row_of(name, run, call_id, "one")
    _as_text(monkeypatch)
    typed = AgentClient.provide_callback_result

    async def beaten(self: AgentClient, tool_id: str, *, result: Any, update_id: Any) -> Any:
        await typed(self, tool_id, result=result, update_id=row.request_id)
        try:
            return await typed(self, tool_id, result=result, update_id=update_id)
        finally:
            monkeypatch.setattr(WorkflowUpdateHandle, "result", _no_answer)

    monkeypatch.setattr(AgentClient, "provide_callback_result", beaten)
    with pytest.raises(CallbackResultError):
        await operate(
            flows, kinds(flows, Marks())[name], await answering(name, run, call_id, "one")
        )
    assert await responses(flows, run, call_id) == row.response


async def test_an_answer_whose_key_cannot_be_read_records_nothing(flows: Flows) -> None:
    run, call_id = await parked(flows)

    class NoKeys(Keys):
        async def key_for(self, subject: str, create: bool) -> bytes:
            raise RuntimeError("no key")

    answer = {kind.name: kind for kind in flow_kinds(flows.remote, NoKeys(), HASH_KEY)}[FLOW_ANSWER]
    # Not sealed: opening it would read the key too.
    request = {"run_id": run.id, "call_id": call_id, "answer": "ok"}
    with pytest.raises(RuntimeError, match="no key"):
        await operate(flows, answer, request)
    assert await responses(flows, run, call_id) is None


@BOTH
async def test_a_reset_leaves_the_old_row_and_the_call_is_answered_again(
    outward: Outward, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run_id = await outward.run(
        script(
            "a = await wait_for_human('First?', 600)",
            "await wait_for_human('Second?', 600)",
            "return a['answer']",
        )
    )
    before = await outward.row(run_id, lambda r: r.status == "waiting")
    call_id = before.waiting_on[0].call_id
    first = await stuck(outward, name, before, call_id)
    answer = kinds(outward, Marks())[name]
    request = await answering(name, before, call_id, "two")
    env = ActivityEnvironment(client=outward.client)
    checked = await env.run(answer.check, request)
    # Answered another way, and that undone: the call waits in the new execution too.
    waited = await point_after(outward, before, before.steps[0].history_length)
    client = AgentClient(outward.client, before.workflow_id)
    await client.provide_callback_result(call_id, result={"answer": "y"}, update_id="other")
    await outward.row(run_id, lambda r: [w.prompt for w in r.waiting_on] == ["Second?"])
    await reset(outward, before, await point_after(outward, before, waited))
    after = await outward.row(run_id, lambda r: r.workflow_run_id != before.workflow_run_id)
    assert [w.call_id for w in after.waiting_on] == [call_id]
    _as_text(monkeypatch)
    with pytest.raises(ApiError) as err:
        await env.run(answer.run, request, checked)
    assert (err.value.status, err.value.type) == (409, STALE_ENTRY)
    assert await responses(outward, before, call_id) == first.response
    # The first answer was the old execution's: this one is the new one's first.
    assert (await operate(outward, answer, request))["outcome"] == "answered"
    second = await outward.row(run_id, lambda r: [w.prompt for w in r.waiting_on] == ["Second?"])
    await client.provide_callback_result(
        second.waiting_on[0].call_id, result={"answer": "z"}, update_id="z"
    )
    assert (await outward.finished(run_id)).result == f"result: {given(name, 'two')!r}"
    assert await responses(outward, after, call_id) == row_of(name, after, call_id, "two").response
    assert await responses(outward, before, call_id) == first.response


async def test_two_answers_share_no_update_and_one_answer_keeps_its_own(flows: Flows) -> None:
    run, call_id = await parked(flows)

    async def update_id(answer: Any) -> str:
        row = row_of(FLOW_ANSWER, run, call_id, "x").model_copy(
            update={"response": {"answer": answer}}
        )
        return await decision_update_id(Keys(), row)

    one = await update_id({"a": 1e23, "b": [1.5, 2]})
    # As jsonb gives it back.
    assert await update_id({"b": [1.5, 2.0], "a": 10**23}) == one
    assert await update_id({"a": 1e23, "b": [1.5, 3]}) != one
    assert await update_id({"a": True}) != await update_id({"a": 1})
    assert one.startswith(request_id(run, call_id) + ":")
