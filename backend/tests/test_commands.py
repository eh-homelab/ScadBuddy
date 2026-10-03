"""`start_command` (#1052, spec 2026-10-01 §4.2): every command starts with
update-with-start, attaches to a running execution and keeps the reuse policy."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.worker import Worker

from scadbuddy.workflows.commands import (
    AlreadyClosedError,
    CommandStillAcceptingError,
    TemporalUnavailableError,
    start_command,
)
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_temporal


class EchoInput(BaseModel):
    #: How long the `accepted` Update takes to answer.
    delay_s: float = 0.0
    #: Complete as soon as the first Update has answered.
    finish_at_once: bool = False


class EchoAnswer(BaseModel):
    updates: int
    run_id: str


@workflow.defn(name="EchoCommand")
class EchoCommand:
    @workflow.init
    def __init__(self, arg: EchoInput) -> None:
        self.arg = arg
        self.updates = 0
        self.finished = False

    @workflow.run
    async def run(self, arg: EchoInput) -> int:
        await workflow.wait_condition(
            lambda: self.finished or (arg.finish_at_once and self.updates > 0)
        )
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self.updates

    @workflow.update(name="accepted")
    async def accepted(self) -> EchoAnswer:
        self.updates += 1
        if self.arg.delay_s:
            await workflow.sleep(self.arg.delay_s)
        return EchoAnswer(updates=self.updates, run_id=workflow.info().run_id)

    @workflow.signal
    def finish(self) -> None:
        self.finished = True


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
def queue() -> str:
    return f"commands-{uuid.uuid4().hex[:8]}"


async def echo(
    client: Client,
    queue: str,
    workflow_id: str,
    arg: EchoInput | None = None,
    *,
    reuse: WorkflowIDReusePolicy = WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
    deadline: timedelta = timedelta(seconds=10),
) -> EchoAnswer:
    return await start_command(
        client,
        "EchoCommand",
        arg or EchoInput(),
        id=workflow_id,
        task_queue=queue,
        update="accepted",
        result_type=EchoAnswer,
        reuse=reuse,
        deadline=deadline,
    )


async def test_the_first_call_starts_and_answers_from_the_update(
    client: Client, queue: str
) -> None:
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        answer = await echo(client, queue, f"echo-{uuid.uuid4().hex}")
    assert answer.updates == 1


async def test_a_second_call_while_running_attaches_to_the_same_execution(
    client: Client, queue: str
) -> None:
    workflow_id = f"echo-{uuid.uuid4().hex}"
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        first = await echo(client, queue, workflow_id)
        second = await echo(client, queue, workflow_id)
        await client.get_workflow_handle(workflow_id).signal("finish")
    assert (first.updates, second.updates) == (1, 2)
    assert first.run_id == second.run_id


async def test_failed_only_refuses_a_new_start_after_a_completed_execution(
    client: Client, queue: str
) -> None:
    workflow_id = f"echo-{uuid.uuid4().hex}"
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        await echo(client, queue, workflow_id, EchoInput(finish_at_once=True))
        await client.get_workflow_handle(workflow_id).result()
        with pytest.raises(AlreadyClosedError):
            await echo(client, queue, workflow_id, EchoInput(finish_at_once=True))


async def test_allow_duplicate_starts_a_new_execution_after_one_completed(
    client: Client, queue: str
) -> None:
    workflow_id = f"echo-{uuid.uuid4().hex}"
    allow = WorkflowIDReusePolicy.ALLOW_DUPLICATE
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        first = await echo(client, queue, workflow_id, EchoInput(finish_at_once=True), reuse=allow)
        await client.get_workflow_handle(workflow_id).result()
        second = await echo(client, queue, workflow_id, EchoInput(finish_at_once=True), reuse=allow)
    assert second.updates == 1 and second.run_id != first.run_id


async def test_an_update_slower_than_the_deadline_is_still_accepting(
    client: Client, queue: str
) -> None:
    workflow_id = f"echo-{uuid.uuid4().hex}"
    async with Worker(client, task_queue=queue, workflows=[EchoCommand]):
        with pytest.raises(CommandStillAcceptingError):
            await echo(
                client, queue, workflow_id, EchoInput(delay_s=2), deadline=timedelta(seconds=0.3)
            )
        # The execution goes on, and the same request attaches to it.
        again = await echo(client, queue, workflow_id, EchoInput(delay_s=2))
        await client.get_workflow_handle(workflow_id).signal("finish")
    assert again.updates == 2


async def test_an_unreachable_temporal_is_unavailable_within_the_deadline(queue: str) -> None:
    """The lazy client's first connect retries for minutes; a route must not (§4.2)."""
    # Here, not at the top: the workflow sandbox re-imports this module.
    from scadbuddy.workflows.client import connect_lazily

    began = time.monotonic()
    with pytest.raises(TemporalUnavailableError):
        await echo(
            connect_lazily("127.0.0.1:1", "default"),
            queue,
            "echo-unreachable",
            deadline=timedelta(seconds=1),
        )
    assert time.monotonic() - began < 10
