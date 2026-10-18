"""The Temporal SDK's own metrics on a worker's /metrics (#3788)."""

from __future__ import annotations

import socket
import uuid
from datetime import timedelta

import httpx
import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from scadbuddy import worker as worker_module
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.settings import Settings
from scadbuddy.workflows.sdk_metrics import sdk_exposition, sdk_runtime
from tests.support.temporal import temporal_client


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@workflow.defn(sandboxed=False)
class _Echo:
    @workflow.run
    async def run(self, text: str) -> str:
        return text


@pytest.mark.requires_temporal
async def test_a_client_on_the_sdk_runtime_exports_workflow_task_latency() -> None:
    port = _free_port()
    async with temporal_client() as plain:
        client = await Client.connect(
            plain.service_client.config.target_host,
            namespace=plain.namespace,
            data_converter=pydantic_data_converter,
            runtime=sdk_runtime(port),
        )
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(client, task_queue=queue, workflows=[_Echo]):
            result = await client.execute_workflow(
                _Echo.run,
                "hi",
                id=f"echo-{uuid.uuid4().hex}",
                task_queue=queue,
                execution_timeout=timedelta(seconds=30),
            )
        assert result == "hi"
        text = (await sdk_exposition(port)).decode()
    # The series #3788 found missing from the worker's exposition.
    assert "temporal_workflow_task_execution_latency" in text
    assert "temporal_workflow_task_schedule_to_start_latency" in text


async def test_nothing_listening_reads_as_no_sdk_series() -> None:
    assert await sdk_exposition(_free_port()) == b""


async def test_metrics_appends_the_sdk_series_only_with_a_port(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []

    async def fake(port: int) -> bytes:
        asked.append(port)
        return b"temporal_workflow_task_execution_latency_count 3\n"

    monkeypatch.setattr(worker_module, "sdk_exposition", fake)
    metrics = Metrics()
    for sdk_port, expected in ((9464, True), (None, False)):
        app = worker_module._health_app(settings, metrics, None, "render", sdk_port)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as http:
            body = (await http.get("/metrics")).text
        assert "scadbuddy_" in body
        assert ("temporal_workflow_task_execution_latency_count 3" in body) is expected
    assert asked == [9464]


def test_the_runtime_is_built_once_per_port() -> None:
    port = _free_port()
    assert sdk_runtime(port) is sdk_runtime(port)


def test_port_zero_turns_the_sdk_metrics_off(settings: Settings) -> None:
    off = settings.model_copy(update={"temporal_sdk_metrics_port": 0})
    assert worker_module._sdk_runtime(off) == (None, None)
