"""`handler_wait` (`workflows/handlers.py`) against the real SDK, so a temporalio bump
that changes `wait_condition` under a workflow cancel fails here."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.client import Client, WorkflowUpdateStage
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.workflows.handlers import handler_wait
from tests.support.temporal import temporal_available, temporal_client

#: One dev server for the module's tests, on one loop.
pytestmark = [pytest.mark.requires_temporal, pytest.mark.asyncio(loop_scope="module")]


@workflow.defn(sandboxed=False)
class _Waits:
    def __init__(self) -> None:
        self.go = False
        self.cancelled = False
        #: Whether the SDK has checked the waiting task's condition: it is in the wait.
        self.checked = False

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
    async def plain_wait_for_go(self) -> str:
        """`wait_condition` with no `handler_wait`, so the test sees whether it raised."""
        try:
            await workflow.wait_condition(lambda: self.go)
        except asyncio.CancelledError:
            return "raised"
        return "answered"

    @workflow.update
    async def cancelled_while_false(self) -> str:
        def never() -> bool:
            self.checked = True
            return False

        waiting = asyncio.create_task(handler_wait(never))
        await workflow.sleep(timedelta(milliseconds=100))  # a later workflow task
        if not self.checked:
            return "never waited"
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


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client() -> AsyncIterator[Client]:
    # A module fixture is set up before the function-scoped `requires_temporal` skip.
    if not temporal_available():
        pytest.skip("no Temporal")
    async with temporal_client() as connected:
        yield connected


async def test_a_workflow_cancel_while_the_condition_is_false_leaves_the_handler_to_answer(
    client: Client,
) -> None:
    """The wait began before the cancel and its condition held after it: temporalio
    1.36's `wait_condition` raises then, and the handler answers instead. A cancel that
    arrives while the condition is false does not end the wait. A plain `wait_condition`
    under the same cancel must raise, so an SDK that drops the raise fails here too."""
    queue = f"hw-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client, task_queue=queue, workflows=[_Waits], workflow_runner=UnsandboxedWorkflowRunner()
    ):
        handle = await client.start_workflow(_Waits.run, id=queue, task_queue=queue)
        update = await handle.start_update(
            _Waits.wait_for_go, wait_for_stage=WorkflowUpdateStage.ACCEPTED
        )
        plain = await handle.start_update(
            _Waits.plain_wait_for_go, wait_for_stage=WorkflowUpdateStage.ACCEPTED
        )
        await handle.cancel()
        async with asyncio.timeout(10):
            while not await handle.query(_Waits.saw_cancel):
                await asyncio.sleep(0.05)
        await handle.signal(_Waits.release)

        assert await asyncio.wait_for(update.result(), timeout=10) == "answered"
        assert await asyncio.wait_for(plain.result(), timeout=10) == "raised"
        assert await handle.result() == "done"


async def test_a_cancel_of_the_waiting_task_with_the_condition_false_is_raised(
    client: Client,
) -> None:
    """`handler_wait`'s own re-raise, for a cancel of the handler's task (an eviction).
    The cancel is an asyncio one inside the handler, so it never reaches temporalio's
    `cancellation_arrived`: this pins `handler_wait`, not the SDK. The test above is the
    one a temporalio change to `wait_condition` fails."""
    queue = f"hw-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client, task_queue=queue, workflows=[_Waits], workflow_runner=UnsandboxedWorkflowRunner()
    ):
        handle = await client.start_workflow(_Waits.run, id=queue, task_queue=queue)
        answer = await handle.execute_update(_Waits.cancelled_while_false)
        await handle.cancel()
        await handle.signal(_Waits.release)

        assert answer != "never waited", "the task never entered the wait"
        assert answer == "raised"
        assert await handle.result() == "done"
