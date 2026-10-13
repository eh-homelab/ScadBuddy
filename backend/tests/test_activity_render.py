"""Renders as activity (spec 2026-10-09 §3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.render.activity import row_of
from scadbuddy.render.job_models import Job, JobState, JobTableKind
from scadbuddy.render.projection import JobProjection
from tests.conftest import PgPool


def make_job(*, state: JobState = "pending", kind: JobTableKind = "render") -> Job:
    return Job.model_validate(
        {
            "id": uuid.uuid4().hex,
            "slug": "demo",
            "state": state,
            "kind": kind,
            "created_at": datetime.now(UTC),
            "model_version": "abc123",
        }
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("pending", "queued"),
        ("running", "running"),
        ("done", "succeeded"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_status_mapping(state: JobState, expected: str) -> None:
    assert row_of(make_job(state=state)).status == expected


def test_arrange_is_its_own_kind() -> None:
    assert row_of(make_job(state="done", kind="arrange")).kind == "arrange"


def test_row_shape() -> None:
    job = make_job()
    row = row_of(job)
    assert row.id == f"render:{job.id}"
    assert row.kind == "render" and row.subject == "demo"
    assert row.source == "render_jobs" and row.source_id == job.id
    assert row.facets == {"slug": "demo", "model_version": "abc123"}


@pytest.mark.requires_postgres
def test_projection_writes_activity(pg_conninfo: str, pg_pool: PgPool) -> None:
    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        projection.activity = ActivityIndex(pg_pool, events=None)
        job = make_job()
        projection.accept(job, "key-1", workflow_id="render-key-1", run_id="run-1")
        with pg_pool.connection() as conn:
            got = conn.execute("SELECT status, kind FROM activity").fetchall()
        assert [(r["status"], r["kind"]) for r in got] == [("queued", "render")]
        started = projection.mark_started(job.id)
        assert started is not None
        with pg_pool.connection() as conn:
            assert conn.execute("SELECT status FROM activity").fetchone() == {"status": "running"}
    finally:
        projection.close()
