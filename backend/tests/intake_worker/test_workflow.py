"""``CustomerFileCheck`` (#6133, spec 2026-11-13 §7) on a real Temporal, its activities
faked: the check's verdict ends the run, and a check that cannot finish posts ``failed``."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from scadbuddy.intake_worker.wire import CHECK_ACTIVITY, FAILED_ACTIVITY, check_workflow_id
from scadbuddy.intake_worker.workflow import CustomerFileCheck
from scadbuddy.orders.storefront.intake import TemporalChecker
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
def queue() -> str:
    return f"customer-files-{uuid.uuid4().hex[:8]}"


class Fakes:
    def __init__(self, *, crash: bool = False) -> None:
        self.crash = crash
        self.checks = 0
        self.failed: list[str] = []

    def activities(self) -> list[Callable[[str], Awaitable[str]]]:
        @activity.defn(name=CHECK_ACTIVITY)
        async def check(source_id: str) -> str:
            self.checks += 1
            if self.crash:
                raise RuntimeError("the child crashed")
            return "ready"

        @activity.defn(name=FAILED_ACTIVITY)
        async def failed(source_id: str) -> str:
            self.failed.append(source_id)
            return "failed"

        return [check, failed]


async def _run(client: Client, queue: str, fakes: Fakes) -> str:
    source_id = str(uuid.uuid4())
    try:
        async with Worker(
            client, task_queue=queue, workflows=[CustomerFileCheck], activities=fakes.activities()
        ):
            await TemporalChecker(client, queue).start([source_id])
            handle = client.get_workflow_handle(check_workflow_id(source_id), result_type=str)
            return await asyncio.wait_for(handle.result(), 60)
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_check_that_finishes_ends_the_run(client: Client, queue: str) -> None:
    fakes = Fakes()
    assert await _run(client, queue, fakes) == "ready"
    assert (fakes.checks, fakes.failed) == (1, [])


async def test_a_check_that_cannot_finish_posts_failed(client: Client, queue: str) -> None:
    """`CHECK_RETRY`'s three attempts (about 30 s of backoff), then ``failed``."""
    fakes = Fakes(crash=True)
    assert await _run(client, queue, fakes) == "failed"
    assert fakes.checks == 3
    assert len(fakes.failed) == 1
