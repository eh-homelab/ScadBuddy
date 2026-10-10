"""`ProjectWorkflow` (#1057, plan 2026-10-09-durable-phase-6-flows.md Task B3): a flow
run on a real Temporal and Postgres, its row written only by the run itself."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    CallbackResultError,
    MidTurnRejectedError,
)
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.service import RPCError

from scadbuddy.flows.models import RESULT_MAX, TERMINAL, Run
from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.payload_codec import SubjectForgottenError
from scadbuddy.workflows.project import PROJECT_WORKFLOW, FlowStart, RunFlow
from scadbuddy.workflows.projects_worker import projects_worker

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]


class Keys:
    """One fixed data key per subject: the codec runs, the key table is not needed."""

    def __init__(self) -> None:
        self.keys: dict[str, bytes] = {}

    async def key_for(self, subject: str, create: bool) -> bytes:
        if subject not in self.keys:
            if not create:
                raise SubjectForgottenError(subject)
            self.keys[subject] = uuid.uuid4().bytes * 2
        return self.keys[subject]


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=4)
    store.open()
    try:
        yield store
    finally:
        store.close()


class Flows:
    def __init__(self, client: Client, queue: str, store: FlowStore) -> None:
        self.client = client
        self.queue = queue
        self.store = store

    async def start(self, script: str, *, update_id: str | None = None) -> str:
        definition = await self.store.create_definition("t", script, {"kind": "browser"})
        run_id = str(uuid.uuid4())
        await self.send(run_id, definition.id, script, update_id=update_id)
        return run_id

    async def send(
        self, run_id: str, definition_id: str, script: str, *, update_id: str | None = None
    ) -> Any:
        return await AgentClient(self.client, f"flow-{run_id}").start_and_submit_message(
            "execute",
            RunFlow(script=script).model_dump(),
            workflow_name=PROJECT_WORKFLOW,
            task_queue=self.queue,
            start_config=AgentConfig(),
            start_data=FlowStart(
                run_id=run_id,
                definition_id=definition_id,
                version=1,
                name="t",
                started_by={"kind": "browser"},
            ),
            update_id=update_id,
        )

    async def row(self, run_id: str, check: Any, timeout: float = 30) -> Run:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            run = await self.store.get_run(run_id)
            if run is not None and check(run):
                return run
            if loop.time() > deadline:
                raise AssertionError(f"timed out; last row {run!r}")
            await asyncio.sleep(0.1)

    async def finished(self, run_id: str) -> Run:
        run = await self.row(run_id, lambda r: r.status in TERMINAL)
        handle = self.client.get_workflow_handle(f"flow-{run_id}")
        for _ in range(100):
            if (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED:
                return run
            await asyncio.sleep(0.1)
        raise AssertionError("the execution did not complete (Ruling 8)")


@pytest.fixture
async def flows(temporal_address: str, jobs: JobProjection) -> AsyncIterator[Flows]:
    client = await connect_flows(temporal_address, "default", Keys())
    store = FlowStore(jobs.pool)
    queue = f"projects-{uuid.uuid4().hex[:8]}"
    async with projects_worker(client, queue, FlowActivities(store).all()):
        yield Flows(client, queue, store)


def script(*body: str) -> str:
    lines = ["import asyncio", "async def main():", *(f"    {b}" for b in body)]
    return "\n".join([*lines, "asyncio.run(main())"])


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
