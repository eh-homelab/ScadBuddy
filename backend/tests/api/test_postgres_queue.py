"""The app wired to Postgres (`SCADBUDDY_DATABASE_URL`), end to end: the lifespan
opens the store and migrates, renders are recorded in the table, `/metrics` reads
the queue from it."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.render.pg_store import PostgresJobStore
from tests.api.conftest import wait_for_job


@pytest.mark.requires_postgres
def test_the_app_queues_renders_in_postgres(
    settings: Settings, model: str, pg_conninfo: str
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        accepted = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        # The fake openscad may or may not produce geometry; either way the job
        # settles, and it is Postgres that says so.
        settled = wait_for_job(client, job_id)
        metrics = client.get("/metrics").text

    assert isinstance(app.state.scadbuddy.queue.store, PostgresJobStore)
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT state FROM render_jobs WHERE id = %s", (job_id,)).fetchone()
    assert row is not None and row[0] == settled["status"]
    assert "scadbuddy_render_queue_depth 0.0" in metrics
