"""`render_jobs` as a projection (spec 2026-09-27 §3.2).

The workflow `render-<render_key>` inserts its row in its first activity (`accept`,
#1053) and its `project` activity moves `state` forward in place. Every write is
guarded by the state it expects, so a retried activity cannot move a job backwards,
and every state change publishes its ``job.*`` event on the bus inside its own transaction
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
from temporalio.api.errordetails.v1 import NamespaceNotFoundFailure
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.events import JobEvent, JobKind
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.render.inputs import legacy_inputs
from scadbuddy.render.job_models import (
    CANCELLED_ERROR,
    Job,
    JobNotFoundError,
    QueueCounts,
    QueueFullError,
    StepInfo,
    now,
)
from scadbuddy.render.pg_store import TransactionalEvents, migrate

logger = logging.getLogger(__name__)

LEGACY_RUNNING_ERROR = "failed: the upgrade to Temporal-backed rendering left it unfinished"
LEGACY_UNSTARTED_ERROR = "failed: the upgrade left it waiting with no workflow to run it"
ORPHANED_ERROR = "failed: its workflow closed before it ran"
CLOSED_ERROR = "failed: its workflow closed without settling it"
#: How old a pending row an older build inserted must be before it can be orphaned: an
#: older API inserts its row, then starts its workflow, within its own 5 s start
#: timeout; this is six of those.
LEGACY_GRACE = timedelta(seconds=30)
#: How old an unsettled row must be before a settle pass asks about its run: Visibility,
#: which `open_runs` reads, can list a run a moment after it started.
VISIBILITY_GRACE = timedelta(seconds=30)
#: The executions whose rows a settle pass leaves alone: every open render run, of
#: this build (`render-<render_key>`) or an older one (`render-<job id>`).
OPEN_RENDERS = "WorkflowType = 'TemplatePipeline' AND ExecutionStatus = 'Running'"


class LegacyPendingError(Exception):
    """A row an older build inserted waits on the same render key: its pending key
    holds until that build runs it, or `render_accept` finds it orphaned."""

    def __init__(self, job: Job) -> None:
        super().__init__(f"render job {job.id} of an older build waits on the same key")
        self.job = job


def execution_gone(error: RPCError) -> bool:
    """A NOT_FOUND about the execution, not about the namespace: a mistyped or
    unregistered namespace answers NOT_FOUND too (review #1066 (7) 2)."""
    return error.status == RPCStatusCode.NOT_FOUND and not any(
        detail.Is(NamespaceNotFoundFailure.DESCRIPTOR) for detail in error.grpc_status.details
    )


async def legacy_unrun(client: Client, job: Job, *, rpc_timeout: timedelta) -> bool:
    """Whether no workflow will settle ``job``, a pending or running row an older build
    inserted past `LEGACY_GRACE`: it names none, or Temporal has none running. Raises the
    RPC error when Temporal cannot say."""
    if now() - job.created_at < LEGACY_GRACE:
        return False
    if job.workflow_id is None:
        return True
    try:
        described = await client.get_workflow_handle(job.workflow_id).describe(
            rpc_timeout=rpc_timeout
        )
    except RPCError as error:
        if execution_gone(error):
            return True
        raise
    # A closed run (terminated, failed) that retention still keeps never settles it.
    return described.status != WorkflowExecutionStatus.RUNNING


async def open_runs(client: Client, *, rpc_timeout: timedelta) -> set[tuple[str, str]]:
    """The workflow and run ids of every open render run, from one Visibility listing
    (paged) rather than one describe per row (review #1066 (9) 4). Visibility trails
    the executions, so a row missing from it is only a candidate: `run_closed` or
    `legacy_unrun` decides."""
    return {
        (execution.id, execution.run_id)
        async for execution in client.list_workflows(OPEN_RENDERS, rpc_timeout=rpc_timeout)
    }


async def run_closed(client: Client, job: Job, *, rpc_timeout: timedelta) -> bool:
    """Whether the execution that owns ``job``, an unsettled row of a
    ``render-<render_key>`` run, has closed: its run settles the row before it closes,
    so one that closed without (terminated, timed out) never will. Raises the RPC error
    when Temporal cannot say."""
    assert job.workflow_id is not None and job.workflow_run_id is not None
    try:
        described = await client.get_workflow_handle(
            job.workflow_id, run_id=job.workflow_run_id
        ).describe(rpc_timeout=rpc_timeout)
    except RPCError as error:
        if execution_gone(error):
            return True  # past retention
        raise
    return described.status != WorkflowExecutionStatus.RUNNING


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
    "workflow_run_id",
    "traceparent",
)


#: How many of the latest finished renders `recent_render_seconds` reads.
RECENT_RENDERS = 20
#: The event a settled job announces; `core.events.JobKind` has no ``job.cancelled``.
_FINISHED_KINDS: dict[str, JobKind] = {
    "done": "job.done",
    "failed": "job.failed",
    "cancelled": "job.superseded",
}


def workflow_id_for(job_id: str) -> str:
    """A legacy job's workflow, from before renders were keyed by content (#1053)."""
    return f"render-{job_id}"


def workflow_id_for_key(key: str) -> str:
    """The execution every request for one render shares (spec 2026-10-01 §4.5)."""
    return f"render-{key}"


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

    def accept(
        self,
        job: Job,
        key: str,
        *,
        workflow_id: str,
        run_id: str,
        max_pending: int = 0,
        orphaned: str | None = None,
        supersedes: str | None = None,
    ) -> Job:
        """The first activity of `render-<render_key>` (#1053): the execution's row, or
        `QueueFullError` with nothing written. A retried activity finds its row.
        ``orphaned`` names the older build's row on the key that the caller found no
        workflow will run (`LegacyPendingError`): it is failed, and this row goes in.
        ``supersedes``, a pending job of the same slug the request replaces, is not
        counted against ``max_pending``: its release follows the accept."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM render_jobs WHERE workflow_id = %s AND workflow_run_id = %s",
                (workflow_id, run_id),
            ).fetchone()
            if row is not None:
                return _job(row)
            # `render_jobs_pending_key` still holds one pending row per key (see its
            # migration). Only this run of `render-<key>` is open, so another run's
            # pending row is an orphan; an older build's row is that build's to run.
            held = conn.execute(
                "SELECT * FROM render_jobs WHERE render_key = %s AND state = 'pending' FOR UPDATE",
                (key,),
            ).fetchone()
            legacy = held is not None and held["workflow_run_id"] is None
            if held is not None and legacy and held["id"] != orphaned:
                raise LegacyPendingError(_job(held))
            if held is not None:
                conn.execute(
                    "UPDATE render_jobs SET state = 'failed', finished_at = now(), error = %s"
                    " WHERE id = %s",
                    (LEGACY_UNSTARTED_ERROR if legacy else ORPHANED_ERROR, held["id"]),
                )
                self._announce(conn, held["id"], held["slug"], "job.failed")
            if max_pending:
                counted = conn.execute(
                    "SELECT count(*) AS pending FROM render_jobs WHERE state = 'pending'"
                    " AND (id, slug) IS DISTINCT FROM (%s, %s)",
                    (supersedes, job.slug),
                ).fetchone()
                assert counted is not None
                if counted["pending"] >= max_pending:
                    raise QueueFullError(counted["pending"])
            row = conn.execute(
                "INSERT INTO render_jobs (id, slug, params, inputs, model_version, state,"
                " created_at, render_key, workflow_id, workflow_run_id, kind, traceparent)"
                " VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (workflow_id, workflow_run_id)"
                " DO UPDATE SET claims = render_jobs.claims"
                " RETURNING *, (xmax = 0) AS inserted",
                (
                    job.id,
                    job.slug,
                    Jsonb(job.params),
                    Jsonb(job.inputs or legacy_inputs(job.params)),
                    job.model_version,
                    job.created_at,
                    key,
                    workflow_id,
                    run_id,
                    job.kind,
                    job.traceparent,
                ),
            ).fetchone()
            assert row is not None
            if row["inserted"]:
                self._announce(conn, row["id"], row["slug"], "job.pending")
        return _job(row)

    def set_claims(self, job_id: str, claims: int) -> None:
        """The workflow's claim count, projected (#1053)."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE render_jobs SET claims = %s"
                " WHERE id = %s AND state IN ('pending', 'running')",
                (claims, job_id),
            )

    def legacy_unsettled(self, older_than: timedelta) -> list[Job]:
        """Rows an older release inserted over ``older_than`` ago: pending ones past its
        insert-then-start, and ones its worker moved to running (a pre-Temporal running
        row, with no workflow, is `fail_legacy_running`'s). One whose workflow is not
        running is orphaned (review #1066 (5) 2.1)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM render_jobs WHERE workflow_run_id IS NULL"
                " AND (state = 'pending' OR (state = 'running' AND workflow_id IS NOT NULL))"
                " AND created_at < now() - %s ORDER BY created_at, id",
                (older_than,),
            ).fetchall()
        return [_job(row) for row in rows]

    def fail_legacy(self, job_ids: list[str]) -> list[Job]:
        """Fail legacy rows nothing will settle: a pending one never started, a running
        one's workflow closed. Any other row is left alone."""
        with self._pool.connection() as conn, conn.transaction():
            rows = conn.execute(
                "UPDATE render_jobs SET state = 'failed', finished_at = now(),"
                " error = CASE state WHEN 'pending' THEN %s ELSE %s END"
                " WHERE id = ANY(%s) AND workflow_run_id IS NULL"
                " AND (state = 'pending' OR (state = 'running' AND workflow_id IS NOT NULL))"
                " RETURNING *",
                (LEGACY_UNSTARTED_ERROR, CLOSED_ERROR, job_ids),
            ).fetchall()
            for row in rows:
                self._announce(conn, row["id"], row["slug"], "job.failed")
        return [_job(row) for row in rows]

    def unsettled(self, older_than: timedelta) -> list[Job]:
        """The pending and running rows of ``render-<render_key>`` runs (#1053)
        inserted over ``older_than`` ago."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM render_jobs WHERE state IN ('pending', 'running')"
                " AND workflow_run_id IS NOT NULL AND created_at < now() - %s"
                " ORDER BY created_at, id",
                (older_than,),
            ).fetchall()
        return [_job(row) for row in rows]

    def fail_closed(self, job_ids: list[str]) -> list[Job]:
        """Fail the rows of runs that closed without settling them (`run_closed`); a
        row its run settled meanwhile is left alone."""
        with self._pool.connection() as conn, conn.transaction():
            rows = conn.execute(
                "UPDATE render_jobs SET state = 'failed', finished_at = now(), error = %s"
                " WHERE id = ANY(%s) AND state IN ('pending', 'running')"
                " AND workflow_run_id IS NOT NULL RETURNING *",
                (CLOSED_ERROR, job_ids),
            ).fetchall()
            for row in rows:
                self._announce(conn, row["id"], row["slug"], "job.failed")
        return [_job(row) for row in rows]

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

    def release_claim(self, job_id: str, *, slug: str, error: str = CANCELLED_ERROR) -> Job | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM render_jobs WHERE id = %s AND slug = %s"
                " AND state IN ('pending', 'running') FOR UPDATE",
                (job_id, slug),
            ).fetchone()
            if row is None:
                return None
            return self._release(conn, row, error=error)

    def fail_legacy_running(self) -> list[Job]:
        """At start-up: fail the rows a pre-Temporal release's queue left running (no
        workflow), which nothing will finish. Its pending rows are `fail_legacy`'s."""
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

    def recent_render_seconds(self) -> float | None:
        """The median time the latest `RECENT_RENDERS` finished renders took, from their
        start to their end, or None before one has: what a full queue's `Retry-After`
        says (#603)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) AS median"
                " FROM (SELECT extract(epoch FROM finished_at - started_at) AS seconds"
                "  FROM render_jobs WHERE state = 'done' AND started_at IS NOT NULL"
                "  ORDER BY finished_at DESC LIMIT %s) AS latest",
                (RECENT_RENDERS,),
            ).fetchone()
        median = row["median"] if row is not None else None
        return float(median) if median is not None else None

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
