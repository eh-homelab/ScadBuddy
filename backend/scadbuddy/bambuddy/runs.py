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
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Literal, LiteralString, Protocol

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, computed_field, model_validator

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.bambuddy.activity import row_of
from scadbuddy.bambuddy.print_run import PrintRunRequest, PrintRunResult
from scadbuddy.bambuddy.progress import PrintProgress, from_failed_run, queue_url
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.events import Event, PrintRunEvent
from scadbuddy.core.problems import DATABASE_ERRORS
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.lan_models import LanStopped

logger = logging.getLogger(__name__)

RunStatus = Literal["running", "succeeded", "failed"]

#: How long a succeeded run answers a repeat of its request instead of a new print,
#: for a client that sends no ``request_id``. Long enough for any retry of a request a
#: proxy cut.
REPEAT_WINDOW = timedelta(minutes=10)

UNEXPECTED_DETAIL = "ScadBuddy failed unexpectedly while preparing this print; see its logs."

_COLUMNS = (
    "id, output_id, status, created_at, finished_at, result, error,"
    " (status = 'failed' AND enqueue_attempted) AS may_have_queued,"
    " workflow_id, workflow_run_id, printer::text AS printer"
)
#: A run that may still read its subject's files (`PrintRunStore.running_for`): one still
#: running, or a native run with prints still to start (`print_runs_lan_waiting`'s
#: predicate), which it starts from its sliced files later (#4541).
_IN_USE = (
    "(status = 'running' OR (status = 'succeeded'"
    " AND result->'lan_waiting' <> '[]'::jsonb AND result->>'lan_stopped' IS NULL))"
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
    #: What the run prints, as a print subject's key: ``output:<id>`` or
    #: ``library:<file id>`` (#1750).
    subject: str
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
    #: The registry printer the run prints on (#3823), whichever field named it; ``None``
    #: for a run from before the registry, or one whose printer was deleted since.
    printer: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_output_id(cls, data: Any) -> Any:
        # A row (its column is still ``output_id``) and a run serialized before #1750
        # (Temporal history, the accept Update) carry the run subject only.
        if isinstance(data, dict) and "subject" not in data and "output_id" in data:
            data = {**data, "subject": PrintSubject.from_run_subject(data["output_id"]).key}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def output_id(self) -> str:
        """The subject as clients read it before #1750: an output's id, or
        ``library:<file id>``. Kept so they work unchanged; read ``subject``."""
        return PrintSubject.parse(self.subject).run_subject


LOST_DETAIL = (
    "This print's run ended without recording an outcome, after it had started queueing "
    "it, so ScadBuddy cannot tell whether the print was queued."
)
LOST_UNQUEUED_DETAIL = (
    "This print's run ended without recording an outcome, before it queued anything. "
    "Nothing was queued; print again to retry."
)
#: A run whose execution closed, or is gone, while its row still said ``running``: one
#: terminated or reset in the Temporal UI (review #1061, :func:`reconcile_lost_runs`).
LOST = PrintRunError(status=500, title="Internal Server Error", detail=LOST_DETAIL)
LOST_UNQUEUED = LOST.model_copy(update={"detail": LOST_UNQUEUED_DETAIL})


def run_key(output_id: str, request: PrintRunRequest) -> str:
    """The run subject (``PrintSubject.run_subject``) plus the request as parsed, so key
    order and spacing do not matter.

    ``request_id``, ``print_sequence``, ``rack_position``, ``rack_algorithm``,
    ``filament_profiles``, ``supports`` (#1899), ``prime_tower`` (#3628), ``plate_ids``
    (#3652), ``folder_id`` (#3651) and ``printer`` (#3823) are part of it when sent;
    without them the key is what it was before the fields existed. ``printer`` counts
    only without ``printer_id``, which decides when both are sent (#4442 I1, M1): the
    dialog sent it, ignored, before the backend read it, so its keys stay as they were
    across the deploy, and a retry that straddles it joins its run.
    """
    optional = (
        "request_id",
        "print_sequence",
        "rack_position",
        "rack_algorithm",
        "supports",
        "prime_tower",
        "plate_ids",
        "folder_id",
        "printer",
    )
    exclude = {name for name in optional if getattr(request, name) is None}
    if request.printer_id is not None:
        exclude.add("printer")
    # #717: no profiles is the key from before the field existed.
    if not request.filament_profiles:
        exclude.add("filament_profiles")
    body = request.model_dump(mode="json", exclude=exclude or None)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{output_id}\n{canonical}".encode()).hexdigest()


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
        pool: ConnectionPool[Connection[DictRow]],
        *,
        events: TransactionalEvents | None = None,
        repeat_window: timedelta = REPEAT_WINDOW,
        activity: ActivityIndex | None = None,
    ) -> None:
        self._pool = pool
        self.events = events
        self.activity = activity
        self.repeat_window = repeat_window

    async def find(self, key: str, *, has_request_id: bool) -> PrintRun | None:
        """The run a request with ``key`` repeats. With a ``request_id`` that is the key's
        newest run, for as long as ``print_run_retention_seconds`` keeps it: one press is
        one print. Without, one in flight, or one that succeeded or may have queued
        within ``repeat_window`` (#470)."""
        return await asyncio.to_thread(self._find, key, has_request_id)

    async def get(self, run_id: str) -> PrintRun | None:
        return await asyncio.to_thread(self._get, run_id)

    async def get_many(self, run_ids: list[str]) -> dict[str, PrintRun]:
        """The runs of ``run_ids`` still kept, by id, in one read (#2718 review)."""
        if not run_ids:
            return {}
        return await asyncio.to_thread(self._get_many, run_ids)

    async def latest_for_output(self, output_id: str) -> PrintRun | None:
        """The output's newest run, for as long as retention keeps it (#1049): the one
        place a run that failed before it queued anything is recorded."""
        return await asyncio.to_thread(self._latest_for_output, output_id)

    async def lan_waiting(self, printers: list[str]) -> dict[str, PrintRun]:
        """Each of ``printers``' newest succeeded native run with prints still to start,
        neither all started nor stopped (#4942), by printer, in one read: the Prints page
        card's controls."""
        if not printers:
            return {}
        return await asyncio.to_thread(self._lan_waiting, printers)

    async def record_lan_stopped(self, run_id: str, stopped: LanStopped) -> PrintRun | None:
        """Record ``stopped`` on a succeeded native run whose prints still to start no
        sequence will start (its ``PrintLanSequence`` closed without recording its end,
        #4942 review), and announce it as the sequence would have. A run that has none
        left, or records a stop already, is left as it is. ``None`` for no such run."""
        return await asyncio.to_thread(self._record_lan_stopped, run_id, stopped)

    async def running_for(self, output_id: str) -> list[PrintRun]:
        """The output's runs that may still read a slice of it, newest first (#4312
        re-review I-a): those still ``running``, and a succeeded native run with prints
        still to start, which it starts from its sliced files later (#4541, #4337)."""
        return await asyncio.to_thread(self._running_for, output_id)

    async def lan_waiting_runs(self) -> list[tuple[str, str | None]]:
        """Every succeeded native run with prints still to start, neither all started nor
        stopped, whatever its printer, as its id and its ``PrintRun`` workflow's id: the
        runs whose sequence may have closed without saying so (#4337)."""
        return await asyncio.to_thread(self._lan_waiting_runs)

    async def subjects_in_use(self) -> set[str]:
        """Every run subject (``PrintRun.output_id``) one of whose runs `running_for`
        names: what the per-print copies' sweep leaves alone (#4337)."""
        return await asyncio.to_thread(self._subjects_in_use)

    async def failed_before_queueing(self, output_ids: list[str]) -> set[str]:
        """Which of ``output_ids`` have a newest run that failed before it queued
        anything (#1831), in one query: what ``/progress`` reports as the failure."""
        return await asyncio.to_thread(self._failed_before_queueing, output_ids)

    async def insert_accepted(
        self,
        run_id: str,
        *,
        subject: PrintSubject,
        key: str,
        slug: str,
        workflow_id: str,
        workflow_run_id: str,
        retention: timedelta | None,
        printer: str | None = None,
    ) -> PrintRun:
        """Record an accepted run of ``subject``; the execution's row if it has one
        already (a retried activity, §4.2 step 3), announced only when inserted. Prunes
        runs that finished more than ``retention`` ago; ``None`` keeps every one."""
        return await asyncio.to_thread(
            self._insert,
            run_id,
            subject.run_subject,
            key,
            slug,
            workflow_id,
            workflow_run_id,
            retention,
            printer,
        )

    async def start_enqueue(self, run_id: str) -> None:
        """Record that the run is about to queue: from here a failure may have queued."""
        await asyncio.to_thread(self._start_enqueue, run_id)

    async def succeed(self, run_id: str, slug: str, result: PrintRunResult) -> PrintRun:
        return await asyncio.to_thread(
            self._finish, run_id, slug, "succeeded", "result", result.model_dump(mode="json")
        )

    async def update_result(self, run_id: str, slug: str, result: PrintRunResult) -> PrintRun:
        """Rewrite a succeeded run's result: a LAN run's later starts, each made once the
        printer has finished the one before (#4541). Only a ``succeeded`` run changes."""
        return await asyncio.to_thread(
            self._update_result, run_id, slug, result.model_dump(mode="json")
        )

    async def fail(
        self, run_id: str, slug: str, error: PrintRunError, *, unqueued: bool = False
    ) -> PrintRun:
        """End the run failed; ``unqueued``, it stopped before any ``POST /queue/``
        although it had called :meth:`start_enqueue`, so it may not have queued."""
        return await asyncio.to_thread(
            self._finish,
            run_id,
            slug,
            "failed",
            "error",
            error.model_dump(mode="json"),
            unqueued,
        )

    async def fail_lost(self, run_id: str) -> PrintRun:
        """End a run its execution will never end: ``LOST`` once it had tried to queue,
        ``LOST_UNQUEUED`` before. A run that has ended is left as it is."""
        return await asyncio.to_thread(self._fail_lost, run_id)

    async def running_executions(self, older_than: timedelta) -> list[tuple[str, str, str]]:
        """``(run id, workflow id, workflow run id)`` of each run still ``running`` that
        was accepted more than ``older_than`` ago."""
        return await asyncio.to_thread(self._running_executions, older_than)

    # The blocking bodies, run in a worker thread by the coroutines above.

    def _announce(self, conn: Connection[DictRow], run: PrintRun, slug: str, row: DictRow) -> None:
        if self.activity is not None:
            self.activity.upsert_in(
                conn, lambda: row_of(run, slug, row["workflow_id"], row["workflow_run_id"])
            )
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
        with self._pool.connection() as conn:
            row = conn.execute(query, args).fetchone()
        return PrintRun.model_validate(row) if row else None

    def _latest_for_output(self, output_id: str) -> PrintRun | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE output_id = %s"
                " ORDER BY created_at DESC LIMIT 1",
                (output_id,),
            ).fetchone()
        return PrintRun.model_validate(row) if row else None

    def _lan_waiting(self, printers: list[str]) -> dict[str, PrintRun]:
        # The predicate is `print_runs_lan_waiting`'s, so the read is that index's.
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT ON (printer) {_COLUMNS} FROM print_runs"
                " WHERE printer = ANY(%s::uuid[]) AND status = 'succeeded'"
                " AND result->'lan_waiting' <> '[]'::jsonb"
                " AND result->>'lan_stopped' IS NULL"
                " ORDER BY printer, created_at DESC",
                (printers,),
            ).fetchall()
        runs = [PrintRun.model_validate(row) for row in rows]
        return {run.printer: run for run in runs if run.printer is not None}

    def _record_lan_stopped(self, run_id: str, stopped: LanStopped) -> PrintRun | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"SELECT {_COLUMNS}, slug FROM print_runs WHERE id = %s FOR UPDATE", (run_id,)
            ).fetchone()
            if row is None:
                return None
            slug = row.pop("slug") or ""
            run = PrintRun.model_validate(row)
            result = run.result
            if (
                run.status != "succeeded"
                or result is None
                or not result.lan_waiting
                or result.lan_stopped is not None
            ):
                return run
            value = result.model_copy(update={"lan_stopped": stopped, "lan_awaiting_clear": False})
            updated = conn.execute(
                f"UPDATE print_runs SET result = %s WHERE id = %s RETURNING {_COLUMNS}",
                (Jsonb(value.model_dump(mode="json")), run_id),
            ).fetchone()
            assert updated is not None  # the row is locked
            run = PrintRun.model_validate(updated)
            self._announce(conn, run, slug, updated)
            return run

    def _running_for(self, output_id: str) -> list[PrintRun]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE output_id = %s AND {_IN_USE}"
                " ORDER BY created_at DESC".encode(),
                (output_id,),
            ).fetchall()
        return [PrintRun.model_validate(row) for row in rows]

    def _lan_waiting_runs(self) -> list[tuple[str, str | None]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, workflow_id FROM print_runs WHERE status = 'succeeded'"
                " AND result->'lan_waiting' <> '[]'::jsonb"
                " AND result->>'lan_stopped' IS NULL ORDER BY created_at"
            ).fetchall()
        return [(row["id"], row["workflow_id"]) for row in rows]

    def _subjects_in_use(self) -> set[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT output_id FROM print_runs WHERE {_IN_USE}".encode()
            ).fetchall()
        return {row["output_id"] for row in rows}

    def _failed_before_queueing(self, output_ids: list[str]) -> set[str]:
        if not output_ids:
            return set()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT output_id FROM ("
                " SELECT DISTINCT ON (output_id) output_id, status, enqueue_attempted"
                " FROM print_runs WHERE output_id = ANY(%s)"
                " ORDER BY output_id, created_at DESC"
                ") latest WHERE status = 'failed' AND NOT enqueue_attempted",
                (output_ids,),
            ).fetchall()
        return {row["output_id"] for row in rows}

    def _get(self, run_id: str) -> PrintRun | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE id = %s", (run_id,)
            ).fetchone()
        return PrintRun.model_validate(row) if row else None

    def _get_many(self, run_ids: list[str]) -> dict[str, PrintRun]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE id = ANY(%s)", (run_ids,)
            ).fetchall()
        runs = [PrintRun.model_validate(row) for row in rows]
        return {run.id: run for run in runs}

    def _insert(
        self,
        run_id: str,
        subject: str,
        key: str,
        slug: str,
        workflow_id: str,
        workflow_run_id: str,
        retention: timedelta | None,
        printer: str | None = None,
    ) -> PrintRun:
        with self._pool.connection() as conn, conn.transaction():
            if retention is not None:
                conn.execute("DELETE FROM print_runs WHERE finished_at < now() - %s", (retention,))
            row = conn.execute(
                "INSERT INTO print_runs"
                " (id, output_id, idempotency_key, status, workflow_id, workflow_run_id, slug,"
                "  printer)"
                " VALUES (%s, %s, %s, 'running', %s, %s, %s,"
                # A row deleted since the check leaves the run unnamed, not refused.
                "  (SELECT id FROM printers WHERE id = %s::uuid))"
                " ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING"
                f" RETURNING {_COLUMNS}",
                (run_id, subject, key, workflow_id, workflow_run_id, slug, printer),
            ).fetchone()
            if row is not None:
                run = PrintRun.model_validate(row)
                self._announce(conn, run, slug, row)
                return run
            existing = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs"
                " WHERE workflow_id = %s AND workflow_run_id = %s",
                (workflow_id, workflow_run_id),
            ).fetchone()
        assert existing is not None  # the conflict was on this pair
        return PrintRun.model_validate(existing)

    def _fail_lost(self, run_id: str) -> PrintRun:
        return self._end_running(
            run_id,
            "error = CASE WHEN enqueue_attempted THEN %s ELSE %s END",
            (Jsonb(LOST.model_dump(mode="json")), Jsonb(LOST_UNQUEUED.model_dump(mode="json"))),
        )

    def _end_running(
        self,
        run_id: str,
        assignments: LiteralString,
        params: tuple[Jsonb, ...],
    ) -> PrintRun:
        """Fail a ``running`` run with ``assignments`` and announce it; any other is
        answered as it is."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"UPDATE print_runs SET status = 'failed', finished_at = now(), {assignments}"
                " WHERE id = %s AND status = 'running'"
                f" RETURNING {_COLUMNS}, slug",
                (*params, run_id),
            ).fetchone()
            if row is not None:
                run = PrintRun.model_validate(row)
                self._announce(conn, run, row["slug"] or "", row)
                return run
            current = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE id = %s", (run_id,)
            ).fetchone()
        if current is None:
            raise LookupError(f"there is no print run {run_id}")
        return PrintRun.model_validate(current)

    def _running_executions(self, older_than: timedelta) -> list[tuple[str, str, str]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, workflow_id, workflow_run_id FROM print_runs"
                " WHERE status = 'running' AND workflow_id IS NOT NULL"
                " AND created_at <= now() - %s ORDER BY created_at",
                (older_than,),
            ).fetchall()
        return [(row["id"], row["workflow_id"], row["workflow_run_id"]) for row in rows]

    def _update_result(self, run_id: str, slug: str, value: dict[str, Any]) -> PrintRun:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE print_runs SET result = %s WHERE id = %s AND status = 'succeeded'"
                f" RETURNING {_COLUMNS}",
                (Jsonb(value), run_id),
            ).fetchone()
            if row is not None:
                run = PrintRun.model_validate(row)
                self._announce(conn, run, slug, row)
                return run
            current = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE id = %s", (run_id,)
            ).fetchone()
        if current is None:
            raise LookupError(f"there is no print run {run_id}")
        return PrintRun.model_validate(current)

    def _start_enqueue(self, run_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE print_runs SET enqueue_attempted = true"
                " WHERE id = %s AND status = 'running'",
                (run_id,),
            )

    def _finish(
        self,
        run_id: str,
        slug: str,
        state: RunStatus,
        column: str,
        value: dict[str, Any],
        unqueued: bool = False,
    ) -> PrintRun:
        # Only a run still `running`: a retried end finds it ended and changes nothing.
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"UPDATE print_runs SET status = %s, {column} = %s, finished_at = now(),"
                " enqueue_attempted = enqueue_attempted AND NOT %s"
                f" WHERE id = %s AND status = 'running' RETURNING {_COLUMNS}",
                (state, Jsonb(value), unqueued, run_id),
            ).fetchone()
            if row is not None:
                run = PrintRun.model_validate(row)
                self._announce(conn, run, slug, row)
                return run
            current = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE id = %s", (run_id,)
            ).fetchone()
        if current is None:
            raise LookupError(f"there is no print run {run_id}")
        return PrintRun.model_validate(current)


#: The newest run of a run subject (`PrintRunStore.latest_for_output`, or one read already).
LatestRun = Callable[[str], Awaitable["PrintRun | None"]]


def given_run(run: PrintRun | None) -> LatestRun:
    """A `LatestRun` that answers ``run``, read already: a progress route reads the
    subject's newest run once, for its failure and its LAN print alike."""

    async def latest(run_subject: str) -> PrintRun | None:
        return run

    return latest


class RecentRuns:
    """`PrintRunStore.latest_for_output`, kept per subject for ``ttl`` seconds: a follow's
    tick asks for the newest failure and the LAN print (plan 2026-10-16 Task 9) of one
    subject within it, and they share one query. A subject's ``print.run`` event drops
    its kept run (`heard`, an `EventBus` listener), so a tick just after a retry reads
    the new run rather than the one before it (#4541)."""

    def __init__(
        self,
        runs: PrintRunStore,
        *,
        ttl: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        capacity: int = 256,
    ) -> None:
        self.runs = runs
        self.ttl = ttl
        self.clock = clock
        self.capacity = capacity
        self._kept: dict[str, tuple[float, PrintRun | None]] = {}
        # `heard` runs on the publishing thread, `latest` on the loop.
        self._lock = threading.Lock()
        #: How many ``print.run`` events were heard, of any subject: a read that spans
        #: one may answer the run before it, so it is not kept. One counter, not one per
        #: subject, so nothing has to be evicted to bound it, and no eviction can make a
        #: read that spanned an event look as if it had not (#4566 review). Another
        #: subject's event costs a read only its keep, and only when it lands inside the
        #: read's query (3.2 ms median, measured for #4596 over 10,000 rows): with two
        #: events per run (its insert and its end), even 50 runs started in one second
        #: drop about 27% of that second's keeps, each one indexed query on the next read.
        self._heard = 0

    async def latest(self, run_subject: str) -> PrintRun | None:
        now = self.clock()
        with self._lock:
            kept = self._kept.get(run_subject)
            if kept is not None and now - kept[0] < self.ttl:
                return kept[1]
            heard = self._heard
        run = await self.runs.latest_for_output(run_subject)
        with self._lock:
            if self._heard != heard:
                return run
            if len(self._kept) >= self.capacity:
                self._kept = {k: v for k, v in self._kept.items() if now - v[0] < self.ttl}
            self._kept[run_subject] = (now, run)
        return run

    def heard(self, event: Event) -> None:
        """Drop the kept run of a ``print.run`` event's subject."""
        if not isinstance(event, PrintRunEvent):
            return
        with self._lock:
            self._kept.pop(event.output_id, None)
            self._heard += 1


async def read_latest(latest: LatestRun, run_subject: str) -> PrintRun | None:
    """The subject's newest run, or ``None`` with a database that does not answer."""
    try:
        return await latest(run_subject)
    except DATABASE_ERRORS:
        logger.warning("print runs unreadable; progress read without them")
        return None


def failure_of(latest: PrintRun | None) -> str | None:
    """Why ``latest`` failed, when it failed before it queued anything (#1049); else
    ``None``."""
    if latest is None or latest.status != "failed" or latest.may_have_queued:
        return None
    return latest.error.detail if latest.error is not None else None


async def newest_failure(
    runs: PrintRunStore, run_subject: str, *, latest: LatestRun | None = None
) -> str | None:
    """Why the newest run of ``run_subject`` (an output's id, or ``library:<file id>``,
    #1751) failed, when it failed before it queued anything (#1049); else ``None``. A
    run that may have queued recorded what it queued, so its print's own progress says
    more. With a database that does not answer, there are no runs to read, and the
    progress is read as it was before. ``latest`` reads the run in place of ``runs``.

    The one rule for what a subject's progress is: the progress routes and the print
    follow both apply it, so the two never publish different progress for it (#1837)."""
    return failure_of(await read_latest(latest or runs.latest_for_output, run_subject))


#: A run subject's newest-run failure, as the progress it shows (``Follower.newest_failed``).
NewestFailed = Callable[[str], Awaitable[PrintProgress | None]]


def newest_failed_from(
    runs: PrintRunStore,
    load: Callable[[], StoredSettings],
    *,
    latest: LatestRun | None = None,
) -> NewestFailed:
    """``Follower.newest_failed`` over ``runs`` (or ``latest``, a `RecentRuns`' read):
    the progress the routes show for a run subject whose newest run failed before
    queueing, else None."""

    async def newest_failed(run_subject: str) -> PrintProgress | None:
        detail = await newest_failure(runs, run_subject, latest=latest)
        if detail is None:
            return None
        return from_failed_run(detail, bambuddy_url=queue_url(await asyncio.to_thread(load)))

    return newest_failed
