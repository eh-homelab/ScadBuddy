"""The app on Temporal: renders go through RenderService and an in-process worker on
the session's Temporal, and the routes read the projection."""

from __future__ import annotations

import time
import uuid
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

pytestmark = [
    pytest.mark.requires_postgres,
    pytest.mark.requires_temporal,
    pytest.mark.requires_git,
]


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The session's Temporal, as every API test's app, on a build of its own.
    cfg = settings.model_copy(
        update={
            "openscad": fake_3mf_openscad(tmp_path / "bin"),
            "revision": f"test-{uuid.uuid4().hex[:8]}",
        }
    )
    assert ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX).ensure_repo() is not None
    app = create_app(cfg)
    state: AppState = getattr(app.state, STATE_ATTR)
    service = state.render
    assert isinstance(service, RenderService)
    service.reconcile_after = 0.5
    service.reconcile_interval = 0.1

    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["temporal"] == {
            "address": cfg.temporal_address,
            "namespace": cfg.temporal_namespace,
            "task_queue": cfg.temporal_task_queue_render,
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


def test_the_api_boots_while_temporal_is_down_and_queues_renders_for_the_reconciler(
    settings: Settings, model: str
) -> None:
    cfg = settings.model_copy(
        update={
            # Nothing listens on port 1: every call fails to connect. No in-process
            # worker, whose client connects eagerly: the API's own is lazy.
            "temporal_address": "127.0.0.1:1",
            "temporal_task_queue_render": f"t-{uuid.uuid4().hex[:8]}",
            "temporal_worker_inprocess": False,
        }
    )
    app = create_app(cfg)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        accepted = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 3}})
        assert accepted.status_code == 202, accepted.text
        first = accepted.json()["job_id"]
        job = client.get(f"/api/v1/jobs/{first}").json()
        assert job["status"] == "pending"

        # The preview's next submit supersedes the first: its workflow cannot be
        # cancelled either, and that is no reason to refuse the new render.
        again = client.post(
            f"/api/v1/models/{model}/render",
            json={"params": {"width": 4}, "supersedes": first},
        )
        assert again.status_code == 202, again.text
        assert client.get(f"/api/v1/jobs/{first}").json()["status"] == "cancelled"
        assert client.get(f"/api/v1/jobs/{again.json()['job_id']}").json()["status"] == "pending"


def test_a_failing_start_on_temporal_still_closes_the_projection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `_start_render` raises past opening the projection is cleaned up, not
    leaked."""
    cfg = settings.model_copy(
        update={
            "temporal_address": "127.0.0.1:1",
            "temporal_task_queue_render": f"t-{uuid.uuid4().hex[:8]}",
        }
    )
    app = create_app(cfg)
    state: AppState = getattr(app.state, STATE_ATTR)
    assert state.projection is not None
    closed: list[str] = []
    close = state.projection.close

    def recording_close() -> None:
        closed.append("projection")
        close()

    async def refused(*_: object, **__: object) -> Client:
        raise RuntimeError("temporal refused the connection")

    monkeypatch.setattr(state.projection, "close", recording_close)
    monkeypatch.setattr("scadbuddy.main.connect", refused)

    with pytest.raises(RuntimeError, match="temporal refused the connection"), TestClient(app):
        pass

    assert closed == ["projection"]
    assert state.projection.pool.closed
