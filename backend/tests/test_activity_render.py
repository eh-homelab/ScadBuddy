"""Renders as activity (spec 2026-10-09 §3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg import Connection

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.core.events import Event
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


class _OrderedBus:
    """Records, at each publish, whether the job's activity row is already written in
    the publishing transaction."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, bool]] = []

    def publish_in(self, conn: Connection[Any], event: Event) -> None:
        job_id = getattr(event, "job_id", "")
        row = conn.execute("SELECT 1 FROM activity WHERE id = %s", (f"render:{job_id}",)).fetchone()
        self.seen.append((event.kind, row is not None))


@pytest.mark.requires_postgres
def test_the_activity_row_is_written_before_the_job_event(
    pg_conninfo: str, pg_pool: PgPool
) -> None:
    """A publish takes the event log's lock until commit, so a row written after it
    waits on others' rows while holding it: two transactions on one job's activity row
    deadlocked in CI (#3454's backend shard 2). The row comes first, as in the
    operations and print-run stores."""
    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        projection.activity = ActivityIndex(pg_pool, events=None)
        bus = _OrderedBus()
        projection.events = bus
        job = make_job()
        projection.accept(job, "key-1", workflow_id="render-key-1", run_id="run-1")
        projection.mark_started(job.id)

        assert bus.seen and all(written for _, written in bus.seen), bus.seen
    finally:
        projection.close()
