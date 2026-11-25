"""An answer's row names who gave it (#7471): a person (`browser`), or an agent that the
setting `flow_answerers` let answer (`AGENT_ANSWER`), with the rule that admitted it. A
person's Update id and record are as before. A person takes over any answer that never
landed; an agent only one of its own."""

import asyncio
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient

from scadbuddy.core.problems import ApiError
from scadbuddy.flows.models import Decision, Run
from scadbuddy.flows.operations import (
    AGENT_ANSWER,
    ALREADY_RESOLVED,
    FLOW_ANSWER,
    TASK_ANSWER,
    decision_update_id,
    request_id,
)
from tests.flows.flows_support import Flows, Keys
from tests.flows.test_flow_operations import (
    Marks,
    StoreMarks,
    _as_text,
    _task_of,
    answering,
    kinds,
    operate,
    parked,
    started,
    two_questions,
)

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

SESSION = "session:s1"
OTHER = "session:s2"


def agent(request: dict[str, Any], responder: str = SESSION) -> dict[str, Any]:
    """`request` as an agent's, admitted by the rule `sessions`."""
    return {**request, "responder": responder, "admitted_by": "sessions"}


def row_of(run: Run, call_id: str, text: str, responder: str = SESSION) -> Decision:
    return Decision(
        request_id=request_id(run, call_id),
        run_id=run.id,
        workflow_run_id=run.workflow_run_id,
        call_id=call_id,
        kind="answer",
        outcome="answered",
        response={"answer": text},
        responder=responder,
        admitted_by=None if responder == "browser" else "sessions",
    )


async def stuck(flows: Flows, run: Run, call_id: str, responder: str) -> Decision:
    """The row of `responder`'s answer `one`, which never landed, its operation over."""
    request = await answering(FLOW_ANSWER, run, call_id, "one")
    name = FLOW_ANSWER
    if responder != "browser":
        name, request = AGENT_ANSWER, agent(request, responder)
    operations = await started(flows, name, "first", request)
    row = row_of(run, call_id, "one", responder)
    assert await flows.store.record_decision(row)
    await operations.finish("first", result={})
    return row


async def on_record(flows: Flows, run: Run, call_id: str) -> tuple[Any, ...] | None:
    held = await flows.store.get_decision(request_id(run, call_id))
    return None if held is None else (held.response, held.responder, held.admitted_by)


async def test_a_persons_update_id_has_not_changed() -> None:
    """Computed on `main` before #7471 (`answer_update_id`, as it was called then)."""
    row = Decision(
        request_id="run-1:call-1",
        run_id="run-1",
        workflow_run_id="e1",
        call_id="call-1",
        kind="answer",
        outcome="answered",
        response={"answer": "ok"},
        responder="browser",
    )
    assert await decision_update_id(Keys(), row) == "run-1:call-1:5f523afb7a0d29a26e9fe2ac"


async def test_the_same_answer_of_two_answerers_is_two_answers(flows: Flows) -> None:
    run, call_id = await parked(flows)
    person = row_of(run, call_id, "ok", "browser")
    ids = {
        await decision_update_id(Keys(), row_of(run, call_id, "ok", who))
        for who in ("browser", SESSION, OTHER)
    }
    assert len(ids) == 3
    assert await flows.store.record_decision(person)
    assert not await flows.store.record_decision(row_of(run, call_id, "ok"))
    assert await on_record(flows, run, call_id) == ({"answer": "ok"}, "browser", None)


async def test_an_agents_answer_reaches_the_run_and_names_it(flows: Flows) -> None:
    run, call_id = await parked(flows)
    request = agent(await answering(FLOW_ANSWER, run, call_id, "ok"))
    result = await operate(flows, kinds(flows)[AGENT_ANSWER], request)
    assert result["outcome"] == "answered"
    assert (await flows.finished(run.id)).result == "result: 'ok'"
    assert await on_record(flows, run, call_id) == ({"answer": "ok"}, SESSION, "sessions")


@pytest.mark.parametrize("responder", ["browser", "grant:g1"])
async def test_an_agents_kind_never_records_a_person_or_a_grant(
    flows: Flows, responder: str
) -> None:
    run, call_id = await parked(flows)
    request = agent(await answering(FLOW_ANSWER, run, call_id, "ok"), responder)
    with pytest.raises(ApiError) as err:
        await operate(flows, kinds(flows)[AGENT_ANSWER], request)
    assert err.value.status == 422
    assert await on_record(flows, run, call_id) is None


async def test_a_persons_request_never_names_the_answerer(flows: Flows) -> None:
    """A person's kinds record `browser`, whatever their request says."""
    run, call_id = await parked(flows)
    request = agent(await answering(FLOW_ANSWER, run, call_id, "ok"))
    await operate(flows, kinds(flows)[FLOW_ANSWER], request)
    assert await on_record(flows, run, call_id) == ({"answer": "ok"}, "browser", None)


async def test_an_agents_task_answer_records_its_responder(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, call_id = await parked(flows)
    tasks = await _task_of(flows, run.id, call_id)
    _as_text(monkeypatch)
    request = agent(await answering(TASK_ANSWER, run, call_id, "ok"))
    request.pop("by", None)
    result = await operate(flows, kinds(flows, StoreMarks(tasks))[AGENT_ANSWER], request)
    assert result["outcome"] == "answered"
    held = await asyncio.to_thread(tasks.get, call_id)
    assert held is not None
    assert (held.state, held.answer, held.answered_by) == ("answered", {"a": "ok"}, SESSION)
    decision = await flows.store.get_decision(request_id(run, call_id))
    assert decision is not None and (decision.responder, decision.admitted_by) == (
        SESSION,
        "sessions",
    )


async def test_a_person_takes_over_an_agents_lost_answer(flows: Flows) -> None:
    run, call_id = await parked(flows)
    await stuck(flows, run, call_id, SESSION)
    request = await answering(FLOW_ANSWER, run, call_id, "two")
    assert (await operate(flows, kinds(flows)[FLOW_ANSWER], request))["outcome"] == "answered"
    assert (await flows.finished(run.id)).result == "result: 'two'"
    assert await on_record(flows, run, call_id) == ({"answer": "two"}, "browser", None)


@pytest.mark.parametrize("first", ["browser", OTHER])
async def test_an_agent_takes_over_only_its_own_lost_answer(flows: Flows, first: str) -> None:
    run, call_id = await parked(flows)
    row = await stuck(flows, run, call_id, first)
    request = agent(await answering(FLOW_ANSWER, run, call_id, "two"))
    with pytest.raises(ApiError) as err:
        await operate(flows, kinds(flows)[AGENT_ANSWER], request)
    assert (err.value.status, err.value.type) == (409, ALREADY_RESOLVED)
    assert await on_record(flows, run, call_id) == (
        row.response,
        row.responder,
        row.admitted_by,
    )


async def test_an_agent_takes_over_its_own_lost_answer(flows: Flows) -> None:
    run, call_id = await parked(flows)
    await stuck(flows, run, call_id, SESSION)
    request = agent(await answering(FLOW_ANSWER, run, call_id, "two"))
    assert (await operate(flows, kinds(flows)[AGENT_ANSWER], request))["outcome"] == "answered"
    assert (await flows.finished(run.id)).result == "result: 'two'"
    assert await on_record(flows, run, call_id) == ({"answer": "two"}, SESSION, "sessions")


async def test_a_running_agents_answer_refuses_a_persons_take_over(flows: Flows) -> None:
    """The agent's operation may still send its answer: a person waits for it to end."""
    run, call_id = await parked(flows)
    request = agent(await answering(FLOW_ANSWER, run, call_id, "one"))
    operations = await started(flows, AGENT_ANSWER, "first", request)
    assert await flows.store.record_decision(row_of(run, call_id, "one"))
    person = await answering(FLOW_ANSWER, run, call_id, "two")
    with pytest.raises(ApiError) as err:
        await operate(flows, kinds(flows)[FLOW_ANSWER], person)
    assert (err.value.status, err.value.type) == (409, ALREADY_RESOLVED)
    await operations.finish("first", result={})
    assert (await operate(flows, kinds(flows)[FLOW_ANSWER], person))["outcome"] == "answered"


async def test_putting_back_restores_the_answerer(
    flows: Flows, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's answer lands just before the person's take-over is sent: the row is the
    agent's again, with the rule that admitted it."""
    run, call_id = await two_questions(flows)
    first = await stuck(flows, run, call_id, SESSION)
    original = AgentClient.provide_callback_result

    async def beaten(self: AgentClient, tool_id: str, *, result: Any, update_id: Any) -> Any:
        landed = await decision_update_id(Keys(), first)
        await original(self, tool_id, result={"answer": "one"}, update_id=landed)
        return await original(self, tool_id, result=result, update_id=update_id)

    monkeypatch.setattr(AgentClient, "provide_callback_result", beaten)
    marks = Marks()
    request = await answering(FLOW_ANSWER, run, call_id, "two")
    with pytest.raises(ApiError) as err:
        await operate(flows, kinds(flows, marks)[FLOW_ANSWER], request)
    assert (err.value.status, err.value.type) == (409, ALREADY_RESOLVED)
    assert await on_record(flows, run, call_id) == ({"answer": "one"}, SESSION, "sessions")


async def test_withdraw_deletes_only_its_own_answerers_row(flows: Flows) -> None:
    run, call_id = await parked(flows)
    agents = row_of(run, call_id, "ok")
    assert await flows.store.record_decision(agents)
    for other in ("browser", OTHER):
        await flows.remote.withdraw_decision(row_of(run, call_id, "ok", other))
        assert await on_record(flows, run, call_id) == ({"answer": "ok"}, SESSION, "sessions")
    await flows.remote.withdraw_decision(agents)
    assert await on_record(flows, run, call_id) is None
