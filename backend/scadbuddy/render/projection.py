"""`render_jobs` as a projection (spec 2026-09-27 §3.2).

The API inserts a row and starts the workflow named by it; the workflow's `project`
activity moves `state` forward in place. Every write is guarded by the state it
expects, so a retried activity cannot move a job backwards, and every state change
publishes its ``job.*`` event on the bus inside its own transaction
(`PgNotifyEventBus.publish_in`), so it is heard on commit or not at all.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.core.events import JobEvent, JobKind
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.render.inputs import legacy_inputs
from scadbuddy.render.job_models import (
    SUPERSEDED_ERROR,
    Job,
    JobNotFoundError,
    QueueCounts,
    QueueFullError,
    StepInfo,
    Submitted,
    now,
)
from scadbuddy.render.pg_store import TransactionalEvents, migrate

logger = logging.getLogger(__name__)

CANCELLED_ERROR = "cancelled: every request for it was withdrawn"
LEGACY_RUNNING_ERROR = "failed: the upgrade to Temporal-backed rendering left it unfinished"

PROJECTION_COLUMNS = (
    "id",
    "slug",
    "params",
    "model_version",
    "state",
    "created_at",
    "started_at",
    "finished_at",
    "log_tail",
    "error",
    "result",
    "diagnostics",
    "diagnostics_dropped",
    "warnings",
    "inputs",
    "claims",
    "kind",
    "pipeline_version",
    "steps",
    "workflow_id",
    "traceparent",
)


#: The event a settled job announces; `core.events.JobKind` has no ``job.cancelled``.
_FINISHED_KINDS: dict[str, JobKind] = {
    "done": "job.done",
    "failed": "job.failed",
    "cancelled": "job.superseded",
}


def workflow_id_for(job_id: str) -> str:
    return f"render-{job_id}"


def _job(row: DictRow) -> Job:
    return Job.model_validate({column: row[column] for column in PROJECTION_COLUMNS})


class JobProjection:
    backend = "postgres"

    def __init__(
        self,
        conninfo: str,
        *,
        pool_size: int = 10,
        connect_timeout: float = 30.0,
        events: TransactionalEvents | None = None,
    ) -> None:
        self.conninfo = conninfo
        self.connect_timeout = connect_timeout
        self.events = events
        #: This process's LISTEN connection, which the event bus shares.
        self.pg_listener = PgListener(conninfo, connect_timeout=connect_timeout)
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=pool_size,
            open=False,
            connection_class=Connection[DictRow],
            kwargs={"autocommit": True, "row_factory": dict_row},
            name="scadbuddy-jobs",
        )

    def open(self) -> None:
        self._pool.open(wait=True, timeout=self.connect_timeout)
        with self._pool.connection() as conn:
            applied = migrate(conn)
        if applied:
            logger.info("applied database migrations", extra={"versions": applied})

    def close(self) -> None:
        self._pool.close()

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        return self._pool

    def listener(self, *, on_state: Callable[[bool], None]) -> PgListener:
        """The LISTEN connection, told whose state to report; the bus runs it. No
        channel of its own: no worker here waits for a NOTIFY to claim a job."""
        listener = self.pg_listener
        listener.on_state(on_state)
        return listener

    def _announce(self, conn: Connection[Any], job_id: str, slug: str, kind: JobKind) -> None:
        if self.events is not None:
            self.events.publish_in(conn, JobEvent(kind=kind, job_id=job_id, slug=slug))

    # -- the API's writes -------------------------------------------------------

    def submit(
        self, job: Job, key: str, *, supersedes: str | None = None, max_pending: int = 0
    ) -> Submitted:
        superseded: Job | None = None
        with self._pool.connection() as conn, conn.transaction():
            if supersedes is not None:
                previous = conn.execute(
                    "SELECT * FROM render_jobs WHERE id = %s AND state IN ('pending', 'running')"
                    " AND slug = %s FOR UPDATE",
                    (supersedes, job.slug),
                ).fetchone()
                if previous is not None and previous["render_key"] == key:
                    return Submitted(_job(previous), coalesced=True)
                if previous is not None:
                    superseded = self._release(conn, previous, error=SUPERSEDED_ERROR)
            if max_pending:
                twin = conn.execute(
                    "SELECT 1 FROM render_jobs WHERE state = 'pending' AND render_key = %s", (key,)
                ).fetchone()
                if twin is None:
                    counted = conn.execute(
                        "SELECT count(*) AS pending FROM render_jobs WHERE state = 'pending'"
                    ).fetchone()
                    assert counted is not None
                    if counted["pending"] >= max_pending:
                        raise QueueFullError(counted["pending"])
            row = conn.execute(
                "INSERT INTO render_jobs (id, slug, params, inputs, model_version, state,"
                " created_at, render_key, workflow_id, kind, traceparent)"
                " VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s)"
                " ON CONFLICT (render_key) WHERE state = 'pending'"
                " DO UPDATE SET claims = render_jobs.claims + 1"
                " RETURNING *, (xmax = 0) AS inserted",
                (
                    job.id,
                    job.slug,
                    Jsonb(job.params),
                    Jsonb(job.inputs or legacy_inputs(job.params)),
                    job.model_version,
                    job.created_at,
                    key,
                    workflow_id_for(job.id),
                    job.kind,
                    job.traceparent,
                ),
            ).fetchone()
            assert row is not None
            if row["inserted"]:
                self._announce(conn, row["id"], row["slug"], "job.pending")
        return Submitted(_job(row), coalesced=not row["inserted"], superseded=superseded)

    def _release(self, conn: Connection[Any], row: DictRow, *, error: str) -> Job | None:
        """Take one claim off an unfinished row; the last one cancels it. Returns the
        cancelled job, or None while claims remain."""
        if row["claims"] > 1:
            conn.execute("UPDATE render_jobs SET claims = claims - 1 WHERE id = %s", (row["id"],))
            return None
        dropped = conn.execute(
            "UPDATE render_jobs SET state = 'cancelled', claims = 0, finished_at = %s,"
            " error = %s WHERE id = %s RETURNING *",
            (now(), error, row["id"]),
        ).fetchone()
        assert dropped is not None
        self._announce(conn, row["id"], row["slug"], "job.superseded")
        return _job(dropped)

    def release_claim(self, job_id: str, *, slug: str) -> Job | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM render_jobs WHERE id = %s AND slug = %s"
                " AND state IN ('pending', 'running') FOR UPDATE",
                (job_id, slug),
            ).fetchone()
            if row is None:
                return None
            return self._release(conn, row, error=CANCELLED_ERROR)

    def adopt_legacy_pending(self) -> list[str]:
        """At boot: give each row a pre-Temporal release's queue left pending the
        workflow id the reconciler starts it under (`stale_pending` takes only rows
        that name one). Returns their ids."""
        with self._pool.connection() as conn, conn.transaction():
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM render_jobs WHERE state = 'pending' AND workflow_id IS NULL"
                    " ORDER BY created_at, id FOR UPDATE"
                ).fetchall()
            ]
            for job_id in ids:
                conn.execute(
                    "UPDATE render_jobs SET workflow_id = %s WHERE id = %s",
                    (workflow_id_for(job_id), job_id),
                )
        return ids

    def fail_legacy_running(self) -> list[Job]:
        """At start-up: fail the rows a pre-Temporal release's queue left running (no
        workflow), which nothing will finish. Its pending rows are
        `adopt_legacy_pending`'s."""
        with self._pool.connection() as conn, conn.transaction():
            rows = conn.execute(
                "UPDATE render_jobs SET state = 'failed', finished_at = now(), error = %s"
                " WHERE state = 'running' AND workflow_id IS NULL RETURNING *",
                (LEGACY_RUNNING_ERROR,),
            ).fetchall()
            for row in rows:
                self._announce(conn, row["id"], row["slug"], "job.failed")
        return [_job(row) for row in rows]

    # -- the workflow's writes (each guarded by the state it expects) -----------

    def mark_started(self, job_id: str) -> Job | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE render_jobs SET state = 'running', started_at = now(),"
                " attempts = attempts + 1, workflow_id = coalesce(workflow_id, %s)"
                " WHERE id = %s AND state = 'pending' RETURNING *",
                (workflow_id_for(job_id), job_id),
            ).fetchone()
            if row is not None:
                self._announce(conn, row["id"], row["slug"], "job.running")
        return _job(row) if row is not None else None

    def set_steps(self, job_id: str, steps: list[StepInfo]) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE render_jobs SET steps = %s"
                " WHERE id = %s AND state IN ('pending', 'running')",
                (Jsonb([step.model_dump(mode="json") for step in steps]), job_id),
            )

    def finish(self, job: Job) -> bool:
        """Settle an unfinished job: any terminal state is a forward move from pending
        or running (spec §3.4). A job the API already cancelled (`release_claim`)
        takes the cancellation handler's final projection -- log and steps, and its
        error only where the API stored none -- without a second event. A done or
        failed job is left alone."""
        assert job.state in ("done", "failed", "cancelled")
        with self._pool.connection() as conn, conn.transaction():
            cursor = conn.execute(
                "UPDATE render_jobs SET state = %s, finished_at = %s, log_tail = %s,"
                " error = %s, result = %s, diagnostics = %s, diagnostics_dropped = %s,"
                " warnings = %s, steps = %s, pipeline_version = %s"
                " WHERE id = %s AND state IN ('pending', 'running')",
                (
                    job.state,
                    job.finished_at or now(),
                    Jsonb(job.log_tail),
                    job.error,
                    Jsonb(job.result.model_dump(mode="json")) if job.result is not None else None,
                    Jsonb([d.model_dump(mode="json") for d in job.diagnostics]),
                    job.diagnostics_dropped,
                    Jsonb(job.warnings),
                    Jsonb([s.model_dump(mode="json") for s in job.steps]),
                    job.pipeline_version,
                    job.id,
                ),
            )
            if cursor.rowcount == 1:
                self._announce(conn, job.id, job.slug, _FINISHED_KINDS[job.state])
                return True
            if job.state != "cancelled":
                return False
            cursor = conn.execute(
                "UPDATE render_jobs SET finished_at = %s, log_tail = %s, steps = %s,"
                " error = coalesce(error, nullif(%s, ''))"
                " WHERE id = %s AND state = 'cancelled'",
                (
                    job.finished_at or now(),
                    Jsonb(job.log_tail),
                    Jsonb([s.model_dump(mode="json") for s in job.steps]),
                    job.error,
                    job.id,
                ),
            )
            return cursor.rowcount == 1

    # -- reads ------------------------------------------------------------------

    def stale_pending(self, older_than: float) -> list[Job]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                # Only rows a workflow owns: a legacy pending row (no workflow_id) is
                # the legacy queue's during a rolling deploy, never the reconciler's.
                "SELECT * FROM render_jobs WHERE state = 'pending' AND started_at IS NULL"
                " AND workflow_id IS NOT NULL"
                " AND created_at < now() - make_interval(secs => %s) ORDER BY created_at",
                (older_than,),
            ).fetchall()
        return [_job(row) for row in rows]

    def read(self, job_id: str) -> Job:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT * FROM render_jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return _job(row)

    def list_jobs(self) -> list[Job]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM render_jobs ORDER BY created_at, id").fetchall()
        return [_job(row) for row in rows]

    def has_unfinished(self, slug: str) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM render_jobs WHERE slug = %s"
                " AND state IN ('pending', 'running')) AS unfinished",
                (slug,),
            ).fetchone()
        return bool(row and row["unfinished"])

    def latest_finished(self, slug: str) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM render_jobs WHERE slug = %s AND state IN ('done', 'failed')"
                " AND finished_at IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1",
                (slug,),
            ).fetchone()
        return _job(row) if row is not None else None

    def counts(self) -> QueueCounts:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FILTER (WHERE state = 'pending') AS pending,"
                " count(*) FILTER (WHERE state = 'running') AS running,"
                " min(created_at) FILTER (WHERE state = 'pending') AS oldest_pending"
                " FROM render_jobs WHERE state IN ('pending', 'running')"
            ).fetchone()
        assert row is not None
        return QueueCounts(
            pending=row["pending"], running=row["running"], oldest_pending=row["oldest_pending"]
        )

    def prune(self, ttl: float, *, now_: datetime | None = None) -> list[str]:
        cutoff = (now_ or now()) - timedelta(seconds=ttl)
        with self._pool.connection() as conn, conn.transaction():
            rows = conn.execute(
                "DELETE FROM render_jobs WHERE state IN ('done', 'failed', 'cancelled')"
                " AND coalesce(finished_at, created_at) < %s RETURNING id",
                (cutoff,),
            ).fetchall()
            removed = [row["id"] for row in rows]
            if removed:
                conn.execute(
                    "DELETE FROM blob_refs WHERE holder_kind = 'job' AND holder_id = ANY(%s)",
                    (removed,),
                )
        return removed

    def delete(self, job_id: str) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM render_jobs WHERE id = %s", (job_id,))
            conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = 'job' AND holder_id = %s", (job_id,)
            )
