"""The app on Temporal: renders go through RenderService and an in-process worker on
the session's Temporal, and the routes read the projection."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.client import Client, ScheduleActionExecutionStartWorkflow
from temporalio.service import RPCError

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.operations import TEMPORAL_UNAVAILABLE_PROBLEM
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.history import ModelHistory
from scadbuddy.main import create_app
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.render.submit import RenderService
from scadbuddy.workflows.housekeeping import schedule_id_for
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

    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["temporal"] == {
            "address": cfg.temporal_address,
            "namespace": cfg.temporal_namespace,
            "task_queue": cfg.temporal_task_queue_render,
            "worker_inprocess": True,
        }

        # The answer comes after the workflow's first activity wrote the row (#1053).
        first = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
        assert first.status_code == 202, first.text
        job_id = first.json()["job_id"]
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200

        body = _settled(client, job_id)
        assert body["status"] == "done", body
        preview = client.get(f"/api/v1/jobs/{job_id}/preview.glb")
        assert preview.status_code == 200


def test_the_api_boots_while_temporal_is_down_and_a_render_is_a_503_with_no_job(
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
    state: AppState = getattr(app.state, STATE_ATTR)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        refused = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 3}})
        assert refused.status_code == 503, refused.text
        assert refused.json()["type"] == TEMPORAL_UNAVAILABLE_PROBLEM
        assert refused.headers["Retry-After"] == "5"
        assert state.projection is not None
        assert state.projection.list_jobs() == []


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


def test_the_api_sets_up_its_housekeeping_schedule_and_runs_it_once(
    settings: Settings, model: str
) -> None:
    """#1054: the sweeps are a Temporal Schedule on the `library` queue this process
    serves, triggered once at start."""
    settings = settings.model_copy(update={"asset_sweep_interval": 3600.0})
    app = create_app(settings)
    queue = settings.temporal_task_queue_library

    async def described() -> tuple[timedelta, str]:
        temporal = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        handle = temporal.get_schedule_handle(schedule_id_for(queue))
        async with asyncio.timeout(30):
            while True:
                try:
                    schedule = await handle.describe()
                except RPCError:
                    await asyncio.sleep(0.2)
                    continue
                if schedule.info.recent_actions:
                    break
                await asyncio.sleep(0.2)
        run = schedule.info.recent_actions[-1].action
        assert isinstance(run, ScheduleActionExecutionStartWorkflow)
        result = await temporal.get_workflow_handle(
            run.workflow_id, run_id=run.first_execution_run_id
        ).result()
        return schedule.schedule.spec.intervals[0].every, str(result)

    with TestClient(app):
        every, result = asyncio.run(described())
    assert every == timedelta(seconds=3600)
    assert result == "[]"  # every sweep ran, none failed
