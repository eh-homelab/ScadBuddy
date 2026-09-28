"""The render_jobs projection: rows the API reads and the workflow writes in place."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import Job, JobResult, PartInfo, StepInfo
from scadbuddy.render.job_store import SUPERSEDED_ERROR, QueueFullError, render_key
from scadbuddy.render.pg_store import TEMPORAL_INTERRUPTED_ERROR, PostgresJobStore
from scadbuddy.render.projection import (
    CANCELLED_ERROR,
    LEGACY_INTERRUPTED_ERROR,
    JobProjection,
    workflow_id_for,
)
from scadbuddy.render.schema import ParamValue

pytestmark = pytest.mark.requires_postgres

MIGRATION_ID = "20260928T0900Z_render_jobs_projection"


def _job(slug: str = "demo", **params: ParamValue) -> Job:
    return Job(id=uuid.uuid4().hex, slug=slug, params=dict(params), created_at=datetime.now(UTC))


def _result() -> JobResult:
    return JobResult(
        model_3mf="blobs/k/model.3mf",
        preview_glb="blobs/k/preview.glb",
        parts=[PartInfo(name="Color 1", colour="#FF0000", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )


def _kinds(conninfo: str) -> list[str]:
    with psycopg.connect(conninfo) as conn:
        return [row[0] for row in conn.execute("SELECT kind FROM events ORDER BY seq")]


@pytest.fixture
def announcing(pg_conninfo: str) -> Iterator[JobProjection]:
    """A projection publishing on a real (unstarted) bus: its events land in `events`."""
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=2, events=bus)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


def test_the_migration_adds_the_projection_and_keeps_the_queue(
    pg_conninfo: str, projection: JobProjection
) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'render_jobs' AND table_schema = current_schema()"
            )
        }
        applied = {row[0] for row in conn.execute("SELECT id FROM scadbuddy_migrations")}
    assert {"workflow_id", "kind", "inputs", "pipeline_version", "steps"} <= columns
    assert "heartbeat_at" in columns
    assert MIGRATION_ID in applied


def test_submit_inserts_pending_with_its_workflow_id(projection: JobProjection) -> None:
    job = _job(width=1)
    submitted = projection.submit(job, render_key("demo", {"width": 1}, None))
    assert not submitted.coalesced
    stored = projection.read(job.id)
    assert stored.state == "pending"
    assert stored.workflow_id == f"render-{job.id}"
    assert stored.inputs == {"params": {"width": 1}}


def test_an_identical_pending_submit_coalesces(projection: JobProjection) -> None:
    key = render_key("demo", {"width": 1}, None)
    first = projection.submit(_job(width=1), key)
    second = projection.submit(_job(width=1), key)
    assert second.coalesced and second.job.id == first.job.id
    assert projection.read(first.job.id).claims == 2


def test_releasing_the_last_claim_cancels(projection: JobProjection) -> None:
    key = render_key("demo", {"width": 1}, None)
    first = projection.submit(_job(width=1), key)
    projection.submit(_job(width=1), key)
    assert projection.release_claim(first.job.id, slug="demo") is None  # one claim left
    gone = projection.release_claim(first.job.id, slug="demo")
    assert gone is not None and gone.state == "cancelled"


def test_state_moves_forward_only(projection: JobProjection) -> None:
    job = projection.submit(_job(width=2), render_key("demo", {"width": 2}, None)).job
    assert projection.mark_started(job.id) is not None
    assert projection.mark_started(job.id) is None  # already running
    job.state, job.result, job.finished_at = "done", _result(), datetime.now(UTC)
    assert projection.finish(job)
    assert not projection.finish(job)  # already settled


def test_a_late_running_projection_never_moves_a_settled_job_back(
    projection: JobProjection,
) -> None:
    job = projection.submit(_job(width=3), render_key("demo", {"width": 3}, None)).job
    projection.mark_started(job.id)
    job.state, job.result, job.finished_at = "done", _result(), datetime.now(UTC)
    projection.finish(job)
    assert projection.mark_started(job.id) is None
    assert projection.read(job.id).state == "done"


def test_steps_are_stored_as_given(projection: JobProjection) -> None:
    job = projection.submit(_job(width=4), render_key("demo", {"width": 4}, None)).job
    projection.set_steps(job.id, [StepInfo(name="render", state="running")])
    assert projection.read(job.id).steps == [StepInfo(name="render", state="running")]


def test_stale_pending_is_what_the_reconciler_restarts(projection: JobProjection) -> None:
    job = projection.submit(_job(width=5), render_key("demo", {"width": 5}, None)).job
    assert projection.stale_pending(older_than=3600) == []
    assert [j.id for j in projection.stale_pending(older_than=0)] == [job.id]
    projection.mark_started(job.id)
    assert projection.stale_pending(older_than=0) == []


def test_every_state_change_is_published_on_the_bus(pg_conninfo: str) -> None:
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=2, events=bus)
    store.open()
    try:
        job = store.submit(_job(width=6), render_key("demo", {"width": 6}, None)).job
        store.mark_started(job.id)
        job.state, job.error, job.finished_at = "failed", "boom", datetime.now(UTC)
        store.finish(job)
        cancelled = store.submit(_job(width=9), render_key("demo", {"width": 9}, None)).job
        store.release_claim(cancelled.id, slug="demo")
    finally:
        store.close()
    assert _kinds(pg_conninfo) == [
        "job.pending",
        "job.running",
        "job.failed",
        "job.pending",
        "job.superseded",
    ]


def test_a_newer_render_supersedes_the_pending_one(pg_conninfo: str) -> None:
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=2, events=bus)
    store.open()
    try:
        first = store.submit(_job(width=12), render_key("demo", {"width": 12}, None)).job
        second = _job(width=13)
        submitted = store.submit(
            second, render_key("demo", {"width": 13}, None), supersedes=first.id
        )
        assert submitted.superseded is not None and submitted.superseded.id == first.id
        dropped = store.read(first.id)
        assert dropped.state == "cancelled" and dropped.error == SUPERSEDED_ERROR
        assert store.read(second.id).state == "pending"
    finally:
        store.close()
    assert _kinds(pg_conninfo) == ["job.pending", "job.superseded", "job.pending"]


def test_a_full_queue_refuses_a_new_render_and_supersedes_nothing(
    projection: JobProjection,
) -> None:
    first = projection.submit(_job(width=14), render_key("demo", {"width": 14}, None)).job
    with pytest.raises(QueueFullError):
        projection.submit(_job(width=15), render_key("demo", {"width": 15}, None), max_pending=1)
    # The supersede frees `first`'s place, but `other` still fills the queue: the
    # refusal rolls the supersede back.
    other = projection.submit(_job(width=16), render_key("demo", {"width": 16}, None)).job
    with pytest.raises(QueueFullError):
        projection.submit(
            _job(width=17),
            render_key("demo", {"width": 17}, None),
            supersedes=first.id,
            max_pending=1,
        )
    assert projection.read(first.id).state == "pending"
    assert {job.id for job in projection.list_jobs()} == {first.id, other.id}


def test_finishing_as_cancelled_announces_superseded(pg_conninfo: str) -> None:
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=2, events=bus)
    store.open()
    try:
        job = store.submit(_job(width=18), render_key("demo", {"width": 18}, None)).job
        store.mark_started(job.id)
        job.state, job.error, job.finished_at = "cancelled", "withdrawn", datetime.now(UTC)
        assert store.finish(job)
    finally:
        store.close()
    assert _kinds(pg_conninfo) == ["job.pending", "job.running", "job.superseded"]


def test_a_cancelled_projection_keeps_the_reason_the_api_stored(
    projection: JobProjection,
) -> None:
    """The workflow read the row before the API cancelled it: its error must not win."""
    job = projection.submit(_job(width=23), render_key("demo", {"width": 23}, None)).job
    projection.mark_started(job.id)
    assert projection.release_claim(job.id, slug="demo") is not None
    job.state, job.error, job.finished_at = "cancelled", "cancelled", datetime.now(UTC)
    assert projection.finish(job)
    assert projection.read(job.id).error == CANCELLED_ERROR


def test_prune_removes_settled_rows_and_their_blob_refs(
    pg_conninfo: str, projection: JobProjection
) -> None:
    old = projection.submit(_job(width=7), render_key("demo", {"width": 7}, None)).job
    projection.mark_started(old.id)
    old.state, old.error, old.finished_at = "failed", "x", datetime.now(UTC) - timedelta(days=2)
    projection.finish(old)
    fresh = projection.submit(_job(width=8), render_key("demo", {"width": 8}, None)).job
    with psycopg.connect(pg_conninfo) as conn:
        for job_id in (old.id, fresh.id):
            conn.execute(
                "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ('k', 'job', %s)",
                (job_id,),
            )
    assert projection.prune(ttl=86400) == [old.id]
    assert projection.read(fresh.id).state == "pending"
    with psycopg.connect(pg_conninfo) as conn:
        holders = [row[0] for row in conn.execute("SELECT holder_id FROM blob_refs")]
    assert holders == [fresh.id]


def test_delete_removes_the_row_and_its_blob_refs(
    pg_conninfo: str, projection: JobProjection
) -> None:
    job = projection.submit(_job(width=10), render_key("demo", {"width": 10}, None)).job
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ('k', 'job', %s)",
            (job.id,),
        )
    projection.delete(job.id)
    assert projection.list_jobs() == []
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM blob_refs").fetchone() == (0,)


def test_the_legacy_queue_still_works_on_the_migrated_table(
    pg_conninfo: str, projection: JobProjection, tmp_path: Path
) -> None:
    store = PostgresJobStore(pg_conninfo, DataPaths(tmp_path), pool_size=2)
    store.open()
    try:
        job = _job(width=11)
        store.submit(job, render_key("demo", {"width": 11}, None))
        claimed = store.claim()
        assert claimed is not None and claimed.id == job.id
        claimed.state, claimed.result, claimed.finished_at = "done", _result(), datetime.now(UTC)
        assert store.finish(claimed)
        assert store.read(job.id).state == "done"
    finally:
        store.close()


def test_a_newer_render_supersedes_a_running_one(
    pg_conninfo: str, announcing: JobProjection
) -> None:
    first = announcing.submit(_job(width=19), render_key("demo", {"width": 19}, None)).job
    announcing.mark_started(first.id)
    submitted = announcing.submit(
        _job(width=20), render_key("demo", {"width": 20}, None), supersedes=first.id
    )
    assert submitted.superseded is not None and submitted.superseded.id == first.id
    dropped = announcing.read(first.id)
    assert dropped.state == "cancelled" and dropped.error == SUPERSEDED_ERROR
    assert _kinds(pg_conninfo) == ["job.pending", "job.running", "job.superseded", "job.pending"]


def test_a_workflow_that_fails_before_starting_settles_from_pending(
    pg_conninfo: str, announcing: JobProjection
) -> None:
    job = announcing.submit(_job(width=21), render_key("demo", {"width": 21}, None)).job
    job.state, job.error, job.finished_at = "failed", "pipeline did not load", datetime.now(UTC)
    assert announcing.finish(job)
    stored = announcing.read(job.id)
    assert stored.state == "failed" and stored.started_at is None
    assert announcing.stale_pending(older_than=0) == []
    assert _kinds(pg_conninfo) == ["job.pending", "job.failed"]


def test_the_cancellation_handler_writes_the_final_projection_once_announced(
    pg_conninfo: str, announcing: JobProjection
) -> None:
    job = announcing.submit(_job(width=22), render_key("demo", {"width": 22}, None)).job
    announcing.mark_started(job.id)
    assert announcing.release_claim(job.id, slug="demo") is not None
    job.state, job.error, job.finished_at = "cancelled", "", datetime.now(UTC)
    job.steps = [StepInfo(name="render", state="failed")]
    job.log_tail = ["cancelled mid-render"]
    assert announcing.finish(job)
    stored = announcing.read(job.id)
    assert stored.state == "cancelled" and stored.error == CANCELLED_ERROR
    assert stored.steps == job.steps and stored.log_tail == job.log_tail
    assert _kinds(pg_conninfo) == ["job.pending", "job.running", "job.superseded"]


def test_a_cache_hit_is_recorded_done_without_a_workflow(
    pg_conninfo: str, announcing: JobProjection
) -> None:
    waiting = announcing.submit(_job(width=23), render_key("demo", {"width": 23}, None)).job
    hit = _job(width=24)
    hit.state, hit.result = "done", _result()
    hit.started_at = hit.finished_at = datetime.now(UTC)
    submitted = announcing.submit(hit, render_key("demo", {"width": 24}, None), max_pending=1)
    assert submitted.cached and not submitted.coalesced
    stored = announcing.read(hit.id)
    assert stored.state == "done" and stored.result == hit.result
    assert stored.workflow_id is None and stored.finished_at is not None
    assert announcing.read(waiting.id).state == "pending"
    assert _kinds(pg_conninfo) == ["job.pending", "job.done"]


def test_the_legacy_queue_never_claims_a_workflow_row(
    pg_conninfo: str, projection: JobProjection, tmp_path: Path
) -> None:
    projection.submit(_job(width=25), render_key("demo", {"width": 25}, None))
    store = PostgresJobStore(pg_conninfo, DataPaths(tmp_path), pool_size=2)
    store.open()
    try:
        assert store.claim() is None
    finally:
        store.close()


def test_a_legacy_submit_writes_inputs(
    pg_conninfo: str, projection: JobProjection, tmp_path: Path
) -> None:
    store = PostgresJobStore(pg_conninfo, DataPaths(tmp_path), pool_size=2)
    store.open()
    try:
        job = _job(width=26)
        store.submit(job, render_key("demo", {"width": 26}, None))
    finally:
        store.close()
    assert projection.read(job.id).inputs == {"params": {"width": 26}}


def test_stale_pending_never_returns_a_legacy_row(
    pg_conninfo: str, projection: JobProjection, tmp_path: Path
) -> None:
    # During a rolling deploy the legacy queue owns its pending rows (no workflow_id);
    # the reconciler must not start a workflow for one.
    store = PostgresJobStore(pg_conninfo, DataPaths(tmp_path), pool_size=2)
    store.open()
    try:
        store.submit(_job(width=27), render_key("demo", {"width": 27}, None))
    finally:
        store.close()
    owned = projection.submit(_job(width=28), render_key("demo", {"width": 28}, None)).job
    assert [j.id for j in projection.stale_pending(older_than=0)] == [owned.id]


def test_a_legacy_cache_hit_returns_the_inputs_it_wrote(
    pg_conninfo: str, projection: JobProjection, tmp_path: Path
) -> None:
    store = PostgresJobStore(pg_conninfo, DataPaths(tmp_path), pool_size=2)
    store.open()
    try:
        hit = _job(width=29)
        hit.state, hit.result = "done", _result()
        hit.started_at = hit.finished_at = datetime.now(UTC)
        submitted = store.submit(hit, render_key("demo", {"width": 29}, None))
        assert submitted.cached
        assert submitted.job.inputs == {"params": {"width": 29}}
        assert store.read(hit.id).inputs == submitted.job.inputs
    finally:
        store.close()


def test_a_legacy_read_reports_the_claim_count(
    pg_conninfo: str, projection: JobProjection, tmp_path: Path
) -> None:
    store = PostgresJobStore(pg_conninfo, DataPaths(tmp_path), pool_size=2)
    store.open()
    try:
        key = render_key("demo", {"width": 30}, None)
        first = store.submit(_job(width=30), key).job
        assert store.submit(_job(width=30), key).job.id == first.id
        assert store.read(first.id).claims == 2
    finally:
        store.close()


# ── flipping SCADBUDDY_TEMPORAL_ADDRESS across a restart (final review I4) ──────


def test_temporal_boot_fails_the_legacy_queues_running_rows_only(
    announcing: JobProjection, pg_conninfo: str, paths: DataPaths
) -> None:
    legacy = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    legacy.open()
    try:
        running, waiting = _job(n=1), _job(n=2)
        legacy.submit(running, render_key("demo", running.params, None))
        claimed = legacy.claim()
        assert claimed is not None and claimed.id == running.id
        legacy.submit(waiting, render_key("demo", waiting.params, None))
    finally:
        legacy.close()

    failed = announcing.fail_legacy_running(LEGACY_INTERRUPTED_ERROR)

    assert [job.id for job in failed] == [running.id]
    stored = announcing.read(running.id)
    assert (stored.state, stored.error) == ("failed", LEGACY_INTERRUPTED_ERROR)
    assert stored.finished_at is not None
    # A legacy pending row is the reconciler's to start.
    assert announcing.read(waiting.id).state == "pending"
    assert "job.failed" in _kinds(pg_conninfo)


def test_legacy_boot_adopts_temporal_pending_rows_and_fails_running_ones(
    projection: JobProjection, pg_conninfo: str, paths: DataPaths
) -> None:
    running, waiting = _job(n=3), _job(n=4)
    projection.submit(running, render_key("demo", running.params, None))
    assert projection.mark_started(running.id) is not None
    projection.submit(waiting, render_key("demo", waiting.params, None))

    legacy = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    legacy.open()
    try:
        abandoned = legacy.abandon_orphans()
        claimed = legacy.claim()
    finally:
        legacy.close()

    assert [job.id for job in abandoned] == [running.id]
    stored = projection.read(running.id)
    assert (stored.state, stored.error) == ("failed", TEMPORAL_INTERRUPTED_ERROR)
    # The pending one is the legacy queue's now: its claim takes it.
    assert claimed is not None and claimed.id == waiting.id


def test_a_legacy_row_a_workflow_adopted_is_not_failed_as_a_legacy_running_row(
    projection: JobProjection, pg_conninfo: str, paths: DataPaths
) -> None:
    legacy = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    legacy.open()
    try:
        adopted = _job(n=5)
        legacy.submit(adopted, render_key("demo", adopted.params, None))
    finally:
        legacy.close()

    # The reconciler started `render-<id>` for it, and the workflow marked it running.
    started = projection.mark_started(adopted.id)

    assert started is not None and started.workflow_id == workflow_id_for(adopted.id)
    assert projection.fail_legacy_running(LEGACY_INTERRUPTED_ERROR) == []
    assert projection.read(adopted.id).state == "running"


def test_temporal_boot_adopts_the_legacy_queues_pending_rows(
    projection: JobProjection, pg_conninfo: str, paths: DataPaths
) -> None:
    ours = _job(n=6)
    projection.submit(ours, render_key("demo", ours.params, None))
    legacy = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    legacy.open()
    try:
        waiting = _job(n=7)
        legacy.submit(waiting, render_key("demo", waiting.params, None))
    finally:
        legacy.close()
    assert projection.read(waiting.id).workflow_id is None

    assert projection.adopt_legacy_pending() == [waiting.id]

    assert projection.read(waiting.id).workflow_id == workflow_id_for(waiting.id)
    assert projection.read(ours.id).workflow_id == workflow_id_for(ours.id)
    # The reconciler starts it like any row of this path.
    assert waiting.id in [job.id for job in projection.stale_pending(older_than=0)]
    assert projection.adopt_legacy_pending() == []
