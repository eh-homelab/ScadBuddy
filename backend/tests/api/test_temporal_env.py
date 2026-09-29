"""The API tests' `_clean_env` scrubs every SCADBUDDY_* variable, the test ones too;
`tests.support.temporal` read them at import, so a `requires_temporal` test here still
reaches the Temporal it was configured with."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid

import pytest
from temporalio import workflow
from temporalio.worker import Worker

from tests.support import temporal
from tests.support.temporal import (
    TEST_TEMPORAL_ADDRESS_ENV,
    TEST_TEMPORAL_DEV_SERVER_ENV,
    temporal_client,
)


@workflow.defn
class Echo:
    @workflow.run
    async def run(self, text: str) -> str:
        return text


@pytest.mark.requires_temporal
async def test_the_configured_temporal_survives_the_env_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if temporal.TEST_TEMPORAL_ADDRESS is None and temporal.TEST_TEMPORAL_DEV_SERVER is None:
        pytest.skip(f"set {TEST_TEMPORAL_ADDRESS_ENV} or {TEST_TEMPORAL_DEV_SERVER_ENV}")
    assert TEST_TEMPORAL_ADDRESS_ENV not in os.environ
    assert TEST_TEMPORAL_DEV_SERVER_ENV not in os.environ
    # Not by luck from PATH: only what was read at import can find the server.
    monkeypatch.setattr(shutil, "which", lambda *_: None)

    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(client, task_queue=queue, workflows=[Echo]):
            result = await asyncio.wait_for(
                client.execute_workflow(
                    Echo.run, "hi", id=f"echo-{uuid.uuid4().hex[:8]}", task_queue=queue
                ),
                timeout=60,
            )
    assert result == "hi"
