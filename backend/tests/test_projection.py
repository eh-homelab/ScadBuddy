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
    SUPERSEDED_ERROR,
    Job,
    JobResult,
    PartInfo,
    QueueFullError,
    StepInfo,
    render_key,
)
from scadbuddy.render.projection import (
    CANCELLED_ERROR,
    LEGACY_RUNNING_ERROR,
    JobProjection,
    workflow_id_for,
)
from scadbuddy.render.schema import ParamValue
from scadbuddy.store.refs import BlobRefs

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
    assert "render_jobs_pending_key" in indexes
    assert MIGRATION_ID in applied


def test_submit_inserts_pending_with_its_workflow_id(projection: JobProjection) -> None:
    job = _job(width=1)
    submitted = projection.submit(job, render_key("demo", {"width": 1}, None))
    assert not submitted.coalesced
    stored = projection.read(job.id)
    assert stored.state == "pending"
    assert stored.workflow_id == f"render-{job.id}"


def test_a_job_without_inputs_is_stored_with_the_legacy_inputs(
    projection: JobProjection,
) -> None:
    job = _job(width=1)
    assert job.inputs == {}
    projection.submit(job, render_key("demo", {"width": 1}, None))
    assert projection.read(job.id).inputs == {"params": {"width": 1}, "v": 0}


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
    # #1323: it had started, so the error must not say "before it started".
    assert dropped.state == "cancelled" and dropped.error == "superseded by a newer render"
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


def _as_legacy(conninfo: str, *job_ids: str) -> None:
    """What a pre-Temporal release's queue wrote: rows that name no workflow."""
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            "UPDATE render_jobs SET workflow_id = NULL WHERE id = ANY(%s)", (list(job_ids),)
        )


def test_stale_pending_never_returns_a_legacy_row(
    pg_conninfo: str, projection: JobProjection
) -> None:
    # Until `adopt_legacy_pending` gives it a workflow id, the reconciler leaves it.
    legacy = projection.submit(_job(width=27), render_key("demo", {"width": 27}, None)).job
    _as_legacy(pg_conninfo, legacy.id)
    owned = projection.submit(_job(width=28), render_key("demo", {"width": 28}, None)).job
    assert [j.id for j in projection.stale_pending(older_than=0)] == [owned.id]


def test_boot_adopts_the_legacy_queues_pending_rows(
    projection: JobProjection, pg_conninfo: str
) -> None:
    ours, waiting = _job(n=6), _job(n=7)
    for job in (ours, waiting):
        projection.submit(job, render_key("demo", job.params, None))
    _as_legacy(pg_conninfo, waiting.id)

    assert projection.adopt_legacy_pending() == [waiting.id]

    assert projection.read(waiting.id).workflow_id == workflow_id_for(waiting.id)
    assert projection.read(ours.id).workflow_id == workflow_id_for(ours.id)
    # The reconciler starts it like any row of this path.
    assert waiting.id in [job.id for job in projection.stale_pending(older_than=0)]
    assert projection.adopt_legacy_pending() == []


def test_start_up_fails_the_rows_a_legacy_queue_left_running_only(
    announcing: JobProjection, pg_conninfo: str
) -> None:
    legacy_running, legacy_pending, on_temporal = _job(n=1), _job(n=2), _job(n=3)
    for job in (legacy_running, legacy_pending, on_temporal):
        announcing.submit(job, render_key("demo", job.params, None))
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
    # A legacy pending row is the reconciler's to start; a workflow's row is its own.
    assert announcing.read(legacy_pending.id).state == "pending"
    assert announcing.read(on_temporal.id).state == "running"
    assert "job.failed" in _kinds(pg_conninfo)


def test_a_result_stored_before_its_newer_fields_still_reads(
    projection: JobProjection, pg_conninfo: str
) -> None:
    """Rows outlive a deploy: a `result` written before `diagnostics` and
    `source_version` existed must still load, with their defaults."""
    job = _job()
    projection.submit(job, render_key("demo", job.params, None))
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


def test_the_latest_finished_render_is_never_an_arrange(projection: JobProjection) -> None:
    # An arrange row carries a source output's slug; the model's diagnostics must still
    # read its last render (final review M1).
    render = projection.submit(_job(width=2), render_key("demo", {"width": 2}, None)).job
    render.state, render.result, render.finished_at = "done", _result(), datetime.now(UTC)
    assert projection.finish(render)
    arrange = Job(
        id=uuid.uuid4().hex, slug="demo", kind="arrange", inputs={}, created_at=datetime.now(UTC)
    )
    arrange = projection.submit(arrange, "arrange-key").job
    arrange.state, arrange.error = "failed", "piece is not in the store"
    arrange.finished_at = datetime.now(UTC) + timedelta(seconds=5)
    assert projection.finish(arrange)
    latest = projection.latest_finished("demo")
    assert latest is not None and latest.id == render.id


def test_a_pending_arrange_holds_its_parts_from_insertion(
    projection: JobProjection, pg_conninfo: str
) -> None:
    # Deleting a source output while the arrange waits for a worker must not let a sweep
    # take the Parts it will place (final review M2); the prune releases the hold.
    refs = BlobRefs(projection.pool)
    refs.add("pieces/a", "output", "o-1")
    inputs = {"items": [{"part": {"piece_key": "pieces/a"}}, {"part": {"piece_key": "pieces/b"}}]}
    job = Job(
        id=uuid.uuid4().hex,
        slug="demo",
        kind="arrange",
        inputs=inputs,
        created_at=datetime.now(UTC),
    )
    first = projection.submit(job, "arrange-key")
    again = projection.submit(job.model_copy(update={"id": uuid.uuid4().hex}), "arrange-key")
    assert again.coalesced
    refs.drop_holder("output", "o-1")
    assert {"pieces/a", "pieces/b"} <= refs.referenced()
    with psycopg.connect(pg_conninfo) as conn:
        holders = conn.execute(
            "SELECT DISTINCT holder_id FROM blob_refs WHERE holder_kind = 'job'"
        ).fetchall()
    assert holders == [(first.job.id,)]
    first.job.state, first.job.finished_at = "failed", datetime.now(UTC) - timedelta(days=30)
    first.job.error = "x"
    assert projection.finish(first.job)
    projection.prune(1.0)
    assert not {"pieces/a", "pieces/b"} & refs.referenced()


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


def test_a_coalesced_submit_returns_the_first_callers_traceparent(
    projection: JobProjection,
) -> None:
    key = render_key("demo", {"width": 1}, None)
    first = _job(width=1)
    first.traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    projection.submit(first, key)
    joined = projection.submit(_job(width=1), key)
    assert joined.coalesced
    assert joined.job.traceparent == first.traceparent
    assert projection.read(first.id).traceparent == first.traceparent


def test_a_resubmit_that_supersedes_its_twin_adopts_a_traceparent_the_row_lacks(
    projection: JobProjection,
) -> None:
    """The `supersedes` coalesce, like ON CONFLICT, keeps the first traceparent and
    fills one in only where the row has none."""
    key = render_key("demo", {"width": 1}, None)
    first = _job(width=1)
    projection.submit(first, key)
    again = _job(width=1)
    again.traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    joined = projection.submit(again, key, supersedes=first.id)
    assert joined.coalesced
    assert joined.job.traceparent == again.traceparent
    assert projection.read(first.id).traceparent == again.traceparent
    later = _job(width=1)
    later.traceparent = "00-1af7651916cd43dd8448eb211c80319c-c7ad6b7169203331-01"
    projection.submit(later, key, supersedes=first.id)
    assert projection.read(first.id).traceparent == again.traceparent


def test_a_resubmit_of_a_running_twin_adopts_no_traceparent(projection: JobProjection) -> None:
    """A running row's workflow already has its parent; the coalesce writes nothing."""
    key = render_key("demo", {"width": 2}, None)
    first = _job(width=2)
    projection.submit(first, key)
    projection.mark_started(first.id)
    again = _job(width=2)
    again.traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    joined = projection.submit(again, key, supersedes=first.id)
    assert joined.coalesced
    assert joined.job.traceparent is None
    assert projection.read(first.id).traceparent is None
