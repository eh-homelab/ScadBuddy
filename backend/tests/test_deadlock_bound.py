"""The test session's workflow workers take a larger deadlock bound than the SDK's 2 s
(#7443): a starved runner's pause is not a deadlock, and a real one still fails the task."""

from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from scadbuddy.workflows.pipelines import RenderPiece
from tests.conftest import TEST_DEADLOCK_SECONDS
from tests.support.temporal import temporal_client

pytestmark = [pytest.mark.requires_temporal, pytest.mark.asyncio]


async def test_a_workflow_worker_takes_the_tests_deadlock_bound() -> None:
    async with temporal_client() as client:
        worker = Worker(client, task_queue=f"t-{uuid.uuid4().hex[:8]}", workflows=[RenderPiece])
        assert worker._workflow_worker is not None
        assert worker._workflow_worker._deadlock_timeout_seconds == TEST_DEADLOCK_SECONDS
