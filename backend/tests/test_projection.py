"""The render_jobs projection: rows the API reads and the workflow writes in place."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import (
    CANCELLED_ERROR,
    Job,
    JobResult,
    PartInfo,
    QueueFullError,
    StepInfo,
    render_key,
)
from scadbuddy.render.projection import (
    CLOSED_ERROR,
    LEGACY_RUNNING_ERROR,
    LEGACY_UNSTARTED_ERROR,
    ORPHANED_ERROR,
    JobProjection,
    LegacyPendingError,
)
from scadbuddy.render.schema import ParamValue
from tests.support.renders import legacy_row as _row

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


def test_the_migrations_add_the_projection_and_drop_the_queues_lease(
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
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes"
                " WHERE tablename = 'render_jobs' AND schemaname = current_schema()"
            )
        }
        applied = {row[0] for row in conn.execute("SELECT id FROM scadbuddy_migrations")}
    assert {"workflow_id", "kind", "inputs", "pipeline_version", "steps"} <= columns
    assert "heartbeat_at" not in columns
    assert "render_jobs_running" not in indexes
    # Coalescing is the workflow's (#1053): rows are unique per execution. The pending
    # key stays while a pre-#1053 API may still insert against it (expand/contract).
    assert "render_jobs_pending_key" in indexes
    assert "render_jobs_execution" in indexes
    assert "workflow_run_id" in columns
    assert MIGRATION_ID in applied


def test_a_job_without_inputs_is_stored_with_the_legacy_inputs(
    projection: JobProjection,
) -> None:
    job = _job(width=1)
    assert job.inputs == {}
    _row(projection, job)
    assert projection.read(job.id).inputs == {"params": {"width": 1}, "v": 0}


def test_releasing_the_last_claim_cancels(projection: JobProjection) -> None:
    first = _row(projection, _job(width=1))
    projection.set_claims(first.id, 2)
    assert projection.release_claim(first.id, slug="demo") is None  # one claim left
    gone = projection.release_claim(first.id, slug="demo")
    assert gone is not None and gone.state == "cancelled"


def test_state_moves_forward_only(projection: JobProjection) -> None:
    job = _row(projection, _job(width=2))
    assert projection.mark_started(job.id) is not None
    assert projection.mark_started(job.id) is None  # already running
    job.state, job.result, job.finished_at = "done", _result(), datetime.now(UTC)
    assert projection.finish(job)
    assert not projection.finish(job)  # already settled


def test_a_late_running_projection_never_moves_a_settled_job_back(
    projection: JobProjection,
) -> None:
    job = _row(projection, _job(width=3))
    projection.mark_started(job.id)
    job.state, job.result, job.finished_at = "done", _result(), datetime.now(UTC)
    projection.finish(job)
    assert projection.mark_started(job.id) is None
    assert projection.read(job.id).state == "done"


def test_steps_are_stored_as_given(projection: JobProjection) -> None:
    job = _row(projection, _job(width=4))
    projection.set_steps(job.id, [StepInfo(name="render", state="running")])
    assert projection.read(job.id).steps == [StepInfo(name="render", state="running")]


def test_every_state_change_is_published_on_the_bus(pg_conninfo: str) -> None:
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=2, events=bus)
    store.open()
    try:
        job = _row(store, _job(width=6))
        store.mark_started(job.id)
        job.state, job.error, job.finished_at = "failed", "boom", datetime.now(UTC)
        store.finish(job)
        cancelled = _row(store, _job(width=9))
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


def test_finishing_as_cancelled_announces_superseded(pg_conninfo: str) -> None:
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=2, events=bus)
    store.open()
    try:
        job = _row(store, _job(width=18))
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
    job = _row(projection, _job(width=23))
    projection.mark_started(job.id)
    assert projection.release_claim(job.id, slug="demo") is not None
    job.state, job.error, job.finished_at = "cancelled", "cancelled", datetime.now(UTC)
    assert projection.finish(job)
    assert projection.read(job.id).error == CANCELLED_ERROR


def test_prune_removes_settled_rows_and_their_blob_refs(
    pg_conninfo: str, projection: JobProjection
) -> None:
    old = _row(projection, _job(width=7))
    projection.mark_started(old.id)
    old.state, old.error, old.finished_at = "failed", "x", datetime.now(UTC) - timedelta(days=2)
    projection.finish(old)
    fresh = _row(projection, _job(width=8))
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
    job = _row(projection, _job(width=10))
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ('k', 'job', %s)",
            (job.id,),
        )
    projection.delete(job.id)
    assert projection.list_jobs() == []
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM blob_refs").fetchone() == (0,)


def test_a_workflow_that_fails_before_starting_settles_from_pending(
    pg_conninfo: str, announcing: JobProjection
) -> None:
    job = _row(announcing, _job(width=21))
    job.state, job.error, job.finished_at = "failed", "pipeline did not load", datetime.now(UTC)
    assert announcing.finish(job)
    stored = announcing.read(job.id)
    assert stored.state == "failed" and stored.started_at is None
    assert _kinds(pg_conninfo) == ["job.pending", "job.failed"]


def test_the_cancellation_handler_writes_the_final_projection_once_announced(
    pg_conninfo: str, announcing: JobProjection
) -> None:
    job = _row(announcing, _job(width=22))
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


def _as_legacy(conninfo: str, *job_ids: str) -> None:
    """What a pre-Temporal release's queue wrote: rows that name no workflow."""
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            "UPDATE render_jobs SET workflow_id = NULL WHERE id = ANY(%s)", (list(job_ids),)
        )


def test_start_up_fails_the_rows_a_legacy_queue_left_running_only(
    announcing: JobProjection, pg_conninfo: str
) -> None:
    legacy_running, legacy_pending, on_temporal = _job(n=1), _job(n=2), _job(n=3)
    for job in (legacy_running, legacy_pending, on_temporal):
        _row(announcing, job)
    assert announcing.mark_started(on_temporal.id) is not None
    # What the legacy queue left: rows with no workflow, one of them claimed.
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "UPDATE render_jobs SET workflow_id = NULL WHERE id = ANY(%s)",
            ([legacy_running.id, legacy_pending.id],),
        )
        conn.execute(
            "UPDATE render_jobs SET state = 'running', started_at = now() WHERE id = %s",
            (legacy_running.id,),
        )

    failed = announcing.fail_legacy_running()

    assert [job.id for job in failed] == [legacy_running.id]
    stored = announcing.read(legacy_running.id)
    assert (stored.state, stored.error) == ("failed", LEGACY_RUNNING_ERROR)
    assert stored.finished_at is not None
    # A legacy pending row is `settle_legacy`'s; a workflow's row is its own.
    assert announcing.read(legacy_pending.id).state == "pending"
    assert announcing.read(on_temporal.id).state == "running"
    assert "job.failed" in _kinds(pg_conninfo)


def test_a_result_stored_before_its_newer_fields_still_reads(
    projection: JobProjection, pg_conninfo: str
) -> None:
    """Rows outlive a deploy: a `result` written before `diagnostics` and
    `source_version` existed must still load, with their defaults."""
    job = _job()
    _row(projection, job)
    projection.mark_started(job.id)
    assert projection.finish(job.model_copy(update={"state": "done", "result": _result()}))
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "UPDATE render_jobs SET result = result - 'diagnostics' - 'source_version'"
            " WHERE id = %s",
            (job.id,),
        )

    stored = projection.read(job.id).result

    assert stored is not None
    assert (stored.diagnostics, stored.source_version) == ([], "")


def test_prune_can_use_the_settled_index(pg_conninfo: str, projection: JobProjection) -> None:
    """#606: prune's predicate, `coalesce(finished_at, created_at)` over every settled
    state, has an index to use as settled rows accumulate."""
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("SET enable_seqscan = off")
        plan = "\n".join(
            row[0]
            for row in conn.execute(
                "EXPLAIN DELETE FROM render_jobs WHERE state IN ('done', 'failed', 'cancelled')"
                " AND coalesce(finished_at, created_at) < now()"
            )
        )
    assert "render_jobs_settled_at" in plan


def _accept(
    store: JobProjection, run_id: str, *, max_pending: int = 0, **params: ParamValue
) -> Job:
    job = _job("demo", **params)
    key = render_key("demo", job.params, None)
    return store.accept(
        job, key, workflow_id=f"render-{key}", run_id=run_id, max_pending=max_pending
    )


def test_accept_inserts_once_per_execution(pg_conninfo: str, announcing: JobProjection) -> None:
    first = _accept(announcing, "run-1", width=40)
    again = _accept(announcing, "run-1", width=40)
    assert again.id == first.id
    stored = announcing.read(first.id)
    assert stored.state == "pending" and stored.claims == 1
    assert stored.workflow_id == f"render-{render_key('demo', {'width': 40}, None)}"
    assert stored.workflow_run_id == "run-1"
    assert _kinds(pg_conninfo) == ["job.pending"]


def test_accept_counts_the_queue(projection: JobProjection) -> None:
    held = _accept(projection, "run-a", width=41)
    with pytest.raises(QueueFullError) as refused:
        _accept(projection, "run-b", max_pending=1, width=42)
    assert refused.value.depth == 1
    # A retried accept of an execution already in finds its row, full queue or not.
    assert _accept(projection, "run-a", max_pending=1, width=41).id == held.id


def test_two_executions_of_one_key_each_get_a_row(projection: JobProjection) -> None:
    first = _accept(projection, "run-1", width=43)
    second = _accept(projection, "run-2", width=43)
    assert first.id != second.id
    assert projection.read(second.id).state == "pending"
    # Only one run of `render-<key>` is open, so the other's pending row is an orphan
    # (its run closed before it ran): failed, so the pending key holds the new one.
    orphan = projection.read(first.id)
    assert (orphan.state, orphan.error) == ("failed", ORPHANED_ERROR)


def test_accept_waits_for_a_legacy_row_pending_on_the_same_key(
    projection: JobProjection,
) -> None:
    legacy = _row(projection, _job(width=47))
    with pytest.raises(LegacyPendingError) as waiting:
        _accept(projection, "run-1", width=47)
    assert waiting.value.job.id == legacy.id
    assert projection.read(legacy.id).state == "pending"
    # Once the older build runs it, the key is free.
    assert projection.mark_started(legacy.id) is not None
    assert projection.read(_accept(projection, "run-1", width=47).id).state == "pending"


def test_accept_fails_a_legacy_row_its_caller_found_orphaned(projection: JobProjection) -> None:
    legacy = _row(projection, _job(width=49))
    job = _job("demo", width=49)
    key = render_key("demo", job.params, None)
    ours = projection.accept(
        job, key, workflow_id=f"render-{key}", run_id="run-1", orphaned=legacy.id
    )
    assert projection.read(ours.id).state == "pending"
    stored = projection.read(legacy.id)
    assert (stored.state, stored.error) == ("failed", LEGACY_UNSTARTED_ERROR)


def test_set_claims_moves_only_an_unfinished_row(projection: JobProjection) -> None:
    job = _accept(projection, "run-1", width=44)
    projection.set_claims(job.id, 3)
    assert projection.read(job.id).claims == 3
    job.state = "done"
    assert projection.finish(job)
    projection.set_claims(job.id, 5)
    assert projection.read(job.id).claims == 3


def test_legacy_unsettled_and_fail_legacy(pg_conninfo: str, announcing: JobProjection) -> None:
    legacy, named, stalled, unnamed = _job(width=45), _job(width=48), _job(width=47), _job(width=49)
    for job in (legacy, named, stalled, unnamed):
        _row(announcing, job)
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "UPDATE render_jobs SET created_at = now() - interval '1 hour' WHERE id = ANY(%s)",
            ([legacy.id, named.id, stalled.id, unnamed.id],),
        )
        conn.execute("UPDATE render_jobs SET workflow_id = NULL WHERE id = %s", (legacy.id,))
        conn.execute("UPDATE render_jobs SET state = 'running' WHERE id = %s", (stalled.id,))
        # A pre-Temporal running row is `fail_legacy_running`'s.
        conn.execute(
            "UPDATE render_jobs SET state = 'running', workflow_id = NULL WHERE id = %s",
            (unnamed.id,),
        )
    ours = _accept(announcing, "run-1", width=46)
    stale = announcing.legacy_unsettled(timedelta(minutes=1))
    assert {job.id for job in stale} == {legacy.id, named.id, stalled.id}
    assert announcing.legacy_unsettled(timedelta(hours=2)) == []

    failed = announcing.fail_legacy([legacy.id, stalled.id, unnamed.id, ours.id])

    assert {job.id for job in failed} == {legacy.id, stalled.id}
    assert announcing.read(legacy.id).error == LEGACY_UNSTARTED_ERROR
    assert announcing.read(stalled.id).state == "failed"
    assert announcing.read(stalled.id).error == CLOSED_ERROR
    assert announcing.read(unnamed.id).state == "running"
    assert announcing.read(ours.id).state == "pending"
    assert announcing.read(named.id).state == "pending"
    assert [job.id for job in announcing.legacy_unsettled(timedelta(minutes=1))] == [named.id]
    assert _kinds(pg_conninfo)[-2:] == ["job.failed", "job.failed"]


def test_accept_writes_the_first_callers_traceparent(projection: JobProjection) -> None:
    job = _job("demo", width=50)
    job.traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    key = render_key("demo", job.params, None)
    accepted = projection.accept(job, key, workflow_id=f"render-{key}", run_id="run-1")
    assert accepted.traceparent == job.traceparent
    assert projection.read(accepted.id).traceparent == job.traceparent
