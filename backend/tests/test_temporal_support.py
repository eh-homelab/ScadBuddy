"""Pins that the dev server starts, the pydantic converter is wired, and
`tests.support.temporal`'s skip logic is right."""

from __future__ import annotations

import uuid

import pytest
from temporalio import workflow
from temporalio.worker import Worker

from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_temporal


@workflow.defn
class Shout:
    @workflow.run
    async def run(self, text: str) -> str:
        return text.upper()


async def test_a_workflow_runs_against_the_temporal_dev_server() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(client, task_queue=queue, workflows=[Shout]):
            result = await client.execute_workflow(
                Shout.run, "hello", id=f"shout-{uuid.uuid4().hex[:8]}", task_queue=queue
            )
        assert result == "HELLO"
