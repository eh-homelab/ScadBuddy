"""The app with SCADBUDDY_TEMPORAL_ADDRESS set: renders go through RenderService and an
in-process worker on a dev server, and the routes read the projection."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.client import Client

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.history import ModelHistory
from scadbuddy.main import create_app
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.render.submit import RenderService
from tests.conftest import fake_3mf_openscad
from tests.support.temporal import current_address, temporal_client

pytestmark = [
    pytest.mark.requires_postgres,
    pytest.mark.requires_temporal,
    pytest.mark.requires_git,
]


@pytest.fixture
async def temporal() -> AsyncIterator[Client]:
    async with temporal_client() as client:
        yield client


def _settled(client: TestClient, job_id: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body: dict[str, Any] = client.get(f"/api/v1/jobs/{job_id}").json()
        if body["status"] in ("done", "failed", "cancelled"):
            return body
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} never settled")


def test_a_render_runs_on_temporal_and_the_routes_read_the_projection(
    settings: Settings,
    paths: DataPaths,
    model: str,
    temporal: Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = f"t-{uuid.uuid4().hex[:8]}"
    cfg = settings.model_copy(
        update={
            "openscad": fake_3mf_openscad(tmp_path / "bin"),
            "temporal_address": current_address(temporal),
            "temporal_namespace": temporal.namespace,
            "temporal_task_queue_render": queue,
            "temporal_worker_inprocess": True,
            "revision": f"test-{uuid.uuid4().hex[:8]}",
        }
    )
    assert ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX).ensure_repo() is not None
    app = create_app(cfg)
    state: AppState = getattr(app.state, STATE_ATTR)
    service = state.queue
    assert isinstance(service, RenderService)
    service.reconcile_after = 0.5
    service.reconcile_interval = 0.1

    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["temporal"] == {
            "address": cfg.temporal_address,
            "namespace": cfg.temporal_namespace,
            "task_queue": queue,
            "worker_inprocess": True,
        }

        # Temporal refuses the starts: both submits are answered from the one row,
        # which waits for the reconciler.
        async def unavailable(*_: object, **__: object) -> None:
            raise RuntimeError("temporal is down")

        with monkeypatch.context() as patched:
            patched.setattr(service.client, "start_workflow", unavailable)
            first = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
            again = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
        assert first.status_code == 202, first.text
        assert again.status_code == 202, again.text
        job_id = first.json()["job_id"]
        assert again.json()["job_id"] == job_id

        body = _settled(client, job_id)
        assert body["status"] == "done", body
        preview = client.get(f"/api/v1/jobs/{job_id}/preview.glb")
        assert preview.status_code == 200
