"""`python -m scadbuddy.worker`: the render worker as a process, over a dev server."""

from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from scadbuddy.core.settings import Settings
from scadbuddy.render.job_models import Job
from scadbuddy.render.job_store import render_key
from scadbuddy.render.projection import JobProjection, workflow_id_for
from scadbuddy.worker import run_worker
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.support.temporal import temporal_client


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def _healthy(http: httpx.AsyncClient, worker: asyncio.Task[None]) -> dict[str, Any]:
    for _ in range(300):
        if worker.done():
            worker.result()  # raises what stopped it
            raise AssertionError("the worker exited before serving /healthz")
        try:
            response = await http.get("/healthz")
        except httpx.TransportError:
            await asyncio.sleep(0.1)
            continue
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body
    raise AssertionError("/healthz never answered")


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
async def test_the_worker_renders_a_job_and_serves_health_and_metrics(
    settings: Settings, model: str, pg_conninfo: str
) -> None:
    queue = f"t-{uuid.uuid4().hex[:8]}"
    build_id = f"test-{uuid.uuid4().hex[:8]}"
    cfg = settings.model_copy(update={"temporal_task_queue_render": queue, "revision": build_id})
    port = _free_port()
    params: dict[str, Any] = {"width": 12}
    job = Job(
        id=uuid.uuid4().hex,
        slug=model,
        params=params,
        inputs={"params": params},
        created_at=datetime.now(UTC),
    )
    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        async with temporal_client() as client:
            stop = asyncio.Event()
            worker = asyncio.create_task(
                run_worker(cfg, stop=stop, health_port=port, client=client)
            )
            try:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                    health = await _healthy(http, worker)
                    assert health == {"ok": True, "build_id": build_id, "task_queue": queue}

                    projection.submit(job, render_key(model, params, None))
                    await asyncio.wait_for(
                        client.execute_workflow(
                            TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue
                        ),
                        timeout=120,
                    )
                    metrics = (await http.get("/metrics")).text
            finally:
                stop.set()
                await asyncio.wait_for(worker, timeout=30)

        stored = projection.read(job.id)
    finally:
        projection.close()

    # The fake openscad writes no 3MF, so without the real binary the render fails.
    assert stored.state in ("done", "failed")
    if stored.state == "failed":
        assert stored.error
    assert "scadbuddy_render_duration_seconds" in metrics
    assert f'revision="{build_id}"' in metrics
