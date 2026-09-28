"""The app wired to Postgres (`SCADBUDDY_DATABASE_URL`), end to end: the lifespan
opens the store and migrates, renders are recorded in the table, `/metrics` reads
the queue from it."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.analyzers.decisions import PostgresDecisionStore
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.render.pg_store import PostgresJobStore
from tests.api.conftest import wait_for_job


@pytest.mark.requires_postgres
def test_the_app_queues_renders_in_postgres(
    settings: Settings, model: str, pg_conninfo: str
) -> None:
    app = create_app(settings.model_copy(update={"database_url": pg_conninfo}))
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


def test_without_a_database_url_the_queue_uses_files(settings: Settings) -> None:
    app = create_app(settings)
    assert not isinstance(app.state.scadbuddy.queue.store, PostgresJobStore)


@pytest.mark.requires_postgres
def test_analyzer_decisions_are_kept_in_postgres(settings: Settings, pg_conninfo: str) -> None:
    app = create_app(settings.model_copy(update={"database_url": pg_conninfo}))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/analyzers/decisions",
            json={"diagnostic_id": "SB1003", "kind": "ignore", "scope": {"kind": "global"}},
        )
        assert created.status_code == 201, created.text
        listed = client.get("/api/v1/analyzers/decisions").json()

    assert isinstance(app.state.scadbuddy.decisions, PostgresDecisionStore)
    assert [row["id"] for row in listed] == [created.json()["id"]]
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT kind FROM analyzer_decisions").fetchone()
    assert row is not None and row[0] == "ignore"


def test_without_a_database_url_there_is_no_decision_store(settings: Settings) -> None:
    # No file fallback: the routes that persist answer 503 (tests/api/test_analyzers.py).
    app = create_app(settings)
    assert app.state.scadbuddy.decisions is None
