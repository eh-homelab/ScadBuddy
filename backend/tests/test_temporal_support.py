"""Pins that the dev server starts, the pydantic converter is wired, and
`tests.support.temporal`'s skip logic is right."""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest
from temporalio import workflow
from temporalio.client import WorkflowExecutionStatus
from temporalio.worker import Worker

from tests.support.temporal import (
    WorkerThread,
    WorkflowReaper,
    _store_dir,
    current_address,
    temporal_client,
    temporal_server,
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
            result = await asyncio.wait_for(
                client.execute_workflow(
                    Shout.run, "hello", id=f"shout-{uuid.uuid4().hex[:8]}", task_queue=queue
                ),
                timeout=60,
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
async def test_the_reaper_ends_only_the_queues_running_workflows_on_one_client() -> None:
    async with temporal_client() as client:
        queues = [f"t-{uuid.uuid4().hex[:8]}" for _ in range(3)]
        started = [
            await client.start_workflow(Shout.run, q, id=f"s-{q}", task_queue=q) for q in queues
        ]

        with WorkflowReaper(current_address(client), client.namespace) as reaper:
            first = reaper.client
            await asyncio.to_thread(reaper.terminate, queues[0])
            await asyncio.to_thread(reaper.terminate, queues[1])
            assert reaper.client is first  # one connection for every teardown

        statuses = [(await handle.describe()).status for handle in started]
        assert statuses == [
            WorkflowExecutionStatus.TERMINATED,
            WorkflowExecutionStatus.TERMINATED,
            WorkflowExecutionStatus.RUNNING,
        ]


@pytest.mark.requires_temporal
def test_the_session_dev_server_keeps_its_store_in_a_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In memory, the dev server's SQLite was lost mid-session under load ("interrupted",
    then "Namespace default is not found") and every later API test failed."""
    monkeypatch.setattr("tests.support.temporal.TEST_TEMPORAL_ADDRESS", None)
    with tempfile.TemporaryDirectory(dir=_store_dir()) as scratch:
        db = Path(scratch) / "temporal.db"
        with temporal_server(db_file=db):
            assert db.is_file()


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
