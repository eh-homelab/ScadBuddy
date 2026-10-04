"""``Operation`` (#1053, spec 2026-10-01 §4.2) against fake activities registered under
the real names, on a dev server: the generic command every Bambuddy write runs as."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.operations.store import Operation
from scadbuddy.workflows import print_activities
from scadbuddy.workflows.commands import start_command
from scadbuddy.workflows.operation import OperationWorkflow
from scadbuddy.workflows.operation_activities import _kind_activities
from scadbuddy.workflows.operation_models import (
    FinishOp,
    InsertOp,
    OperationAnswer,
    OperationInput,
    RunOp,
)
from scadbuddy.workflows.print_models import FAILED, REFUSED
from scadbuddy.workflows.problems import (
    OPERATION_CANCELLED,
    OPERATION_CANCELLED_RUNNING,
    OPERATION_UNEXPECTED_DETAIL,
    OPERATION_UNEXPECTED_RUNNING_DETAIL,
)
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_temporal

REFUSAL = PrintRunError(status=409, title="Conflict", detail="archive 5 was deleted")
BAMBUDDY_502 = PrintRunError(status=502, title="Bad Gateway", detail="Bambuddy said no")


class Fake:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.refuse = False
        self.run_error: Exception | None = None
        self.hold = asyncio.Event()
        self.hold.set()
        #: Set, the check waits on it, so a test can act while it is in flight.
        self.check_gate: asyncio.Event | None = None
        #: Set, the insert waits on it.
        self.insert_gate: asyncio.Event | None = None
        #: Set, the finish waits on it.
        self.finish_gate: asyncio.Event | None = None

    def _op(self, status: str = "running", **fields: Any) -> Operation:
        return Operation(
            id="op-1",
            kind="test",
            subject="s",
            status=status,  # type: ignore[arg-type]
            created_at=datetime(2026, 10, 3, tzinfo=UTC),
            **fields,
        )

    @activity.defn(name="op.test.check")
    async def check(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("check")
        if self.check_gate is not None:
            await self.check_gate.wait()
        if self.refuse:
            raise ApplicationError(REFUSAL.detail, REFUSAL, type=REFUSED, non_retryable=True)
        return {"checked": True}

    @activity.defn(name="op_insert")
    async def insert(self, input: InsertOp) -> Operation:
        self.calls.append("insert")
        if self.insert_gate is not None:
            await self.insert_gate.wait()
        return self._op()

    @activity.defn(name="op.test.run")
    async def run(self, input: RunOp) -> dict[str, Any]:
        self.calls.append("run")
        assert input.checked == {"checked": True}
        await self.hold.wait()
        if self.run_error is not None:
            raise self.run_error
        return {"queue_item_id": 7}

    @activity.defn(name="op_finish")
    async def finish(self, input: FinishOp) -> Operation:
        self.calls.append("finish:ok" if input.error is None else f"finish:{input.error.status}")
        if self.finish_gate is not None:
            await self.finish_gate.wait()
        if input.error is not None:
            return self._op("failed", error=input.error)
        return self._op("succeeded", result=input.result)

    def all(self) -> list[Callable[..., Any]]:
        return [self.check, self.insert, self.run, self.finish]


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
async def worker(client: Client, fake: Fake) -> AsyncIterator[str]:
    queue = f"op-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client, task_queue=queue, workflows=[OperationWorkflow], activities=fake.all()
    ):
        yield queue


def op_input(*, run_attempts: int = 1) -> OperationInput:
    return OperationInput(
        kind="test",
        subject="s",
        key=uuid.uuid4().hex,
        request={"archive_id": 5},
        run_attempts=run_attempts,
    )


async def start(client: Client, queue: str, arg: OperationInput) -> OperationAnswer:
    return await start_command(
        client,
        "Operation",
        arg,
        id=f"op-{arg.kind}-{arg.key}",
        task_queue=queue,
        update="accepted",
        result_type=OperationAnswer,
        reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
    )


async def ended(client: Client, arg: OperationInput) -> Operation:
    handle = client.get_workflow_handle(f"op-{arg.kind}-{arg.key}", result_type=Operation)
    result: Operation = await handle.result()
    return result


async def test_a_refusal_answers_the_update_and_writes_nothing(
    client: Client, worker: str, fake: Fake
) -> None:
    fake.refuse = True
    arg = op_input()
    answer = await start(client, worker, arg)
    assert answer.operation is None and answer.refusal == REFUSAL
    with pytest.raises(WorkflowFailureError):
        await ended(client, arg)
    assert fake.calls == ["check"]


async def test_a_done_kind_answers_the_finished_operation(
    client: Client, worker: str, fake: Fake
) -> None:
    arg = op_input()
    answer = await start(client, worker, arg)
    assert answer.operation is not None and answer.operation.status == "succeeded"
    assert answer.operation.result == {"queue_item_id": 7} and not answer.repeated
    assert fake.calls == ["check", "insert", "run", "finish:ok"]


async def test_an_effect_failure_completes_with_the_problem(
    client: Client, worker: str, fake: Fake
) -> None:
    """§4.2: once the record exists, every outcome completes the execution."""
    fake.run_error = ApplicationError(
        BAMBUDDY_502.detail, BAMBUDDY_502, type=FAILED, non_retryable=True
    )
    arg = op_input()
    answer = await start(client, worker, arg)
    assert answer.operation is not None and answer.operation.error == BAMBUDDY_502
    assert (await ended(client, arg)).status == "failed"
    assert fake.calls[-1] == "finish:502"


async def test_an_unexpected_effect_error_says_the_effect_may_have_happened(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1063 (second) 1: a crash or timeout in the run may come after Bambuddy
    took the write, so the record warns as a cancel mid-run does."""
    fake.run_error = RuntimeError("boom")
    arg = op_input()
    answer = await start(client, worker, arg)
    assert answer.operation is not None and answer.operation.error is not None
    assert answer.operation.error.status == 500
    assert answer.operation.error.detail == OPERATION_UNEXPECTED_RUNNING_DETAIL
    assert OPERATION_UNEXPECTED_RUNNING_DETAIL != OPERATION_UNEXPECTED_DETAIL


async def test_the_run_activity_takes_the_kinds_attempts(
    client: Client, worker: str, fake: Fake
) -> None:
    attempts: dict[str, int] = {}
    for n in (1, 3):
        arg = op_input(run_attempts=n)
        await start(client, worker, arg)
        await ended(client, arg)
        handle = client.get_workflow_handle(f"op-{arg.kind}-{arg.key}")
        async for event in handle.fetch_history_events():
            if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                scheduled = event.activity_task_scheduled_event_attributes
                if scheduled.activity_type.name == "op.test.run":
                    attempts[f"run{n}"] = scheduled.retry_policy.maximum_attempts
                    heartbeat = scheduled.heartbeat_timeout.ToTimedelta().total_seconds()
                    attempts["run_heartbeat"] = int(heartbeat)
                if scheduled.activity_type.name == "op.test.check":
                    timeout = scheduled.start_to_close_timeout.ToTimedelta().total_seconds()
                    attempts["check_timeout"] = int(timeout)
    assert attempts == {"run1": 1, "run3": 3, "check_timeout": 8, "run_heartbeat": 30}


async def test_a_second_update_while_running_is_a_repeat_that_runs_nothing(
    client: Client, worker: str, fake: Fake
) -> None:
    fake.hold.clear()
    arg = op_input()
    first = asyncio.create_task(start(client, worker, arg))
    while "run" not in fake.calls:
        await asyncio.sleep(0.05)
    second = asyncio.create_task(start(client, worker, arg))
    await asyncio.sleep(0.5)
    fake.hold.set()
    answers = await asyncio.gather(first, second)
    assert sorted(a.repeated for a in answers) == [False, True]
    assert fake.calls.count("run") == 1


async def test_a_cancel_during_the_check_answers_the_update_before_the_execution_ends(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1061 1c: the Update answers a refusal, never outlived by its execution."""
    fake.check_gate = asyncio.Event()
    arg = op_input()
    accepting = asyncio.create_task(start(client, worker, arg))
    try:
        while "check" not in fake.calls:
            await asyncio.sleep(0.05)
        await client.get_workflow_handle(f"op-{arg.kind}-{arg.key}").cancel()
        answer = await accepting
        assert answer.operation is None
        assert answer.refusal == OPERATION_CANCELLED
        with pytest.raises(WorkflowFailureError):
            await ended(client, arg)
        assert fake.calls == ["check"]
    finally:
        fake.check_gate.set()


async def test_a_cancel_during_the_insert_still_records_and_ends_the_operation(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1063 1: the insert may commit after the workflow saw the cancel, so it is
    waited out and its row ended, never left ``running``. Nothing ran."""
    fake.insert_gate = asyncio.Event()
    arg = op_input()
    accepting = asyncio.create_task(start(client, worker, arg))
    while "insert" not in fake.calls:
        await asyncio.sleep(0.05)
    await client.get_workflow_handle(f"op-{arg.kind}-{arg.key}").cancel()
    await asyncio.sleep(0.5)
    fake.insert_gate.set()
    answer = await accepting
    assert answer.operation is not None and answer.operation.error == OPERATION_CANCELLED
    assert (await ended(client, arg)).status == "failed"
    assert fake.calls == ["check", "insert", "finish:409"]


async def test_a_cancel_during_the_finish_still_records_the_effects_outcome(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1063 third review 1: the effect ran, so a cancel while its outcome is being
    recorded waits the finish out; the row is never left for the reconciler to call lost."""
    fake.finish_gate = asyncio.Event()
    arg = op_input()
    accepting = asyncio.create_task(start(client, worker, arg))
    while "finish:ok" not in fake.calls:
        await asyncio.sleep(0.05)
    await client.get_workflow_handle(f"op-{arg.kind}-{arg.key}").cancel()
    await asyncio.sleep(0.5)
    fake.finish_gate.set()
    answer = await accepting
    assert answer.operation is not None and answer.operation.status == "succeeded"
    done = await ended(client, arg)
    assert done.status == "succeeded" and done.result == {"queue_item_id": 7}
    assert fake.calls == ["check", "insert", "run", "finish:ok"]


async def test_a_cancel_during_the_run_says_the_effect_may_have_happened(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1063 2: not the unexpected failure, which points at logs that say
    nothing: the effect may have reached Bambuddy, so check there first."""
    fake.hold.clear()
    arg = op_input()
    accepting = asyncio.create_task(start(client, worker, arg))
    try:
        while "run" not in fake.calls:
            await asyncio.sleep(0.05)
        await client.get_workflow_handle(f"op-{arg.kind}-{arg.key}").cancel()
        answer = await accepting
    finally:
        fake.hold.set()
    assert answer.operation is not None
    assert answer.operation.error == OPERATION_CANCELLED_RUNNING
    assert (await ended(client, arg)).status == "failed"


async def test_the_run_activity_heartbeats_while_the_effect_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #1063 (second) 2: a run on a worker that died is retired after the
    heartbeat timeout, not after the whole ``RUN_TIMEOUT``."""
    monkeypatch.setattr(print_activities, "HEARTBEAT_EVERY", 0.01)

    async def check(request: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def slow(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {"done": True}

    _, run = _kind_activities(OperationKind(name="slow", check=check, run=slow))
    env = ActivityEnvironment()
    beats: list[Any] = []
    env.on_heartbeat = lambda *details: beats.append(details)
    assert await env.run(run, RunOp(request={}, checked={})) == {"done": True}
    assert beats
