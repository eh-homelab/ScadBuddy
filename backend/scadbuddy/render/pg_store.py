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
- **Wake-ups.** A new or requeued job sends ``NOTIFY scadbuddy_render_queue`` in the
  transaction that queues it, so it is delivered on commit and never for a job that
  was rolled back. Each process holds one `QueueListener` connection that wakes its
  idle workers; their poll is only the fallback for a notification missed while
  that connection was down.

Schema changes go in `MIGRATIONS`, append-only, applied at `open` under an advisory
lock so two starting pods cannot race each other.
"""

from __future__ import annotations

import asyncio
import logging
import random
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection, Connection
from psycopg.errors import UniqueViolation
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
    QueueFullError,
    Reaped,
    Submitted,
)

logger = logging.getLogger(__name__)

#: `pg_advisory_xact_lock` key for applying migrations ("SCADBDDY" in ASCII).
MIGRATION_LOCK = 0x5343_4144_4244_4459

#: The channel a queued job is announced on. Channels are per database, not per
#: schema: deployments sharing one database only wake each other's idle workers.
QUEUE_CHANNEL = "scadbuddy_render_queue"
#: How the listening connection shows in `pg_stat_activity`.
LISTENER_APPLICATION_NAME = "scadbuddy-render-listener"

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
    # 2: print-analyzer decisions (#284; `scadbuddy.analyzers.decisions`). One row per
    # rule, instance ('' for every instance) and scope; `body` is the whole decision.
    """
    CREATE TABLE analyzer_decisions (
        id            text PRIMARY KEY,
        diagnostic_id text NOT NULL,
        instance      text NOT NULL DEFAULT '',
        scope_kind    text NOT NULL,
        scope_key     text NOT NULL DEFAULT '',
        kind          text NOT NULL CHECK (kind IN ('accept', 'ignore', 'suppress')),
        body          jsonb NOT NULL,
        created_at    timestamptz NOT NULL
    );
    CREATE UNIQUE INDEX analyzer_decisions_target
        ON analyzer_decisions (scope_kind, scope_key, diagnostic_id, instance);
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


def _notify(conn: Connection[Any]) -> None:
    """Announce queued work. Called inside the transaction that queued it: Postgres
    delivers it on commit, once the row is visible to the claim it prompts, and
    drops it on a rollback."""
    conn.execute("SELECT pg_notify(%s, '')", (QUEUE_CHANNEL,))


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
    backend = "postgres"

    def __init__(
        self,
        conninfo: str,
        paths: DataPaths,
        *,
        pool_size: int = 10,
        connect_timeout: float = 30.0,
    ) -> None:
        self.paths = paths
        self.conninfo = conninfo
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

    def listener(
        self,
        *,
        on_notify: Callable[[], None],
        on_state: Callable[[bool], None],
        check_interval: float,
    ) -> QueueListener:
        return QueueListener(
            self.conninfo,
            on_notify=on_notify,
            on_state=on_state,
            check_interval=check_interval,
            connect_timeout=self.connect_timeout,
        )

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
                error = LOST_WORKER_ERROR
                if row["attempts"] < max_attempts:
                    # Tried, not checked first: an identical render already pending
                    # (or submitted concurrently) makes the requeue violate the
                    # pending-key unique index, and a SELECT beforehand would only
                    # narrow that race, not close it. The savepoint confines the
                    # violation to this row instead of rolling back the whole pass.
                    # `claims` is left as it is: every submitter coalesced onto the
                    # job is still waiting on it, and a supersede from one of them
                    # must release only that one's claim.
                    try:
                        with conn.transaction():
                            back = conn.execute(
                                "UPDATE render_jobs SET state = 'pending', started_at = NULL,"
                                " heartbeat_at = NULL WHERE id = %s RETURNING *",
                                (row["id"],),
                            ).fetchone()
                    except UniqueViolation:
                        error = TWIN_QUEUED_ERROR
                    else:
                        assert back is not None
                        requeued.append(_job(back))
                        continue
                dead = conn.execute(
                    "UPDATE render_jobs SET state = 'failed', finished_at = now(),"
                    " error = %s WHERE id = %s RETURNING *",
                    (error, row["id"]),
                ).fetchone()
                assert dead is not None
                failed.append(_job(dead))
            if requeued:
                _notify(conn)
        return Reaped(requeued=requeued, failed=failed)

    def submit(
        self,
        job: Job,
        key: str,
        *,
        supersedes: str | None = None,
        max_pending: int = 0,
    ) -> Submitted:
        superseded: Job | None = None
        with self._pool.connection() as conn, conn.transaction():
            if supersedes is not None:
                # Only a job of the same model: a job id from another model's page
                # (or another client) is never this submit's to drop.
                previous = conn.execute(
                    "SELECT * FROM render_jobs WHERE id = %s AND state = 'pending'"
                    " AND slug = %s FOR UPDATE",
                    (supersedes, job.slug),
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
            if max_pending:
                # Inside the transaction: raising rolls the supersede above back,
                # so a refusal changes nothing. A soft limit across replicas --
                # two may each see one place left -- which is all it needs to be.
                twin = conn.execute(
                    "SELECT 1 FROM render_jobs WHERE state = 'pending' AND render_key = %s",
                    (key,),
                ).fetchone()
                if twin is None:
                    counted = conn.execute(
                        "SELECT count(*) AS pending FROM render_jobs WHERE state = 'pending'"
                    ).fetchone()
                    assert counted is not None
                    if counted["pending"] >= max_pending:
                        raise QueueFullError(counted["pending"])
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
            if row["inserted"]:
                # A coalesced submit queued nothing new, so no worker has more to do.
                _notify(conn)
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


class QueueListener:
    """This process's ``LISTEN`` on `QUEUE_CHANNEL`, on one dedicated connection
    outside the pool: a notification only reaches the session that listens, and a
    pooled connection goes back to other callers between uses.

    Every notification calls ``on_notify``, and so does every (re)connect: a job
    queued while nothing listened sent a notification this process never saw. A
    dropped or refused connection is retried after a capped exponential back-off
    with jitter, so replicas do not all reconnect in step after a database restart.
    An idle connection is checked every ``check_interval``, since a half-open TCP
    connection delivers nothing and raises nothing until something is sent on it.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        on_notify: Callable[[], None],
        on_state: Callable[[bool], None],
        check_interval: float,
        connect_timeout: float = 30.0,
        backoff: float = 0.5,
        max_backoff: float = 30.0,
    ) -> None:
        self.conninfo = conninfo
        self.on_notify = on_notify
        self.on_state = on_state
        self.check_interval = check_interval
        self.connect_timeout = connect_timeout
        self.backoff = backoff
        self.max_backoff = max_backoff
        #: Times a LISTEN has been established.
        self.connects = 0
        #: The listening session's server process while connected, else `None`.
        self.backend_pid: int | None = None

    async def run(self) -> None:
        delay = self.backoff
        while True:
            connects = self.connects
            try:
                await self._listen()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "the render queue listener is disconnected; workers poll until it is back",
                    extra={"error": str(error)},
                )
            if self.connects != connects:
                delay = self.backoff  # it had been up: a new outage starts short
            await asyncio.sleep(delay * random.uniform(0.5, 1.0))
            delay = min(delay * 2, self.max_backoff)

    async def _listen(self) -> None:
        conn = await AsyncConnection.connect(
            self.conninfo,
            autocommit=True,
            connect_timeout=max(1, round(self.connect_timeout)),
            application_name=LISTENER_APPLICATION_NAME,
        )
        async with conn:
            await conn.execute(f"LISTEN {QUEUE_CHANNEL}".encode())
            self.connects += 1
            self.backend_pid = conn.info.backend_pid
            self.on_state(True)
            try:
                self.on_notify()
                while True:
                    async for _ in conn.notifies(timeout=self.check_interval):
                        self.on_notify()
                    await conn.execute(b"SELECT 1")
            finally:
                self.backend_pid = None
                self.on_state(False)
