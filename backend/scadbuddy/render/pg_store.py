"""The render queue in Postgres (``SCADBUDDY_DATABASE_URL``).

One table, ``render_jobs``, is both the job record and the wait list:

- **Claiming** is ``UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)``,
  so any number of workers, in any number of processes, each take a different job
  and never block on each other.
- **Coalescing** is a partial unique index on ``render_key`` over pending rows:
  ``INSERT … ON CONFLICT … DO UPDATE SET claims = claims + 1`` either queues the
  render or counts one more claim on the identical one already waiting, atomically.
- **Leases.** A worker heartbeats the job it holds. `reap` requeues a running job
  whose heartbeat is older than the lease (the pod died mid-render), up to the
  attempt limit; `finish` only lands for the attempt that still holds the job.

Schema changes go in `MIGRATIONS`, append-only, applied at `open` under an advisory
lock so two starting pods cannot race each other.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow, dict_row, tuple_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.job_models import Job
from scadbuddy.render.job_models import now as _now
from scadbuddy.render.job_store import (
    LOST_WORKER_ERROR,
    SUPERSEDED_ERROR,
    JobNotFoundError,
    QueueCounts,
    Reaped,
    Submitted,
)

logger = logging.getLogger(__name__)

#: `pg_advisory_xact_lock` key for applying migrations ("SCADBDDY" in ASCII).
MIGRATION_LOCK = 0x5343_4144_4244_4459

#: Append-only: each entry is applied once, in order, and recorded by its position.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE render_jobs (
        id            text PRIMARY KEY,
        slug          text NOT NULL,
        params        jsonb NOT NULL DEFAULT '{}'::jsonb,
        model_version text,
        state         text NOT NULL CHECK (state IN ('pending', 'running', 'done', 'failed')),
        created_at    timestamptz NOT NULL,
        started_at    timestamptz,
        finished_at   timestamptz,
        log_tail      jsonb NOT NULL DEFAULT '[]'::jsonb,
        error         text,
        result        jsonb,
        render_key    text NOT NULL,
        claims        integer NOT NULL DEFAULT 1,
        attempts      integer NOT NULL DEFAULT 0,
        heartbeat_at  timestamptz
    );
    CREATE INDEX render_jobs_pending ON render_jobs (created_at, id) WHERE state = 'pending';
    CREATE UNIQUE INDEX render_jobs_pending_key ON render_jobs (render_key)
        WHERE state = 'pending';
    CREATE INDEX render_jobs_running ON render_jobs (heartbeat_at) WHERE state = 'running';
    CREATE INDEX render_jobs_unfinished_slug ON render_jobs (slug)
        WHERE state IN ('pending', 'running');
    CREATE INDEX render_jobs_settled ON render_jobs (finished_at)
        WHERE state IN ('done', 'failed');
    """,
)

JOB_COLUMNS = (
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
)

TWIN_QUEUED_ERROR = "interrupted when its worker stopped responding; an identical render is queued"


def _job(row: DictRow) -> Job:
    return Job.model_validate({column: row[column] for column in JOB_COLUMNS})


def migrate(conn: Connection[Any]) -> list[int]:
    """Apply the migrations this database has not seen; returns their versions."""
    applied: list[int] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scadbuddy_migrations ("
            " version integer PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        versions = conn.cursor(row_factory=tuple_row).execute(
            "SELECT version FROM scadbuddy_migrations"
        )
        done = {row[0] for row in versions}
        for version, statement in enumerate(MIGRATIONS, start=1):
            if version in done:
                continue
            conn.execute(statement.encode("utf-8"))
            conn.execute("INSERT INTO scadbuddy_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
    return applied


class PostgresJobStore:
    def __init__(
        self,
        conninfo: str,
        paths: DataPaths,
        *,
        pool_size: int = 10,
        connect_timeout: float = 30.0,
    ) -> None:
        self.paths = paths
        self.connect_timeout = connect_timeout
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

    def abandon_orphans(self) -> list[Job]:
        # Pending jobs are durable and still wanted; running ones are recovered by
        # `reap` once their lease runs out, whichever process held them.
        return []

    def reap(self, *, lease: float, max_attempts: int) -> Reaped:
        requeued: list[Job] = []
        failed: list[Job] = []
        with self._pool.connection() as conn, conn.transaction():
            stale = conn.execute(
                "SELECT * FROM render_jobs WHERE state = 'running'"
                " AND heartbeat_at < now() - make_interval(secs => %s)"
                " FOR UPDATE SKIP LOCKED",
                (lease,),
            ).fetchall()
            for row in stale:
                twin = conn.execute(
                    "SELECT 1 FROM render_jobs WHERE state = 'pending' AND render_key = %s",
                    (row["render_key"],),
                ).fetchone()
                if row["attempts"] < max_attempts and twin is None:
                    back = conn.execute(
                        "UPDATE render_jobs SET state = 'pending', started_at = NULL,"
                        " heartbeat_at = NULL, claims = 1 WHERE id = %s RETURNING *",
                        (row["id"],),
                    ).fetchone()
                    assert back is not None
                    requeued.append(_job(back))
                else:
                    error = TWIN_QUEUED_ERROR if twin is not None else LOST_WORKER_ERROR
                    dead = conn.execute(
                        "UPDATE render_jobs SET state = 'failed', finished_at = now(),"
                        " error = %s WHERE id = %s RETURNING *",
                        (error, row["id"]),
                    ).fetchone()
                    assert dead is not None
                    failed.append(_job(dead))
        return Reaped(requeued=requeued, failed=failed)

    def submit(self, job: Job, key: str, *, supersedes: str | None = None) -> Submitted:
        superseded: Job | None = None
        with self._pool.connection() as conn, conn.transaction():
            if supersedes is not None:
                previous = conn.execute(
                    "SELECT * FROM render_jobs WHERE id = %s AND state = 'pending' FOR UPDATE",
                    (supersedes,),
                ).fetchone()
                if previous is not None and previous["render_key"] == key:
                    return Submitted(_job(previous), coalesced=True)
                if previous is not None and previous["claims"] > 1:
                    conn.execute(
                        "UPDATE render_jobs SET claims = claims - 1 WHERE id = %s", (supersedes,)
                    )
                elif previous is not None:
                    dropped = conn.execute(
                        "UPDATE render_jobs SET state = 'failed', claims = 0,"
                        " finished_at = %s, error = %s WHERE id = %s RETURNING *",
                        (_now(), SUPERSEDED_ERROR, supersedes),
                    ).fetchone()
                    assert dropped is not None
                    superseded = _job(dropped)
            row = conn.execute(
                "INSERT INTO render_jobs"
                " (id, slug, params, model_version, state, created_at, render_key)"
                " VALUES (%s, %s, %s, %s, 'pending', %s, %s)"
                " ON CONFLICT (render_key) WHERE state = 'pending'"
                " DO UPDATE SET claims = render_jobs.claims + 1"
                " RETURNING *, (xmax = 0) AS inserted",
                (
                    job.id,
                    job.slug,
                    Jsonb(job.params),
                    job.model_version,
                    job.created_at,
                    key,
                ),
            ).fetchone()
            assert row is not None
        return Submitted(_job(row), coalesced=not row["inserted"], superseded=superseded)

    def claim(self) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE render_jobs SET state = 'running', started_at = now(),"
                " heartbeat_at = now(), attempts = attempts + 1"
                " WHERE id = ("
                "  SELECT id FROM render_jobs WHERE state = 'pending'"
                "  ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1"
                " ) RETURNING *"
            ).fetchone()
        if row is None:
            return None
        # `finish` and `heartbeat` land only for this attempt, so a worker whose
        # lease was reaped cannot overwrite -- or keep alive -- the retry.
        return _job(row).claimed(row["attempts"])

    def heartbeat(self, job: Job) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE render_jobs SET heartbeat_at = now()"
                " WHERE id = %s AND state = 'running' AND attempts = %s",
                (job.id, job.attempt),
            )

    def finish(self, job: Job) -> bool:
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "UPDATE render_jobs SET state = %s, started_at = %s, finished_at = %s,"
                " log_tail = %s, error = %s, result = %s, heartbeat_at = NULL"
                " WHERE id = %s AND state = 'running' AND attempts = %s",
                (
                    job.state,
                    job.started_at,
                    job.finished_at,
                    Jsonb(job.log_tail),
                    job.error,
                    Jsonb(job.result.model_dump(mode="json")) if job.result is not None else None,
                    job.id,
                    job.attempt,
                ),
            )
            return cursor.rowcount == 1

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
                "SELECT EXISTS (SELECT 1 FROM render_jobs"
                " WHERE slug = %s AND state IN ('pending', 'running')) AS unfinished",
                (slug,),
            ).fetchone()
        return bool(row and row["unfinished"])

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

    def prune(self, ttl: float, *, now: datetime | None = None) -> list[str]:
        cutoff = (now or _now()) - timedelta(seconds=ttl)
        with self._pool.connection() as conn:
            rows = conn.execute(
                "DELETE FROM render_jobs WHERE state IN ('done', 'failed')"
                " AND coalesce(finished_at, created_at) < %s RETURNING id",
                (cutoff,),
            ).fetchall()
        removed = [row["id"] for row in rows]
        for job_id in removed:
            shutil.rmtree(self.paths.job_work_dir(job_id), ignore_errors=True)
        return removed

    def delete(self, job_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM render_jobs WHERE id = %s", (job_id,))
        shutil.rmtree(self.paths.job_work_dir(job_id), ignore_errors=True)
