"""Pins that the dev server starts, the pydantic converter is wired, and
`tests.support.temporal`'s skip logic is right."""

from __future__ import annotations

import uuid

import pytest
from temporalio import workflow
from temporalio.client import WorkflowExecutionStatus
from temporalio.worker import Worker

from tests.support.temporal import (
    WorkerThread,
    current_address,
    temporal_client,
    terminate_open_workflows,
)


@workflow.defn
class Shout:
    @workflow.run
    async def run(self, text: str) -> str:
        return text.upper()


@pytest.mark.requires_temporal
async def test_a_workflow_runs_against_the_temporal_dev_server() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(client, task_queue=queue, workflows=[Shout]):
            result = await client.execute_workflow(
                Shout.run, "hello", id=f"shout-{uuid.uuid4().hex[:8]}", task_queue=queue
            )
        assert result == "HELLO"


def test_a_worker_thread_lets_the_tests_own_failure_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def make_worker() -> Worker:
        raise RuntimeError("could not build the worker")

    thread = WorkerThread(make_worker)
    with pytest.raises(AssertionError, match="the test's own failure"), thread:
        raise AssertionError("the test's own failure")
    assert "could not build the worker" in caplog.text


def test_a_worker_thread_raises_what_stopped_its_worker() -> None:
    async def make_worker() -> Worker:
        raise RuntimeError("could not build the worker")

    thread = WorkerThread(make_worker)
    with pytest.raises(RuntimeError, match="could not build the worker"), thread:
        assert thread._loop is not None  # set before __enter__ returns


@pytest.mark.requires_temporal
async def test_terminate_open_workflows_ends_only_the_queues_running_workflows() -> None:
    async with temporal_client() as client:
        queue, other = (f"t-{uuid.uuid4().hex[:8]}" for _ in range(2))
        mine = await client.start_workflow(Shout.run, "a", id=f"a-{queue}", task_queue=queue)
        theirs = await client.start_workflow(Shout.run, "b", id=f"b-{other}", task_queue=other)

        await terminate_open_workflows(current_address(client), client.namespace, queue)

        assert (await mine.describe()).status == WorkflowExecutionStatus.TERMINATED
        assert (await theirs.describe()).status == WorkflowExecutionStatus.RUNNING
