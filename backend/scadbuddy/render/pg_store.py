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
  attempt limit, each in a transaction of its own; `finish` only lands for the
  attempt that still holds the job.
- **Wake-ups.** A new or requeued job sends ``NOTIFY scadbuddy_render_queue`` in the
  transaction that queues it, so it is delivered on commit and never for a job that
  was rolled back. Each process holds one LISTEN connection (`PgListener`, shared
  with the event bus) that wakes its idle workers; their poll is only the fallback
  for a notification missed while that connection was down.
- **Job events.** With an event bus attached (`events`), the ``job.*`` events of a
  submit, a reap and a finish are logged and NOTIFYed in the same transaction as the
  change (`PgNotifyEventBus.publish_in`), so they are heard on commit or not at all.

Schema changes are files in ``backend/scadbuddy/migrations/`` (`MIGRATIONS_DIR`), one
per migration, named by UTC timestamp and slug (``20260928T0612Z_settings.sql``). Add a
new file; never edit, rename or remove a merged one. They are applied at `open`, in
timestamp order and each once by file id, under an advisory lock so two starting pods
cannot race each other. They include the tables of the
stores that share this pool through `PostgresJobStore.connection` -- the
default-render previews' ``model_previews``
(``20260928T0721Z_model_previews.sql``).
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from psycopg import Connection, sql
from psycopg.errors import UniqueViolation
from psycopg.rows import DictRow, dict_row, tuple_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.core.events import Event, JobEvent, JobKind
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_listener import PgListener
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

#: The migration files (#491): one per migration, named ``<yyyymmdd>T<hhmm>Z_<slug>.sql``
#: (UTC). The stem is the file's id in ``scadbuddy_migrations``.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

#: ``<yyyymmdd>T<hhmm>Z_<slug>``; fixed width, so sorting ids sorts by time.
MIGRATION_ID = re.compile(r"\d{8}T\d{4}Z_[a-z0-9_]+")


class Migration(NamedTuple):
    id: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """The migration files of `directory`, in timestamp order. Anything there that is
    not a well-named ``.sql`` file raises, so a misnamed migration fails at start
    instead of being skipped."""
    migrations: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql" or not MIGRATION_ID.fullmatch(path.stem):
            raise ValueError(
                f"{path} is not a migration: files there are named"
                " <yyyymmdd>T<hhmm>Z_<slug>.sql (UTC, slug in [a-z0-9_])"
            )
        migrations.append(Migration(path.stem, path.read_text(encoding="utf-8")))
    return tuple(migrations)


#: Read at import, so an image without the files fails at start.
MIGRATIONS: tuple[Migration, ...] = load_migrations()

#: Before #491 the ledger recorded migrations by POSITION in a list: entry ``n`` of
#: that list is ``LEGACY_VERSIONS[n - 1]``. `migrate` rewrites positional rows to
#: these ids once, and keeps writing ``version`` for them so an image from before
#: #491 still starts on the ledger. Frozen: nothing is ever added here.
LEGACY_VERSIONS: tuple[str, ...] = (
    "20260927T2243Z_render_jobs",
    "20260928T0105Z_render_diagnostics",
    "20260928T0600Z_render_warnings",
    "20260928T0630Z_events",
)


class MigrationLedgerError(RuntimeError):
    """The ledger holds a positional row (from before #491) that main's list never
    had: this database ran a migration from a branch that did not merge as such.
    Guessing which file it was would either re-run or skip real schema."""


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
    "diagnostics",
    "diagnostics_dropped",
    "warnings",
)

TWIN_QUEUED_ERROR = "interrupted when its worker stopped responding; an identical render is queued"
#: A render a Temporal deployment was running when the API restarted onto this queue.
TEMPORAL_INTERRUPTED_ERROR = "interrupted: the API restarted onto the legacy queue"


def _job(row: DictRow) -> Job:
    return Job.model_validate({column: row[column] for column in JOB_COLUMNS})


def _notify(conn: Connection[Any]) -> None:
    """Announce queued work. Called inside the transaction that queued it: Postgres
    delivers it on commit, once the row is visible to the claim it prompts, and
    drops it on a rollback."""
    conn.execute("SELECT pg_notify(%s, '')", (QUEUE_CHANNEL,))


class TransactionalEvents(Protocol):
    """Publishes an event inside a caller's transaction (`PgNotifyEventBus`)."""

    def publish_in(self, conn: Connection[Any], event: Event) -> None: ...


def migrate(conn: Connection[Any], migrations: tuple[Migration, ...] = MIGRATIONS) -> list[str]:
    """Apply the migration files this database has not seen, in timestamp order;
    returns their ids.

    Any file not in the ledger is applied, so one with an OLDER timestamp that
    merges after newer ones ran (a branch cut earlier) still runs. A ledger row
    naming a file this build does not have (a newer image ran it) is left alone.
    """
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        _ensure_ledger(conn)
        ids = conn.cursor(row_factory=tuple_row).execute("SELECT id FROM scadbuddy_migrations")
        done = {row[0] for row in ids}
        for migration in sorted(migrations, key=lambda m: m.id):
            if migration.id in done:
                continue
            conn.execute(migration.sql.encode("utf-8"))
            version = (
                LEGACY_VERSIONS.index(migration.id) + 1 if migration.id in LEGACY_VERSIONS else None
            )
            conn.execute(
                "INSERT INTO scadbuddy_migrations (id, version) VALUES (%s, %s)",
                (migration.id, version),
            )
            applied.append(migration.id)
    return applied


def _ensure_ledger(conn: Connection[Any]) -> None:
    """Create the ledger, or convert one from before #491 (keyed by position) to file
    ids, inside `migrate`'s transaction and advisory lock: exactly once, however many
    pods start together."""
    columns = {
        row[0]
        for row in conn.cursor(row_factory=tuple_row).execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'scadbuddy_migrations'"
        )
    }
    if not columns:
        # `version` is kept for the LEGACY_VERSIONS files only.
        conn.execute(
            "CREATE TABLE scadbuddy_migrations ("
            " id text PRIMARY KEY,"
            " version integer UNIQUE,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        return
    if "id" in columns:
        return
    versions = [
        row[0]
        for row in conn.cursor(row_factory=tuple_row).execute(
            "SELECT version FROM scadbuddy_migrations ORDER BY version"
        )
    ]
    unknown = [v for v in versions if not 1 <= v <= len(LEGACY_VERSIONS)]
    if unknown:
        raise MigrationLedgerError(
            f"scadbuddy_migrations records positional version(s)"
            f" {', '.join(map(str, unknown))}, but main only ever had {len(LEGACY_VERSIONS)}:"
            " this database ran a migration from a branch that has not merged as such."
            " Recreate the database, or delete those rows and the schema they created,"
            " then start again."
        )
    pkey = (
        conn.cursor(row_factory=tuple_row)
        .execute(
            "SELECT conname FROM pg_constraint"
            " WHERE conrelid = 'scadbuddy_migrations'::regclass AND contype = 'p'"
        )
        .fetchone()
    )
    conn.execute("ALTER TABLE scadbuddy_migrations ADD COLUMN id text")
    conn.execute(
        "UPDATE scadbuddy_migrations SET id = (%s::text[])[version]", (list(LEGACY_VERSIONS),)
    )
    if pkey is not None:
        conn.execute(
            sql.SQL("ALTER TABLE scadbuddy_migrations DROP CONSTRAINT {}").format(
                sql.Identifier(pkey[0])
            )
        )
    conn.execute(
        "ALTER TABLE scadbuddy_migrations"
        " ALTER COLUMN id SET NOT NULL,"
        " ADD PRIMARY KEY (id),"
        " ADD UNIQUE (version),"
        " ALTER COLUMN version DROP NOT NULL"
    )


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
        #: This process's LISTEN connection, which the event bus shares.
        self.pg_listener = PgListener(conninfo, connect_timeout=connect_timeout)
        #: Where the job events of this store's transactions go; `None` leaves
        #: them all to the render queue (published after the change commits).
        self.events: TransactionalEvents | None = None
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

    def connection(self) -> AbstractContextManager[Connection[DictRow]]:
        """A pooled connection (autocommit, dict rows) for the other stores that keep
        their tables in this database. Usable once `open` has migrated it."""
        return self._pool.connection()

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        """The process's one pool, shared with the other Postgres stores
        (`bambuddy.uploads.BambuddyUploadStore`, `template_media`). Opened and migrated
        by `open`."""
        return self._pool

    def listener(
        self,
        *,
        on_notify: Callable[[], None],
        on_state: Callable[[bool], None],
        check_interval: float,
    ) -> PgListener:
        listener = self.pg_listener
        listener.check_interval = check_interval
        listener.on_state(on_state)
        # A (re)connect wakes the workers too: a job queued while nothing
        # listened sent a notification this process never saw.
        listener.listen(
            QUEUE_CHANNEL,
            on_notify=lambda _payload: on_notify(),
            on_connect=lambda _reconnected: on_notify(),
        )
        return listener

    @property
    def announces_jobs(self) -> bool:
        return self.events is not None

    def announce(self, job: Job, kind: JobKind) -> None:
        if self.events is None:
            return
        with self._pool.connection() as conn, conn.transaction():
            self._announce(conn, job, kind)

    def _announce(self, conn: Connection[Any], job: Job, kind: JobKind) -> None:
        if self.events is not None:
            self.events.publish_in(conn, JobEvent(kind=kind, job_id=job.id, slug=job.slug))

    def abandon_orphans(self) -> list[Job]:
        # Pending jobs are durable and still wanted; running ones are recovered by
        # `reap` once their lease runs out, whichever process held them.
        #
        # Rows a Temporal deployment left (SCADBUDDY_TEMPORAL_ADDRESS cleared since):
        # a pending one becomes this queue's to claim, and a running one, whose
        # workflow no worker here will finish, is failed. The caller announces those.
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE render_jobs SET workflow_id = NULL"
                " WHERE state = 'pending' AND workflow_id IS NOT NULL"
            )
            rows = conn.execute(
                "UPDATE render_jobs SET state = 'failed', finished_at = now(), error = %s"
                " WHERE state = 'running' AND workflow_id IS NOT NULL RETURNING *",
                (TEMPORAL_INTERRUPTED_ERROR,),
            ).fetchall()
        return [_job(row) for row in rows]

    def reap(self, *, lease: float, max_attempts: int) -> Reaped:
        """One short transaction per stale job, not one for the whole pass: each
        job's event takes the event log's lock (`EVENT_LOG_LOCK`), which is held to
        commit, and a pass over many lost workers must not hold every replica's
        publishing up for all of them."""
        requeued: list[Job] = []
        failed: list[Job] = []
        with self._pool.connection() as conn:
            # Candidates only, unlocked: each is locked, and re-checked, in its own
            # transaction below, so one another replica is reaping (locked, hence
            # skipped) or one whose worker heartbeated since is left alone.
            candidates = conn.execute(
                "SELECT id FROM render_jobs WHERE state = 'running' AND workflow_id IS NULL"
                " AND heartbeat_at < now() - make_interval(secs => %s)"
                " ORDER BY heartbeat_at, id",
                (lease,),
            ).fetchall()
            for candidate in candidates:
                with conn.transaction():
                    row = conn.execute(
                        "SELECT * FROM render_jobs WHERE id = %s AND state = 'running'"
                        " AND workflow_id IS NULL"
                        " AND heartbeat_at < now() - make_interval(secs => %s)"
                        " FOR UPDATE SKIP LOCKED",
                        (candidate["id"], lease),
                    ).fetchone()
                    if row is None:
                        continue
                    back = self._requeue(conn, row) if row["attempts"] < max_attempts else None
                    if isinstance(back, Job):
                        requeued.append(back)
                        self._announce(conn, back, "job.pending")
                        _notify(conn)
                        continue
                    dead = conn.execute(
                        "UPDATE render_jobs SET state = 'failed', finished_at = now(),"
                        " error = %s WHERE id = %s RETURNING *",
                        (back or LOST_WORKER_ERROR, row["id"]),
                    ).fetchone()
                    assert dead is not None
                    failed.append(_job(dead))
                    self._announce(conn, failed[-1], "job.failed")
        return Reaped(requeued=requeued, failed=failed)

    @staticmethod
    def _requeue(conn: Connection[DictRow], row: DictRow) -> Job | str:
        """Put a stale job back in the queue; the error to fail it with if it cannot.

        Tried, not checked first: an identical render already pending (or submitted
        concurrently) makes the requeue violate the pending-key unique index, and a
        SELECT beforehand would only narrow that race, not close it. The savepoint
        confines the violation to the requeue. `claims` is left as it is: every
        submitter coalesced onto the job is still waiting on it, and a supersede from
        one of them must release only that one's claim."""
        try:
            with conn.transaction():
                back = conn.execute(
                    "UPDATE render_jobs SET state = 'pending', started_at = NULL,"
                    " heartbeat_at = NULL, diagnostics = '[]'::jsonb,"
                    " diagnostics_dropped = 0, warnings = '[]'::jsonb"
                    " WHERE id = %s RETURNING *",
                    (row["id"],),
                ).fetchone()
        except UniqueViolation:
            return TWIN_QUEUED_ERROR
        assert back is not None
        return _job(back)

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
                    self._announce(conn, superseded, "job.superseded")
            if job.state == "done":
                # Already answered, from the render kept under the template: on the
                # row for the status poll, past the pending-key index and the limit.
                conn.execute(
                    "INSERT INTO render_jobs"
                    " (id, slug, params, inputs, model_version, state, created_at, started_at,"
                    "  finished_at, log_tail, result, diagnostics, diagnostics_dropped,"
                    "  render_key)"
                    " VALUES (%s, %s, %s, %s, %s, 'done', %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        job.id,
                        job.slug,
                        Jsonb(job.params),
                        Jsonb({"params": job.params}),
                        job.model_version,
                        job.created_at,
                        job.started_at,
                        job.finished_at,
                        Jsonb(job.log_tail),
                        Jsonb(job.result.model_dump(mode="json")) if job.result else None,
                        Jsonb([d.model_dump(mode="json") for d in job.diagnostics]),
                        job.diagnostics_dropped,
                        key,
                    ),
                )
                self._announce(conn, job, "job.done")
                return Submitted(job, cached=True, superseded=superseded)
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
                " (id, slug, params, inputs, model_version, state, created_at, render_key)"
                " VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)"
                " ON CONFLICT (render_key) WHERE state = 'pending'"
                " DO UPDATE SET claims = render_jobs.claims + 1"
                " RETURNING *, (xmax = 0) AS inserted",
                (
                    job.id,
                    job.slug,
                    Jsonb(job.params),
                    Jsonb({"params": job.params}),
                    job.model_version,
                    job.created_at,
                    key,
                ),
            ).fetchone()
            assert row is not None
            if row["inserted"]:
                # A coalesced submit queued nothing new, so no worker has more to do.
                _notify(conn)
                self._announce(conn, _job(row), "job.pending")
        return Submitted(_job(row), coalesced=not row["inserted"], superseded=superseded)

    def claim(self) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE render_jobs SET state = 'running', started_at = now(),"
                " heartbeat_at = now(), attempts = attempts + 1"
                " WHERE id = ("
                "  SELECT id FROM render_jobs WHERE state = 'pending' AND workflow_id IS NULL"
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

    def finish(self, job: Job, *, announce: JobKind | None = None) -> bool:
        with self._pool.connection() as conn, conn.transaction():
            cursor = conn.execute(
                "UPDATE render_jobs SET state = %s, started_at = %s, finished_at = %s,"
                " log_tail = %s, error = %s, result = %s, heartbeat_at = NULL,"
                " diagnostics = %s, diagnostics_dropped = %s, warnings = %s"
                " WHERE id = %s AND state = 'running' AND attempts = %s",
                (
                    job.state,
                    job.started_at,
                    job.finished_at,
                    Jsonb(job.log_tail),
                    job.error,
                    Jsonb(job.result.model_dump(mode="json")) if job.result is not None else None,
                    Jsonb([diagnostic.model_dump(mode="json") for diagnostic in job.diagnostics]),
                    job.diagnostics_dropped,
                    Jsonb(job.warnings),
                    job.id,
                    job.attempt,
                ),
            )
            landed = cursor.rowcount == 1
            if landed and announce is not None:
                self._announce(conn, job, announce)
        return landed

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


#: The render queue's name for the process's shared LISTEN connection (#348).
QueueListener = PgListener
