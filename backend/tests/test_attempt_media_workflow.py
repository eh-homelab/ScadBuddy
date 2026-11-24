"""An attempt's media import (#7233, spec 2026-11-05 §4.1, §4.6) on a real Temporal, its
step faked: the step's own rules are `tests/orders/test_archives.py`'s.

The dev server does not skip time, so the steps' offsets are patched to seconds and the
worker runs unsandboxed, so the patched offsets are the ones the workflow reads."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.orders.attempt_media import TemporalMediaStarter
from scadbuddy.workflows import attempt_media as module
from scadbuddy.workflows.attempt_media import (
    STEP_ACTIVITY,
    AttemptMedia,
    AttemptMediaStep,
    workflow_id,
)
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
def queue() -> str:
    return f"library-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        module, "STEP_AFTER", (timedelta(0), timedelta(seconds=1), timedelta(seconds=2))
    )


class Steps:
    """A fake step that answers ``missing`` in turn (the last one for ever). ``None``
    fails the step for good (no retry), as a step out of tries does."""

    def __init__(self, *missing: bool | None) -> None:
        self.missing = list(missing)
        self.calls: list[str] = []
        self.release = asyncio.Event()
        self.release.set()

    def activity(self) -> Callable[[str], Awaitable[AttemptMediaStep]]:
        @activity.defn(name=STEP_ACTIVITY)
        async def attempt_media_step(fulfilment_id: str) -> AttemptMediaStep:
            self.calls.append(fulfilment_id)
            await self.release.wait()
            answer = self.missing[min(len(self.calls), len(self.missing)) - 1]
            if answer is None:
                raise ApplicationError("the step failed", non_retryable=True)
            return AttemptMediaStep(missing=answer)

        return attempt_media_step


def serving(client: Client, queue: str, steps: Steps) -> Worker:
    return Worker(
        client,
        task_queue=queue,
        workflows=[AttemptMedia],
        activities=[steps.activity()],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )


async def start(
    client: Client, queue: str, fulfilment_id: str, *, retries: bool
) -> WorkflowHandle[object, str]:
    await TemporalMediaStarter(client, queue).start(fulfilment_id, retries=retries)
    return client.get_workflow_handle(workflow_id(fulfilment_id), result_type=str)


async def test_a_run_with_retries_steps_three_times_while_a_medium_is_missing(
    client: Client, queue: str
) -> None:
    attempt = uuid.uuid4().hex
    steps = Steps(True)
    try:
        async with serving(client, queue, steps):
            handle = await start(client, queue, attempt, retries=True)
            assert await asyncio.wait_for(handle.result(), 30) == "missing"
        assert steps.calls == [attempt] * 3
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_run_ends_after_the_first_step_with_nothing_missing(
    client: Client, queue: str
) -> None:
    attempt = uuid.uuid4().hex
    steps = Steps(True, False)
    try:
        async with serving(client, queue, steps):
            handle = await start(client, queue, attempt, retries=True)
            assert await asyncio.wait_for(handle.result(), 30) == "done"
        assert len(steps.calls) == 2
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_run_without_retries_steps_once(client: Client, queue: str) -> None:
    """The read's start: one step, even with a medium still missing."""
    attempt = uuid.uuid4().hex
    steps = Steps(True)
    try:
        async with serving(client, queue, steps):
            handle = await start(client, queue, attempt, retries=False)
            assert await asyncio.wait_for(handle.result(), 30) == "missing"
        assert len(steps.calls) == 1
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_second_start_joins_the_run_and_one_after_its_end_runs_again(
    client: Client, queue: str
) -> None:
    """Three replicas hear one outcome: one run. A trigger after the run ended starts a
    new run, whose step reads the attempt afresh."""
    attempt = uuid.uuid4().hex
    steps = Steps(False)
    steps.release.clear()
    try:
        async with serving(client, queue, steps):
            handle = await start(client, queue, attempt, retries=True)
            first = (await handle.describe()).run_id
            while not steps.calls:
                await asyncio.sleep(0.05)
            await start(client, queue, attempt, retries=True)
            await start(client, queue, attempt, retries=False)
            assert (await handle.describe()).run_id == first
            steps.release.set()
            assert await asyncio.wait_for(handle.result(), 30) == "done"
            assert len(steps.calls) == 1

            again = await start(client, queue, attempt, retries=False)
            assert await asyncio.wait_for(again.result(), 30) == "done"
            assert (await again.describe()).run_id != first
        assert len(steps.calls) == 2
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_step_out_of_tries_moves_on_to_the_next_step(client: Client, queue: str) -> None:
    """A failed step does not fail the run: the next step tries again."""
    attempt = uuid.uuid4().hex
    steps = Steps(None, False)
    try:
        async with serving(client, queue, steps):
            handle = await start(client, queue, attempt, retries=True)
            assert await asyncio.wait_for(handle.result(), 30) == "done"
        assert len(steps.calls) == 2
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_start_with_retries_turns_on_the_retries_of_a_run_it_joins(
    client: Client, queue: str
) -> None:
    """A read starts one step; an outcome heard during that step asks for retries. The
    run it joins steps three times, as a run started with retries does (#7281 review)."""
    attempt = uuid.uuid4().hex
    steps = Steps(True)
    steps.release.clear()
    try:
        async with serving(client, queue, steps):
            handle = await start(client, queue, attempt, retries=False)
            first = (await handle.describe()).run_id
            while not steps.calls:
                await asyncio.sleep(0.05)
            await start(client, queue, attempt, retries=True)
            assert (await handle.describe()).run_id == first
            steps.release.set()
            assert await asyncio.wait_for(handle.result(), 30) == "missing"
        assert len(steps.calls) == 3
    finally:
        await terminate_open_workflows(client, queue)
