"""`python -m scadbuddy.worker`: the render worker as a process, over a dev server."""

from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.history import ModelHistory
from scadbuddy.render.job_models import Job
from scadbuddy.render.job_store import render_key
from scadbuddy.render.projection import JobProjection, workflow_id_for
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.worker import _wait_drained, run_worker
from scadbuddy.workflows.models import piece_key
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.conftest import fake_3mf_openscad
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
@pytest.mark.requires_git
async def test_the_worker_renders_a_job_and_serves_health_and_metrics(
    settings: Settings, paths: DataPaths, model: str, pg_conninfo: str, tmp_path: Path
) -> None:
    queue = f"t-{uuid.uuid4().hex[:8]}"
    build_id = f"test-{uuid.uuid4().hex[:8]}"
    cfg = settings.model_copy(
        update={
            "openscad": fake_3mf_openscad(tmp_path / "bin"),
            "temporal_task_queue_render": queue,
            "revision": build_id,
        }
    )
    # The template at a revision, so the piece is keyed by one.
    revision = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX).ensure_repo()
    assert revision is not None
    port = _free_port()
    params: dict[str, Any] = {"width": 12}
    job = Job(
        id=uuid.uuid4().hex,
        slug=model,
        params=params,
        inputs={"params": params},
        model_version=revision,
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

                    projection.submit(job, render_key(model, params, revision))
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

    assert stored.state == "done", stored.error
    assert stored.result is not None
    key = piece_key(model, revision, "model.scad", params)
    assert (cfg.data_dir / "blobs" / key).is_dir()
    # The worker's own bus published the job's events.
    with psycopg.connect(pg_conninfo) as conn:
        kinds = {
            row[0]
            for row in conn.execute(
                "SELECT kind FROM events WHERE payload->>'job_id' = %s", (job.id,)
            )
        }
    assert {"job.running", "job.done"} <= kinds
    assert "scadbuddy_render_duration_seconds" in metrics
    assert f'revision="{build_id}"' in metrics


# ── the drain after stop ───────────────────────────────────────────────────────


async def test_the_drain_polls_until_the_version_is_drained() -> None:
    answers = iter([False, False, True])
    calls = 0

    async def drained() -> bool:
        nonlocal calls
        calls += 1
        return next(answers)

    assert await _wait_drained(drained, timeout=5, poll=0.01)
    assert calls == 3


async def test_the_drain_gives_up_at_its_bound() -> None:
    calls = 0

    async def drained() -> bool:
        nonlocal calls
        calls += 1
        return False

    assert not await asyncio.wait_for(_wait_drained(drained, timeout=0.1, poll=0.01), 5)
    assert calls > 1
