"""Pins that the dev server starts, the pydantic converter is wired, and
`tests.support.temporal`'s skip logic is right."""

from __future__ import annotations

import uuid

import pytest
from temporalio import workflow
from temporalio.worker import Worker

from tests.support.temporal import WorkerThread, temporal_client


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
