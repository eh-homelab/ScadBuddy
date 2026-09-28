"""One server-side watcher per active print (#268).

Before this, a print moved only when someone asked: every open print dialog polled
``GET /print/outputs/{id}/progress`` every 2 s, and the backend turned those reads
into ``print.*`` events (:class:`~scadbuddy.bambuddy.progress.ProgressObserver`).
Now the backend follows each print itself, once, from the moment a send or run
starts it until it is ``settled``, and the UI follows the ``print:<output id>``
topic on the realtime socket (#266).

Why polling, with back-off
--------------------------
Bambuddy 1.2.5.5 does have a push channel, ``WS /api/v1/ws``, which an API key can
join with a token from ``POST /api/v1/auth/ws-token`` (``can_read_status``;
bambuddy ``backend/app/api/routes/auth.py`` ``mint_websocket_token``). It cannot
replace reading, though:
- nothing is broadcast for a slice job;
- completion is ``print_complete`` per *printer*, not per queue item;
- ``pipeline_run_updated`` goes through ``broadcast_to_user(run.created_by)``.
(bambuddy ``backend/app/core/websocket.py`` ``send_*``;
``backend/app/api/routes/pipeline_runs.py``.)

So the watcher keeps reading through :func:`progress_for`, the rules the progress
route already uses (#89, #233, #240): every ``MIN_INTERVAL`` while the print is
moving, doubling to ``MAX_INTERVAL`` while nothing changes. That is one reader per
print, whoever is looking.

Lifecycle
---------
- :meth:`PrintWatcher.started` records when a send or run started a print, in the
  :class:`PrintLog`, and follows it.
- :meth:`PrintWatcher.start` resumes every output printed within ``MAX_AGE`` (from
  the log), so a restart does not lose a print. It rescans every
  ``RESCAN_INTERVAL``, so a print another replica was watching when it died is
  picked up. A print that settles, 404s or whose output is deleted is forgotten, so
  no rescan reads it again.
- The progress route calls :meth:`PrintWatcher.watch` for a print that has not
  settled, so someone looking at a print brings its watcher back: one sent before
  the watcher existed, one a restart without a database forgot, one that went quiet.
- ``MAX_AGE`` counts from the latest of the print's start, the watch or its last
  poke, and the last change seen: a long print that keeps moving is followed to the
  end, and a forgotten quiet one is not followed for ever.
- With a database, ``print.*`` events reach every replica through the Postgres event
  bus (``core/pg_events.py``), so a UI on another replica than the one following the
  print hears them too.
- With ``SCADBUDDY_DATABASE_URL`` set, the log is the ``print_watches`` table
  (``migrations/20260928T0718Z_print_watches.sql``) and a Postgres session advisory lock makes
  sure one replica follows each print (:class:`WatchLock`). There is no other store
  (#401 makes the database required): without it, a print is followed from the send
  that started it, and a restart forgets it.
- A failed read publishes ``print.progress`` once per distinct failure, so the UI
  re-reads the progress route and shows the scope-aware problem it answers
  (``bambuddy/errors.py``). Events carry ids, never content (``core/events.py``).
  Then the watcher backs off to ``ERROR_INTERVAL``. A 404 means Bambuddy no longer
  has the print, and the watcher stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver
from scadbuddy.core.events import EventBus, PrintEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputNotFoundError, OutputStore

logger = logging.getLogger(__name__)

#: Seconds between reads while a print is moving.
MIN_INTERVAL = 2.0
#: The longest wait between reads of a print that is not changing.
MAX_INTERVAL = 30.0
#: The wait after a failed read (Bambuddy down, a key without the scope).
ERROR_INTERVAL = 60.0
#: A print is followed for at most this long after it was started.
MAX_AGE = timedelta(hours=24)
#: How often the prints a dead replica held are looked for.
RESCAN_INTERVAL = 300.0

#: The first key of the two-key advisory lock: "SBPW", so it can't collide with the
#: migration lock (``render/pg_store.py`` ``MIGRATION_LOCK``, a one-key lock).
WATCH_LOCK_CLASS = 0x5342_5057

Reader = Callable[[OutputMeta], Awaitable[PrintProgress | None]]


def _now() -> datetime:
    return datetime.now(UTC)


class PrintLog(Protocol):
    """When each output's last print was started."""

    async def record(self, output_id: str, at: datetime) -> None: ...

    async def printed_at(self, output_id: str) -> datetime | None: ...

    async def since(self, cutoff: datetime) -> list[str]: ...

    async def forget(self, output_id: str) -> None: ...

    async def aclose(self) -> None: ...


class _PgSession:
    """One autocommit connection, reopened when it drops, used one query at a time."""

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        self._conn: AsyncConnection[TupleRow] | None = None
        self.lock = asyncio.Lock()

    async def connection(self) -> AsyncConnection[TupleRow]:
        if self._conn is None or self._conn.closed:
            self._conn = await AsyncConnection.connect(
                self.conninfo,
                autocommit=True,
                application_name="scadbuddy-print-watcher",
            )
        return self._conn

    @property
    def open(self) -> bool:
        return self._conn is not None and not self._conn.closed

    async def aclose(self) -> None:
        async with self.lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None


class PgPrintLog:
    """The ``print_watches`` table. The render store's ``open`` applies the migration
    that creates it, before the watcher starts (``main.py`` lifespan)."""

    def __init__(self, conninfo: str) -> None:
        self._session = _PgSession(conninfo)

    async def record(self, output_id: str, at: datetime) -> None:
        async with self._session.lock:
            conn = await self._session.connection()
            await conn.execute(
                "INSERT INTO print_watches (output_id, printed_at) VALUES (%s, %s)"
                " ON CONFLICT (output_id) DO UPDATE SET printed_at = EXCLUDED.printed_at",
                (output_id, at),
            )

    async def printed_at(self, output_id: str) -> datetime | None:
        async with self._session.lock:
            conn = await self._session.connection()
            cursor = await conn.execute(
                "SELECT printed_at FROM print_watches WHERE output_id = %s", (output_id,)
            )
            row = await cursor.fetchone()
        return row[0] if row else None

    async def since(self, cutoff: datetime) -> list[str]:
        async with self._session.lock:
            conn = await self._session.connection()
            cursor = await conn.execute(
                "SELECT output_id FROM print_watches WHERE printed_at >= %s", (cutoff,)
            )
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def forget(self, output_id: str) -> None:
        async with self._session.lock:
            conn = await self._session.connection()
            await conn.execute("DELETE FROM print_watches WHERE output_id = %s", (output_id,))

    async def aclose(self) -> None:
        await self._session.aclose()


class WatchLock(Protocol):
    """Who follows a print, when several processes could."""

    async def acquire(self, output_id: str) -> bool: ...

    async def release(self, output_id: str) -> None: ...

    async def aclose(self) -> None: ...


class LocalWatchLock:
    """One process, no database: the watcher's own bookkeeping is the lock."""

    async def acquire(self, output_id: str) -> bool:
        return True

    async def release(self, output_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


class PgWatchLock:
    """A session advisory lock per print, all held on one connection per process.

    Session locks end with the session, so a replica that dies releases its prints,
    and the others take them over at their next rescan
    (https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS).
    If this connection drops, its locks go with it: another replica may then follow
    a print this one is still following. That costs duplicate reads and events,
    which consumers treat as a re-read; it does not cost correctness.
    """

    def __init__(self, conninfo: str) -> None:
        self._session = _PgSession(conninfo)

    async def acquire(self, output_id: str) -> bool:
        async with self._session.lock:
            conn = await self._session.connection()
            cursor = await conn.execute(
                "SELECT pg_try_advisory_lock(%s, hashtext(%s))", (WATCH_LOCK_CLASS, output_id)
            )
            row = await cursor.fetchone()
        return bool(row and row[0])

    async def release(self, output_id: str) -> None:
        async with self._session.lock:
            # A dropped connection took its locks with it.
            if not self._session.open:
                return
            conn = await self._session.connection()
            await conn.execute(
                "SELECT pg_advisory_unlock(%s, hashtext(%s))", (WATCH_LOCK_CLASS, output_id)
            )

    async def aclose(self) -> None:
        await self._session.aclose()


class PrintWatcher:
    def __init__(
        self,
        *,
        outputs: OutputStore,
        observer: ProgressObserver,
        read: Reader,
        events: EventBus | None,
        prints: PrintLog | None = None,
        lock: WatchLock | None = None,
        min_interval: float = MIN_INTERVAL,
        max_interval: float = MAX_INTERVAL,
        error_interval: float = ERROR_INTERVAL,
        max_age: timedelta = MAX_AGE,
        rescan_interval: float = RESCAN_INTERVAL,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self.outputs = outputs
        self.observer = observer
        self.read = read
        self.events = events
        self.prints = prints
        self.lock = lock or LocalWatchLock()
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.error_interval = error_interval
        self.max_age = max_age
        self.rescan_interval = rescan_interval
        self.now = now
        self._tasks: dict[str, asyncio.Task[None]] = {}
        #: Set to cut a follower's wait short: a new print of an output already followed.
        self._pokes: dict[str, asyncio.Event] = {}
        self._rescanner: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def watching(self) -> frozenset[str]:
        return frozenset(self._tasks)

    async def started(self, output_id: str) -> None:
        """A send or run started a print of ``output_id``: record it, and follow it."""
        try:
            if self.prints is not None:
                await self.prints.record(output_id, self.now())
        except Exception:
            # The print was sent; this process follows it, but a restart would not.
            logger.exception("could not record a started print", extra={"output_id": output_id})
        self.watch(output_id)

    def watch(self, output_id: str) -> None:
        """Follow ``output_id``'s print; if it is already followed, read it again now."""
        if output_id in self._tasks:
            self._pokes[output_id].set()
            return
        self._pokes[output_id] = asyncio.Event()
        task = asyncio.create_task(self._follow(output_id), name=f"print-watcher-{output_id}")
        self._tasks[output_id] = task
        task.add_done_callback(lambda _: self._forget(output_id, task))

    def _forget(self, output_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(output_id) is not task:
            return
        del self._tasks[output_id]
        poke = self._pokes.pop(output_id, None)
        # A watch() that came after the follower's last wait (while it released its
        # lock, say) poked a task that no longer reads: that print starts over now
        # rather than at the next rescan.
        if poke is not None and poke.is_set() and not self._closing:
            self.watch(output_id)

    async def start(self) -> None:
        await self.resume()
        if self.rescan_interval > 0:
            self._rescanner = asyncio.create_task(self._rescan(), name="print-watcher-rescan")

    async def resume(self) -> None:
        """Follow every output printed within ``max_age`` that nobody follows."""
        if self.prints is None:
            return
        try:
            recent = await self.prints.since(self.now() - self.max_age)
        except Exception:
            logger.exception("could not list recent prints to resume watching")
            return
        for output_id in recent:
            if output_id not in self._tasks:
                self.watch(output_id)

    async def _rescan(self) -> None:
        while True:
            await asyncio.sleep(self.rescan_interval)
            await self.resume()

    async def aclose(self) -> None:
        self._closing = True
        tasks = [*self._tasks.values(), *([self._rescanner] if self._rescanner else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.lock.aclose()
        if self.prints is not None:
            await self.prints.aclose()

    async def _wait(self, output_id: str, seconds: float) -> bool:
        """Wait ``seconds``, or less if poked; True if it was poked."""
        poke = self._pokes[output_id]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(poke.wait(), seconds)
        poked = poke.is_set()
        poke.clear()
        return poked

    async def _follow(self, output_id: str) -> None:
        try:
            held = await self.lock.acquire(output_id)
        except Exception:
            logger.exception("could not take the watch lock", extra={"output_id": output_id})
            return
        if not held:
            return
        try:
            await self._loop(output_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("the print watcher failed", extra={"output_id": output_id})
        finally:
            with contextlib.suppress(Exception):
                await asyncio.shield(self.lock.release(output_id))

    async def _done(self, output_id: str) -> None:
        """The print is over (settled, gone, or never was): stop resuming it."""
        if self.prints is None:
            return
        try:
            await self.prints.forget(output_id)
        except Exception:
            # The next rescan reads it once more and forgets it then.
            logger.exception("could not forget a finished print", extra={"output_id": output_id})

    async def _loop(self, output_id: str) -> None:
        interval = self.min_interval
        last_failure: tuple[int, str] | None = None
        # The age limit counts from the latest of: the print's start, this watch or its
        # last poke (a new print, or someone reading its progress), and the last change
        # seen. A long print that keeps moving, or one someone is looking at, is
        # followed to the end; a forgotten quiet one is not followed forever.
        active = self.now()
        while True:
            # Waits first: the send or run that started the print answered with its
            # own state, and the UI reads once when it subscribes.
            if await self._wait(output_id, interval):
                active = self.now()
            printed_at: datetime | None = None
            if self.prints is not None:
                try:
                    printed_at = await self.prints.printed_at(output_id)
                except Exception:
                    # The database is briefly away: keep watching, slowly.
                    logger.exception("could not read the print log", extra={"output_id": output_id})
                    interval = self.error_interval
                    continue
            if self.now() - max(active, printed_at or active) > self.max_age:
                return
            try:
                meta = await asyncio.to_thread(self.outputs.get, output_id)
            except OutputNotFoundError:
                await self._done(output_id)
                return
            try:
                progress = await self.read(meta)
            except ApiError as error:
                failure = (error.status, error.detail)
                if failure != last_failure:
                    last_failure = failure
                    emit(
                        self.events,
                        PrintEvent(kind="print.progress", output_id=meta.id, slug=meta.slug),
                    )
                if error.status == 404:
                    await self._done(output_id)
                    return
                interval = self.error_interval
                continue
            except Exception:
                # Not Bambuddy's answer (a bug, a broken setting): keep watching, slowly.
                logger.exception("a print progress read failed", extra={"output_id": output_id})
                interval = self.error_interval
                continue
            last_failure = None
            changed = self.observer.observe(meta, progress)
            if progress is None or progress.settled:
                await self._done(output_id)
                return
            if changed:
                active = self.now()
            interval = self.min_interval if changed else min(self.max_interval, interval * 2)
