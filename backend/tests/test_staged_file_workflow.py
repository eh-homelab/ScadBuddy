"""The staged file's watcher (#6133, spec 2026-11-13 §6.5) on a real Temporal, its step
faked: the step's own rules are `tests/orders/storefront/test_model_requests.py`'s.

The deadlines here are seconds away on the wall clock: the dev server does not skip time,
and the workflow sleeps until whatever deadline its step answers."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.worker import Worker

from scadbuddy.orders.storefront.staged_files import TemporalWatcher
from scadbuddy.workflows.staged_file import (
    STAGED_FILE_WORKFLOW,
    STEP_ACTIVITY,
    StagedFile,
    StagedFileState,
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


def soon(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


class Steps:
    """A fake step that answers ``answers`` in turn (the last one for ever), each a
    callable so a deadline is read when the step runs; ``fail_first`` raises that many
    times first."""

    def __init__(self, *answers: Callable[[], StagedFileState], fail_first: int = 0) -> None:
        self.answers = list(answers)
        self.calls = 0
        self.answered = 0
        self.fail_first = fail_first

    def activity(self) -> Callable[[str], Awaitable[StagedFileState]]:
        @activity.defn(name=STEP_ACTIVITY)
        async def staged_file_step(upload_id: str) -> StagedFileState:
            self.calls += 1
            if self.fail_first:
                self.fail_first -= 1
                raise RuntimeError("the database is away")
            answer = self.answers[min(self.answered, len(self.answers) - 1)]
            self.answered += 1
            return answer()

        return staged_file_step


async def start(client: Client, queue: str, upload_id: str) -> WorkflowHandle[object, str]:
    await TemporalWatcher(client, queue).watch([upload_id])
    return client.get_workflow_handle(workflow_id(upload_id), result_type=str)


def ended(reason: str) -> Callable[[], StagedFileState]:
    return lambda: StagedFileState(phase="ended", reason=reason)


async def test_an_unclaimed_file_is_cleaned_up_at_its_deadline(client: Client, queue: str) -> None:
    upload_id = uuid.uuid4().hex
    steps = Steps(lambda: StagedFileState(phase="unclaimed", due=soon(2)), ended("unclaimed"))
    try:
        async with Worker(
            client, task_queue=queue, workflows=[StagedFile], activities=[steps.activity()]
        ):
            handle = await start(client, queue, upload_id)
            assert await asyncio.wait_for(handle.result(), 30) == "unclaimed"
        assert steps.calls == 2
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_signal_wakes_the_watcher_before_its_deadline(client: Client, queue: str) -> None:
    """A claim far from the 72 h deadline: the signal makes the step read the change now."""
    upload_id = uuid.uuid4().hex
    steps = Steps(lambda: StagedFileState(phase="claimed", due=soon(3600)), ended("removed"))
    try:
        async with Worker(
            client, task_queue=queue, workflows=[StagedFile], activities=[steps.activity()]
        ):
            handle = await start(client, queue, upload_id)
            while steps.calls < 1:
                await asyncio.sleep(0.1)
            await TemporalWatcher(client, queue).poke([upload_id], "unclaimed")
            assert await asyncio.wait_for(handle.result(), 30) == "removed"
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_signal_during_the_step_is_not_lost(client: Client, queue: str) -> None:
    """A line removed while the step runs: the next wait ends at once, not at the
    deadline an hour away."""
    upload_id = uuid.uuid4().hex
    in_step = asyncio.Event()
    signalled = asyncio.Event()
    answered = 0

    @activity.defn(name=STEP_ACTIVITY)
    async def staged_file_step(upload_id: str) -> StagedFileState:
        nonlocal answered
        answered += 1
        if answered > 1:
            return StagedFileState(phase="ended", reason="removed")
        in_step.set()
        await signalled.wait()
        return StagedFileState(phase="claimed", due=soon(3600))

    try:
        async with Worker(
            client, task_queue=queue, workflows=[StagedFile], activities=[staged_file_step]
        ):
            handle = await start(client, queue, upload_id)
            await asyncio.wait_for(in_step.wait(), 30)
            await TemporalWatcher(client, queue).poke([upload_id], "unclaimed")
            await asyncio.sleep(0.5)
            signalled.set()
            assert await asyncio.wait_for(handle.result(), 30) == "removed"
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_submitted_file_waits_with_no_deadline_until_it_is_done(
    client: Client, queue: str
) -> None:
    upload_id = uuid.uuid4().hex
    steps = Steps(lambda: StagedFileState(phase="submitted"), ended("declined"))
    try:
        async with Worker(
            client, task_queue=queue, workflows=[StagedFile], activities=[steps.activity()]
        ):
            handle = await start(client, queue, upload_id)
            await asyncio.sleep(2)
            # No deadline: still waiting, after one step.
            assert steps.calls == 1
            await TemporalWatcher(client, queue).poke([upload_id], "declined")
            assert await asyncio.wait_for(handle.result(), 30) == "declined"
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_failing_step_is_retried_until_the_clean_up_succeeds(
    client: Client, queue: str
) -> None:
    upload_id = uuid.uuid4().hex
    steps = Steps(ended("expired"), fail_first=1)
    try:
        async with Worker(
            client, task_queue=queue, workflows=[StagedFile], activities=[steps.activity()]
        ):
            handle = await start(client, queue, upload_id)
            assert await asyncio.wait_for(handle.result(), 60) == "expired"
        assert steps.calls == 2
    finally:
        await terminate_open_workflows(client, queue)


async def test_the_watcher_survives_a_worker_restart_mid_wait(client: Client, queue: str) -> None:
    upload_id = uuid.uuid4().hex
    steps = Steps(lambda: StagedFileState(phase="unclaimed", due=soon(4)), ended("unclaimed"))
    try:
        async with Worker(
            client, task_queue=queue, workflows=[StagedFile], activities=[steps.activity()]
        ):
            handle = await start(client, queue, upload_id)
            while steps.calls < 1:
                await asyncio.sleep(0.1)
        # No worker while the timer runs; a new one takes the run on.
        await asyncio.sleep(1)
        async with Worker(
            client, task_queue=queue, workflows=[StagedFile], activities=[steps.activity()]
        ):
            assert await asyncio.wait_for(handle.result(), 30) == "unclaimed"
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_second_start_joins_the_running_watcher(client: Client, queue: str) -> None:
    upload_id = uuid.uuid4().hex
    try:
        first = await client.start_workflow(
            STAGED_FILE_WORKFLOW,
            upload_id,
            id=workflow_id(upload_id),
            task_queue=queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
        await TemporalWatcher(client, queue).watch([upload_id])
        described = await client.get_workflow_handle(workflow_id(upload_id)).describe()
        assert described.run_id == first.result_run_id
        assert await TemporalWatcher(client, queue).running(upload_id) is True
        assert await TemporalWatcher(client, queue).running(uuid.uuid4().hex) is False
    finally:
        await terminate_open_workflows(client, queue)
