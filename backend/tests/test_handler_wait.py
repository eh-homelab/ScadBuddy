"""`handler_wait` (`workflows/handlers.py`) against the real SDK, so a temporalio bump
that changes `wait_condition` under a workflow cancel fails here."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from temporalio import workflow
from temporalio.client import Client, WorkflowUpdateStage
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.workflows.handlers import handler_wait
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_temporal


@workflow.defn(sandboxed=False)
class _Waits:
    def __init__(self) -> None:
        self.go = False
        self.cancelled = False

    @workflow.signal
    def release(self) -> None:
        self.go = True

    @workflow.query
    def saw_cancel(self) -> bool:
        return self.cancelled

    @workflow.update
    async def wait_for_go(self) -> str:
        await handler_wait(lambda: self.go)
        return "answered"

    @workflow.update
    async def cancelled_while_false(self) -> str:
        waiting = asyncio.create_task(handler_wait(lambda: False))
        await asyncio.sleep(0)
        waiting.cancel()
        try:
            await waiting
        except asyncio.CancelledError:
            return "raised"
        return "answered"

    @workflow.run
    async def run(self) -> str:
        try:
            await workflow.wait_condition(lambda: False)
        except asyncio.CancelledError:
            self.cancelled = True
        await workflow.wait_condition(lambda: self.go)
        await workflow.wait_condition(workflow.all_handlers_finished)
        return "done"


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


async def test_a_workflow_cancel_while_the_condition_is_false_leaves_the_handler_to_answer(
    client: Client,
) -> None:
    """The wait began before the cancel and its condition held after it: temporalio
    1.36's `wait_condition` raises then, and the handler answers instead. A cancel that
    arrives while the condition is false does not end the wait."""
    queue = f"hw-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client, task_queue=queue, workflows=[_Waits], workflow_runner=UnsandboxedWorkflowRunner()
    ):
        handle = await client.start_workflow(_Waits.run, id=queue, task_queue=queue)
        update = await handle.start_update(
            _Waits.wait_for_go, wait_for_stage=WorkflowUpdateStage.ACCEPTED
        )
        await handle.cancel()
        async with asyncio.timeout(10):
            while not await handle.query(_Waits.saw_cancel):
                await asyncio.sleep(0.05)
        await handle.signal(_Waits.release)

        assert await asyncio.wait_for(update.result(), timeout=10) == "answered"
        assert await handle.result() == "done"


async def test_a_cancel_of_the_waiting_task_with_the_condition_false_is_raised(
    client: Client,
) -> None:
    queue = f"hw-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client, task_queue=queue, workflows=[_Waits], workflow_runner=UnsandboxedWorkflowRunner()
    ):
        handle = await client.start_workflow(_Waits.run, id=queue, task_queue=queue)
        answer = await handle.execute_update(_Waits.cancelled_while_false)
        await handle.cancel()
        await handle.signal(_Waits.release)

        assert answer == "raised"
        assert await handle.result() == "done"
