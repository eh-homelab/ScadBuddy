"""The print dialog's runs, answered with 202 and followed to the end (#470).

``POST /print/outputs/{id}/run`` used to upload, slice, wait for every slice (up to
``DEFAULT_SLICE_TIMEOUT`` each) and queue inside the one request. The proxies in front
cut a request long before that: Envoy's default route timeout is 15 s, Cloudflare's
first-byte limit about 100 s. The browser got a non-JSON 504 while the backend went on
and queued the print, and a retry queued it again.

Now the route makes the refusals that are cheap (:func:`~scadbuddy.bambuddy.pipelines.
prepare_run`), records a run here, answers 202 with it and hands the rest to
:class:`PrintRuns`, which runs it as a task on the event loop. ``GET /print/runs/{id}``
reads the row, so any replica can answer it.

Idempotency
-----------
A run's key is the output plus the parsed request body (:func:`run_key`). Outputs are
immutable, so the two together name exactly one print. A second POST with the same key
returns the run it repeats, instead of starting another, while that run is in flight
or for ``REPEAT_WINDOW`` after it succeeded. The key is looked up and the new row
inserted under one advisory lock, so two POSTs that race get one run.

A run that failed before it tried to queue anything holds nothing: repeating it tries
again. Once it has tried (``enqueue_attempted``, set by :meth:`PrintRunStore.
start_enqueue` before the first ``POST /queue/``), the print may be on Bambuddy's queue
even if the run then failed: that POST timed out (a 504 whose item Bambuddy may still
have created), or plate 1 was queued before plate 2 failed. Such a run is
``may_have_queued`` and keeps holding its key for ``REPEAT_WINDOW`` like a success, so
a retry answers with it instead of queueing again.

Lost runs
---------
While a run is alive its task touches ``heartbeat_at`` every ``HEARTBEAT_INTERVAL``.
A ``running`` row whose heartbeat is older than ``LOST_AFTER``, and that this process
is not running itself, reads as failed: its process died, or is too slow to beat (a
database blip, a saturated thread pool) and so cannot be told from a dead one. Every
state change is a compare-and-set on ``status = 'running'``, which makes that safe:

- expiry before the run's ``start_enqueue`` wins: the run cannot queue any more (its
  ``start_enqueue`` finds the row failed and it stops), so the key is released and
  the message says nothing was queued (``LOST_UNQUEUED_DETAIL``);
- expiry after it keeps the key and says the print may be queued (``LOST_DETAIL``);
- either way the slow run's own end no longer overwrites the row.

A run this process is still running when it shuts down is failed the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import status
from fastapi.encoders import jsonable_encoder
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.pipelines import PrintRunRequest, PrintRunResult
from scadbuddy.core.events import EventBus, PrintRunEvent, emit
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)

RunStatus = Literal["running", "succeeded", "failed"]

#: How often a live run touches its heartbeat.
HEARTBEAT_INTERVAL = 10.0
#: A ``running`` run whose heartbeat is older than this was lost with its process.
LOST_AFTER = timedelta(seconds=60)
#: How long a succeeded run answers a repeat of its request instead of a new print.
#: Long enough for any retry of a request a proxy cut; a deliberate reprint of the
#: same output with the same choices inside it is the price, and ``copies`` is the
#: way to ask for more than one.
REPEAT_WINDOW = timedelta(minutes=10)
#: Finished runs are kept this long, then pruned when a new run is recorded.
RETENTION = timedelta(days=7)
#: The first key of the two-key advisory lock taken per idempotency key: "SBPR".
RUN_LOCK_CLASS = 0x5342_5052

LOST_DETAIL = (
    "ScadBuddy restarted while it was preparing this print, so it cannot tell whether "
    "the print was queued. Check Bambuddy's queue before printing again."
)
LOST_UNQUEUED_DETAIL = (
    "ScadBuddy stopped while it was preparing this print, before it queued anything. "
    "Nothing was queued; print again to retry."
)
UNEXPECTED_DETAIL = "ScadBuddy failed unexpectedly while preparing this print; see its logs."

_COLUMNS = (
    "id, output_id, status, created_at, finished_at, result, error,"
    " (status = 'failed' AND enqueue_attempted) AS may_have_queued"
)


class PrintRunError(BaseModel):
    """Why a run failed: the problem document the route answered with before #470."""

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
    may_have_queued: bool = False


#: A run whose process went away before it ended: lost to a restart, or shut down.
LOST = PrintRunError(
    status=status.HTTP_500_INTERNAL_SERVER_ERROR, title="Internal Server Error", detail=LOST_DETAIL
)
#: The same, lost before it tried to queue anything.
LOST_UNQUEUED = LOST.model_copy(update={"detail": LOST_UNQUEUED_DETAIL})


class RunLostError(RuntimeError):
    """The run was failed as lost while this process was still running it."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"print run {run_id} was failed as lost; it will not queue")


def run_key(output_id: str, request: PrintRunRequest) -> str:
    """The output plus the request as parsed, so key order and spacing do not matter."""
    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{output_id}\n{canonical}".encode()).hexdigest()


class DatabaseRequiredError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("print runs need the database; set SCADBUDDY_DATABASE_URL (#401)")


class PrintRunStore:
    """``print_runs`` (migration ``20260928T1200Z_print_runs``), on the process's pool.

    The pool is the render queue's, as :class:`~scadbuddy.bambuddy.uploads.
    BambuddyUploadStore` uses it. Every method runs its queries in a worker thread.
    Times are the database's ``now()``, so replicas agree on what is stale.
    """

    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]] | None,
        *,
        lost_after: timedelta = LOST_AFTER,
        repeat_window: timedelta = REPEAT_WINDOW,
        retention: timedelta = RETENTION,
    ) -> None:
        self._pool = pool
        self.lost_after = lost_after
        self.repeat_window = repeat_window
        self.retention = retention
        #: The runs this process is running (:class:`PrintRuns` keeps it): never
        #: expired here, however late their heartbeat.
        self.live: set[str] = set()

    def _require(self) -> ConnectionPool[Connection[DictRow]]:
        if self._pool is None:
            raise DatabaseRequiredError
        return self._pool

    async def find(self, key: str) -> PrintRun | None:
        """The run a request with ``key`` repeats: one in flight, or recently succeeded."""
        return await asyncio.to_thread(self._find, key)

    async def claim(self, output_id: str, key: str) -> tuple[PrintRun, bool]:
        """The run for ``key``: the one it repeats, else a new one; True if it is new."""
        return await asyncio.to_thread(self._claim, output_id, key)

    async def get(self, run_id: str) -> PrintRun | None:
        return await asyncio.to_thread(self._get, run_id)

    async def heartbeat(self, run_id: str) -> None:
        await asyncio.to_thread(self._heartbeat, run_id)

    async def start_enqueue(self, run_id: str) -> None:
        """Record that the run is about to queue; :class:`RunLostError` if it was lost."""
        await asyncio.to_thread(self._start_enqueue, run_id)

    async def succeed(self, run_id: str, result: PrintRunResult) -> None:
        await asyncio.to_thread(
            self._finish, run_id, "succeeded", "result", result.model_dump(mode="json")
        )

    async def fail(self, run_id: str, error: PrintRunError) -> None:
        await asyncio.to_thread(
            self._finish, run_id, "failed", "error", error.model_dump(mode="json")
        )

    # The blocking bodies, run in a worker thread by the coroutines above.

    def _expire_lost(self, conn: Connection[DictRow], column: str, value: str) -> None:
        """Fail every ``running`` row matching ``column = value`` whose process is gone.

        Not the runs this process is running, which are alive by definition.
        """
        conn.execute(
            "UPDATE print_runs SET status = 'failed', finished_at = now(),"
            " error = CASE WHEN enqueue_attempted THEN %s ELSE %s END"
            f" WHERE {column} = %s AND status = 'running' AND heartbeat_at < now() - %s"
            " AND NOT (id = ANY(%s))",
            (
                Jsonb(LOST.model_dump(mode="json")),
                Jsonb(LOST_UNQUEUED.model_dump(mode="json")),
                value,
                self.lost_after,
                list(self.live),
            ),
        )

    def _current(self, conn: Connection[DictRow], key: str) -> PrintRun | None:
        self._expire_lost(conn, "idempotency_key", key)
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM print_runs WHERE idempotency_key = %s"
            " AND (status = 'running'"
            "  OR ((status = 'succeeded' OR (status = 'failed' AND enqueue_attempted))"
            "      AND finished_at >= now() - %s))"
            " ORDER BY created_at DESC LIMIT 1",
            (key, self.repeat_window),
        ).fetchone()
        return PrintRun.model_validate(row) if row else None

    def _find(self, key: str) -> PrintRun | None:
        with self._require().connection() as conn:
            return self._current(conn, key)

    def _claim(self, output_id: str, key: str) -> tuple[PrintRun, bool]:
        with self._require().connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (RUN_LOCK_CLASS, key))
            existing = self._current(conn, key)
            if existing is not None:
                return existing, False
            conn.execute("DELETE FROM print_runs WHERE finished_at < now() - %s", (self.retention,))
            row = conn.execute(
                "INSERT INTO print_runs (id, output_id, idempotency_key, status)"
                f" VALUES (%s, %s, %s, 'running') RETURNING {_COLUMNS}",
                (uuid.uuid4().hex, output_id, key),
            ).fetchone()
        assert row is not None  # INSERT ... RETURNING always answers one row
        return PrintRun.model_validate(row), True

    def _get(self, run_id: str) -> PrintRun | None:
        with self._require().connection() as conn:
            self._expire_lost(conn, "id", run_id)
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM print_runs WHERE id = %s",
                (run_id,),
            ).fetchone()
        return PrintRun.model_validate(row) if row else None

    def _heartbeat(self, run_id: str) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "UPDATE print_runs SET heartbeat_at = now() WHERE id = %s AND status = 'running'",
                (run_id,),
            )

    def _start_enqueue(self, run_id: str) -> None:
        # The compare-and-set that orders this against `_expire_lost`: if the run was
        # expired first, its key is released and a retry may be running, so this one
        # must not queue.
        with self._require().connection() as conn:
            row = conn.execute(
                "UPDATE print_runs SET enqueue_attempted = true, heartbeat_at = now()"
                " WHERE id = %s AND status = 'running' RETURNING id",
                (run_id,),
            ).fetchone()
        if row is None:
            raise RunLostError(run_id)

    def _finish(self, run_id: str, state: RunStatus, column: str, value: dict[str, Any]) -> None:
        # Only a run still `running`: one that was failed as lost meanwhile stays as the
        # retries that read it saw it (see the module docstring).
        with self._require().connection() as conn:
            conn.execute(
                f"UPDATE print_runs SET status = %s, {column} = %s, finished_at = now(),"
                " heartbeat_at = now() WHERE id = %s AND status = 'running'",
                (state, Jsonb(value), run_id),
            )


#: Awaited by the work before each ``POST /queue/`` (:meth:`PrintRunStore.start_enqueue`).
BeforeEnqueue = Callable[[], Awaitable[None]]
Work = Callable[[BeforeEnqueue], Awaitable[PrintRunResult]]


class PrintRuns:
    """Runs each accepted print as a task on this process's loop, recording its end."""

    def __init__(
        self,
        store: PrintRunStore,
        events: EventBus | None,
        *,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
    ) -> None:
        self.store = store
        self.events = events
        self.heartbeat_interval = heartbeat_interval
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def running(self) -> frozenset[str]:
        return frozenset(self._tasks)

    def announce(self, run: PrintRun, slug: str) -> None:
        """``print.run`` on the output's topic: re-read ``GET /print/runs/{id}``."""
        emit(self.events, PrintRunEvent(output_id=run.output_id, slug=slug, run_id=run.id))

    def start(self, run: PrintRun, slug: str, work: Work) -> None:
        task = asyncio.create_task(self._run(run, slug, work), name=f"print-run-{run.id}")
        self._tasks[run.id] = task
        self.store.live.add(run.id)

        def done(_: asyncio.Task[None]) -> None:
            self._tasks.pop(run.id, None)
            self.store.live.discard(run.id)

        task.add_done_callback(done)

    async def aclose(self) -> None:
        """Fail every run still in progress: this process will not finish them."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _beat(self, run_id: str) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self.store.heartbeat(run_id)
            except Exception:
                # A missed beat costs nothing until LOST_AFTER; keep trying.
                logger.exception("could not touch a print run's heartbeat")

    async def _run(self, run: PrintRun, slug: str, work: Work) -> None:
        beat = asyncio.create_task(self._beat(run.id), name=f"print-run-beat-{run.id}")
        enqueuing = False

        async def before_enqueue() -> None:
            nonlocal enqueuing
            if not enqueuing:
                # Set first, so a cancel mid-write says "may be queued", never "nothing".
                enqueuing = True
                await self.store.start_enqueue(run.id)

        try:
            try:
                result = await work(before_enqueue)
            except ApiError as error:
                await self.store.fail(
                    run.id,
                    PrintRunError(
                        status=error.status,
                        title=error.title,
                        detail=error.detail,
                        extensions=jsonable_encoder(error.extensions),
                    ),
                )
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await asyncio.shield(
                        self.store.fail(run.id, LOST if enqueuing else LOST_UNQUEUED)
                    )
                raise
            except RunLostError:
                # Failed as lost (by another replica) before it queued; the row says so.
                logger.warning("a print run was failed as lost", extra={"run_id": run.id})
            except Exception:
                logger.exception("a print run failed", extra={"run_id": run.id})
                await self.store.fail(
                    run.id,
                    PrintRunError(
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        title="Internal Server Error",
                        detail=UNEXPECTED_DETAIL,
                    ),
                )
            else:
                await self.store.succeed(run.id, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The row stays `running`; with no heartbeat it reads as lost in LOST_AFTER.
            logger.exception("could not record a print run's end", extra={"run_id": run.id})
        finally:
            beat.cancel()
            self.announce(run, slug)
