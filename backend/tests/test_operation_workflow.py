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
from temporalio.worker import Worker

from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.operations.store import Operation
from scadbuddy.workflows.commands import start_command
from scadbuddy.workflows.operation import OperationWorkflow
from scadbuddy.workflows.operation_models import (
    FinishOp,
    InsertOp,
    OperationAnswer,
    OperationInput,
    RunOp,
)
from scadbuddy.workflows.print_models import FAILED, REFUSED
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
        if self.refuse:
            raise ApplicationError(REFUSAL.detail, REFUSAL, type=REFUSED, non_retryable=True)
        return {"checked": True}

    @activity.defn(name="op_insert")
    async def insert(self, input: InsertOp) -> Operation:
        self.calls.append("insert")
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


async def test_an_unexpected_effect_error_is_recorded_as_unexpected(
    client: Client, worker: str, fake: Fake
) -> None:
    fake.run_error = RuntimeError("boom")
    arg = op_input()
    answer = await start(client, worker, arg)
    assert answer.operation is not None and answer.operation.error is not None
    assert answer.operation.error.status == 500
    assert "boom" not in answer.operation.error.detail


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
                if scheduled.activity_type.name == "op.test.check":
                    timeout = scheduled.start_to_close_timeout.ToTimedelta().total_seconds()
                    attempts["check_timeout"] = int(timeout)
    assert attempts == {"run1": 1, "run3": 3, "check_timeout": 8}


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
