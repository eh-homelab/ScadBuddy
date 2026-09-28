"""Pins that the dev server starts, the pydantic converter is wired, and
`tests.support.temporal`'s skip logic is right."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

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
            result = await asyncio.wait_for(
                client.execute_workflow(
                    Shout.run, "hello", id=f"shout-{uuid.uuid4().hex[:8]}", task_queue=queue
                ),
                timeout=60,
            )
        assert result == "HELLO"


def test_a_worker_thread_raises_what_stopped_its_worker() -> None:
    async def make_worker() -> Worker:
        raise RuntimeError("could not build the worker")

    thread = WorkerThread(make_worker)
    with pytest.raises(RuntimeError, match="could not build the worker"), thread:
        assert thread._loop is not None  # set before __enter__ returns


def test_a_worker_thread_that_does_not_stop_fails_the_test() -> None:
    class _Hangs:
        """A worker whose shutdown outlives the join."""

        async def __aenter__(self) -> _Hangs:
            return self

        async def __aexit__(self, *_: Any) -> None:
            await asyncio.sleep(5)

    async def make_worker() -> Worker:
        return _Hangs()  # type: ignore[return-value]

    with (
        pytest.raises(RuntimeError, match=r"still running 0\.2 s after it was told to stop"),
        WorkerThread(make_worker, join_timeout=0.2),
    ):
        pass
