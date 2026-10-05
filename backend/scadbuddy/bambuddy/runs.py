"""The print dialog's runs, answered with 202 and followed to the end (#470, #1052).

``POST /print/outputs/{id}/run`` used to upload, slice, wait for every slice and queue
inside the one request, which the proxies in front cut long before that (Envoy's route
timeout is 15 s). Then (#470) it answered 202 and ran the rest as a task in the API
process, which a restart lost. Now each run is a Temporal workflow, ``PrintRun``
(``workflows/printing.py``, spec 2026-10-01 §5): the route starts it with
update-with-start, and its first activity makes the cheap refusals and inserts the run
here. ``GET /print/runs/{id}`` reads the row, so any replica can answer it.

The row is written only by the workflow's activities (:class:`PrintRunStore`), each
write with its ``print.run`` event in the same transaction.

Idempotency
-----------
A run's key is the subject plus the parsed request body (:func:`run_key`), including
the caller's ``request_id``: one per deliberate Print, reused by every retry of it. The
workflow ID is ``print-<key>``, so a repeat while the run is in flight attaches to it.
With a ``request_id`` a repeat answers with the recorded run for as long as the row is
kept (``print_run_retention_seconds``, :meth:`PrintRunStore.find`); without one, #470's
rule holds: for ``REPEAT_WINDOW`` after a success, or after a failure once it had tried
to queue (``may_have_queued``).

Once a run has tried to queue (``enqueue_attempted``, set by :meth:`PrintRunStore.
start_enqueue` before the first ``POST /queue/``) the print may be on Bambuddy's queue
even if the run then failed: that POST timed out, or plate 1 was queued before plate 2
failed. Such a run is ``may_have_queued``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.print_run import PrintRunRequest, PrintRunResult
from scadbuddy.core.events import Event, PrintRunEvent

logger = logging.getLogger(__name__)

RunStatus = Literal["running", "succeeded", "failed"]

#: How long a succeeded run answers a repeat of its request instead of a new print,
#: for a client that sends no ``request_id``. Long enough for any retry of a request a
#: proxy cut.
REPEAT_WINDOW = timedelta(minutes=10)

UNEXPECTED_DETAIL = "ScadBuddy failed unexpectedly while preparing this print; see its logs."

_COLUMNS = (
    "id, output_id, status, created_at, finished_at, result, error,"
    " (status = 'failed' AND enqueue_attempted) AS may_have_queued"
)


class PrintRunError(BaseModel):
    """Why a run failed: the problem document the route answered with before #470."""

    #: The problem's ``type``, e.g. ``bambuddy-unavailable`` for a Bambuddy call that
    #: timed out or dropped, as a synchronous answer would have carried it.
    type: str = "about:blank"
    #: The HTTP status that problem carries: 422 for a choice or slot the resolver
    #: refuses, 502 for a slice Bambuddy failed, 504 for one that never finished, and
    #: Bambuddy's own scope-aware 4xx/5xx for a call it refused.
    status: int
    title: str
    #: What to show the user, verbatim.
    detail: str
    #: The problem's extension members, e.g. ``slice_job_id`` for a failed slice.
    extensions: dict[str, Any] = Field(default_factory=dict)


class PrintRun(BaseModel):
    """One ``POST .../run``, as ``GET /print/runs/{id}`` reads it."""

    id: str
    output_id: str
    #: ``running`` until the print is queued (``succeeded``) or refused (``failed``).
    #: Both are final.
    status: RunStatus
    created_at: datetime
    finished_at: datetime | None = None
    #: What was queued, once ``succeeded``: warnings, queue item ids, the Bambuddy URL.
    result: PrintRunResult | None = None
    #: Why not, once ``failed``.
    error: PrintRunError | None = None
    #: A ``failed`` run that had already tried to queue the print: it may be on
    #: Bambuddy's queue anyway, so check there before printing again. A repeat of the
    #: request answers with this run rather than queueing again, for ``REPEAT_WINDOW``.
    #: ``error.detail`` does not say so itself: clients add that advice from this flag.
    may_have_queued: bool = False
    #: Only on a ``POST .../run`` answered 200: this is an earlier run with the same key,
    #: and the POST started nothing.
    repeated: bool = False


LOST_DETAIL = (
    "This print's run ended without recording an outcome, after it had started queueing "
    "it, so ScadBuddy cannot tell whether the print was queued."
)
LOST_UNQUEUED_DETAIL = (
    "This print's run ended without recording an outcome, before it queued anything. "
    "Nothing was queued; print again to retry."
)
#: A pre-#1052 pod beat its run's ``heartbeat_at`` and expired it past this: its
#: ``LOST_AFTER``.
PRE_1052_LOST_AFTER = timedelta(seconds=60)
#: A run whose execution closed, or is gone, while its row still said ``running``: one
#: terminated or reset in the Temporal UI (review #1061, :func:`reconcile_lost_runs`).
LOST = PrintRunError(status=500, title="Internal Server Error", detail=LOST_DETAIL)
LOST_UNQUEUED = LOST.model_copy(update={"detail": LOST_UNQUEUED_DETAIL})


def run_key(output_id: str, request: PrintRunRequest) -> str:
    """The output plus the request as parsed, so key order and spacing do not matter.

    ``request_id``, ``print_sequence``, ``rack_position`` and ``rack_algorithm`` are part
    of it when sent; without them the key is what it was before the fields existed.
    """
    optional = ("request_id", "print_sequence", "rack_position", "rack_algorithm")
    exclude = {name for name in optional if getattr(request, name) is None}
    body = request.model_dump(mode="json", exclude=exclude or None)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{output_id}\n{canonical}".encode()).hexdigest()


class DatabaseRequiredError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("print runs need the database; set SCADBUDDY_DATABASE_URL (#401)")


class TransactionalEvents(Protocol):
    """Publishes an event inside a caller's transaction (`PgNotifyEventBus`)."""

    def publish_in(self, conn: Connection[Any], event: Event) -> None: ...


class PrintRunStore:
    """``print_runs`` (migrations ``20260928T1200Z_print_runs``,
    ``*_print_runs_on_temporal``), on the process's pool. Every method runs its queries
    in a worker thread; times are the database's ``now()``.

    Only the ``PrintRun`` workflow's activities write here (spec 2026-10-01 §5.4), and
    every write that changes a row publishes ``print.run`` in the same transaction.
    """

    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]] | None,
        *,
        events: TransactionalEvents | None = None,
        repeat_window: timedelta = REPEAT_WINDOW,
    ) -> None:
        self._pool = pool
        self.events = events
        self.repeat_window = repeat_window

    @property
    def available(self) -> bool:
        """False without a database: the routes answer 503 before touching the store."""
        return self._pool is not None

    def _require(self) -> ConnectionPool[Connection[DictRow]]:
        if self._pool is None:
            raise DatabaseRequiredError
        return self._pool

    async def find(self, key: str, *, has_request_id: bool) -> PrintRun | None:
        """The run a request with ``key`` repeats. With a ``request_id`` that is the key's
        newest run, for as long as ``print_run_retention_seconds`` keeps it: one press is
        one print. Without, one in flight, or one that succeeded or may have queued
        within ``repeat_window`` (#470)."""
        return await asyncio.to_thread(self._find, key, has_request_id)

    async def get(self, run_id: str) -> PrintRun | None:
        return await asyncio.to_thread(self._get, run_id)

    async def insert_accepted(
        self,
        run_id: str,
        *,
        subject: str,
        key: str,
        slug: str,
        workflow_id: str,
        workflow_run_id: str,
        retention: timedelta | None,
    ) -> PrintRun:
        """Record an accepted run; the execution's row if it has one already (a retried
        activity, §4.2 step 3), announced only when inserted. Prunes runs that finished
        more than ``retention`` ago; ``None`` keeps every one."""
        return await asyncio.to_thread(
            self._insert, run_id, subject, key, slug, workflow_id, workflow_run_id, retention
        )

    async def start_enqueue(self, run_id: str) -> None:
        """Record that the run is about to queue: from here a failure may have queued."""
        await asyncio.to_thread(self._start_enqueue, run_id)

    async def succeed(self, run_id: str, slug: str, result: PrintRunResult) -> PrintRun:
        return await asyncio.to_thread(
            self._finish, run_id, slug, "succeeded", "result", result.model_dump(mode="json")
        )

    async def fail(self, run_id: str, slug: str, error: PrintRunError) -> PrintRun:
        return await asyncio.to_thread(
            self._finish, run_id, slug, "failed", "error", error.model_dump(mode="json")
        )

    async def fail_lost(self, run_id: str) -> PrintRun:
        """End a run its execution will never end: ``LOST`` once it had tried to queue,
        ``LOST_UNQUEUED`` before. A run that has ended is left as it is."""
        return await asyncio.to_thread(self._fail_lost, run_id)

    async def running_executions(self, older_than: timedelta) -> list[tuple[str, str, str]]:
        """``(run id, workflow id, workflow run id)`` of each run still ``running`` that
        was accepted more than ``older_than`` ago."""
        return await asyncio.to_thread(self._running_executions, older_than)

    async def stale_pre_1052_runs(self) -> list[str]:
        """Ids of the ``running`` rows a pre-#1052 pod inserted during the rolling update
        and stopped beating: it died mid-run, and no execution will end them (review
        #1061 (3) 2). Goes with ``heartbeat_at`` (review #1061 3a)."""
        return await asyncio.to_thread(self._stale_pre_1052_runs)

    # The blocking bodies, run in a worker thread by the coroutines above.

    def _announce(self, conn: Connection[DictRow], run: PrintRun, slug: str) -> None:
        if self.events is not None:
            self.events.publish_in(
                conn, PrintRunEvent(output_id=run.output_id, slug=slug, run_id=run.id)
            )

    def _find(self, key: str, has_request_id: bool) -> PrintRun | None:
        if has_request_id:
            query = (
                f"SELECT {_COLUMNS} FROM print_runs WHERE idempotency_key = %s"
                " ORDER BY created_at DESC LIMIT 1"
            )
            args: tuple[Any, ...] = (key,)
        else:
            query = (
                f"SELECT {_COLUMNS} FROM print_runs WHERE idempotency_key = %s"
                " AND (status = 'running'"
                "  OR ((status = 'succeeded' OR (status = 'failed' AND enqueue_attempted))"
                "      AND finished_at >= now() - %s))"
                " ORDER BY created_at DESC LIMIT 1"
            )
            args = (key, self.repeat_window)
        with self._require().connection() as conn:
            row = conn.execute(query, args).fetchone()
        return PrintRun.model_validate(row) if row else None

    def _get(self, run_id: str) -> PrintRun | None:
        with self._require().connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE id = %s", (run_id,)
            ).fetchone()
        return PrintRun.model_validate(row) if row else None

    def _insert(
        self,
        run_id: str,
        subject: str,
        key: str,
        slug: str,
        workflow_id: str,
        workflow_run_id: str,
        retention: timedelta | None,
    ) -> PrintRun:
        with self._require().connection() as conn, conn.transaction():
            if retention is not None:
                conn.execute("DELETE FROM print_runs WHERE finished_at < now() - %s", (retention,))
            row = conn.execute(
                "INSERT INTO print_runs"
                " (id, output_id, idempotency_key, status, workflow_id, workflow_run_id, slug)"
                " VALUES (%s, %s, %s, 'running', %s, %s, %s)"
                " ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING"
                f" RETURNING {_COLUMNS}",
                (run_id, subject, key, workflow_id, workflow_run_id, slug),
            ).fetchone()
            if row is not None:
                run = PrintRun.model_validate(row)
                self._announce(conn, run, slug)
                return run
            existing = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs"
                " WHERE workflow_id = %s AND workflow_run_id = %s",
                (workflow_id, workflow_run_id),
            ).fetchone()
        assert existing is not None  # the conflict was on this pair
        return PrintRun.model_validate(existing)

    def _fail_lost(self, run_id: str) -> PrintRun:
        with self._require().connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE print_runs SET status = 'failed', finished_at = now(),"
                " error = CASE WHEN enqueue_attempted THEN %s ELSE %s END"
                f" WHERE id = %s AND status = 'running' RETURNING {_COLUMNS}, slug",
                (
                    Jsonb(LOST.model_dump(mode="json")),
                    Jsonb(LOST_UNQUEUED.model_dump(mode="json")),
                    run_id,
                ),
            ).fetchone()
            if row is not None:
                run = PrintRun.model_validate(row)
                self._announce(conn, run, row["slug"] or "")
                return run
            current = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE id = %s", (run_id,)
            ).fetchone()
        if current is None:
            raise LookupError(f"there is no print run {run_id}")
        return PrintRun.model_validate(current)

    def _running_executions(self, older_than: timedelta) -> list[tuple[str, str, str]]:
        with self._require().connection() as conn:
            rows = conn.execute(
                "SELECT id, workflow_id, workflow_run_id FROM print_runs"
                " WHERE status = 'running' AND workflow_id IS NOT NULL"
                " AND created_at <= now() - %s ORDER BY created_at",
                (older_than,),
            ).fetchall()
        return [(row["id"], row["workflow_id"], row["workflow_run_id"]) for row in rows]

    def _stale_pre_1052_runs(self) -> list[str]:
        with self._require().connection() as conn:
            rows = conn.execute(
                "SELECT id FROM print_runs WHERE status = 'running' AND workflow_id IS NULL"
                " AND (heartbeat_at < now() - %s"
                # Never beaten: the old pod died before its first beat, so the row kept
                # the column's default (review #1316 3).
                " OR (heartbeat_at = 'infinity' AND created_at < now() - %s))"
                " ORDER BY created_at",
                (PRE_1052_LOST_AFTER, PRE_1052_LOST_AFTER),
            ).fetchall()
        return [row["id"] for row in rows]

    def _start_enqueue(self, run_id: str) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "UPDATE print_runs SET enqueue_attempted = true"
                " WHERE id = %s AND status = 'running'",
                (run_id,),
            )

    def _finish(
        self, run_id: str, slug: str, state: RunStatus, column: str, value: dict[str, Any]
    ) -> PrintRun:
        # Only a run still `running`: a retried end finds it ended and changes nothing.
        with self._require().connection() as conn, conn.transaction():
            row = conn.execute(
                f"UPDATE print_runs SET status = %s, {column} = %s, finished_at = now()"
                f" WHERE id = %s AND status = 'running' RETURNING {_COLUMNS}",
                (state, Jsonb(value), run_id),
            ).fetchone()
            if row is not None:
                run = PrintRun.model_validate(row)
                self._announce(conn, run, slug)
                return run
            current = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE id = %s", (run_id,)
            ).fetchone()
        if current is None:
            raise LookupError(f"there is no print run {run_id}")
        return PrintRun.model_validate(current)
