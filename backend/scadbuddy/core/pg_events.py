"""The event bus on Postgres (spec §7, §9; #264, #266): ``LISTEN``/``NOTIFY`` for
delivery, and an append-only ``events`` table for ``Last-Event-ID`` replay.

Publishing
----------
Every publish is one transaction that

1. takes `EVENT_LOG_LOCK` (``pg_advisory_xact_lock``),
2. appends the event to ``events``, which numbers it (``seq``), and
3. sends ``pg_notify('scadbuddy_events', encode_event(event))``.

Postgres delivers a NOTIFY only when its transaction commits ("if a NOTIFY is
executed inside a transaction, the notify events are not delivered until and
unless the transaction is committed"), so **no event is heard before its log row is
readable**, and a rolled-back change announces nothing. The lock is held to commit,
so log rows commit in ``seq`` order: a reader that has seen ``seq`` N never later
finds a smaller one appear behind it (a bare identity column would allow that, as
numbers are handed out at insert and transactions commit in any order). Events are
rare and each transaction is short, so serialising them costs nothing noticeable.

Two paths lead there:

- :meth:`PgNotifyEventBus.publish_in` writes the event **inside the caller's
  transaction** -- the render queue's submit, reap and finish (`pg_store`) -- so the
  event commits, or is rolled back, with the change it describes. It runs in a
  savepoint: a failing log write is logged and never fails the change.
- :meth:`PgNotifyEventBus.publish` is for changes that are not in the database (the
  git-backed catalogue, outputs, settings, fonts). It never blocks and is safe from
  any thread: the event goes into an outbox -- `_Outbox`, the same bounded,
  drop-oldest, ``call_soon_threadsafe`` hand-off #321's subscriptions use -- and a
  task on the event loop drains it in short autocommit transactions. Publishes
  before :meth:`~PgNotifyEventBus.start` wait in a bounded buffer.

Either way each event is encoded once: the payload whose size was checked is the
one logged and NOTIFYed, carried through the outbox beside its event.

Neither delivers locally. This process hears its own NOTIFY like every other
replica, so each subscriber sees each event exactly once whoever published it.

Payload size. Postgres caps a NOTIFY payload: "in the default configuration it must
be shorter than 8000 bytes" (https://www.postgresql.org/docs/current/sql-notify.html).
Events carry ids only and are a few hundred bytes; an encoded event over
`MAX_PAYLOAD_BYTES` (half that limit) is refused with a logged error and counted
(``scadbuddy_events_dropped_total{reason="oversize"}``) rather than truncated, since
a truncated payload is not an event any listener could decode.

Listening
---------
One `PgListener` connection per process -- the one the render queue already holds
for ``scadbuddy_render_queue`` -- also LISTENs on ``scadbuddy_events``. Each payload
is decoded into an embedded :class:`~.events.InProcessEventBus`, whose
:meth:`subscribe` this bus exposes unchanged.

**Resync.** A NOTIFY sent while that connection was down reached nobody here. When
it comes back after a drop, the bus delivers a ``bus.resync`` event
(:class:`~.events.BusResync`) to every subscription whatever its filter, carrying the
last event id it received before the gap. A subscriber re-reads what it follows, or
replays the log after that id (:meth:`position` then :meth:`replay`). The first
connect sends none: nothing could have been missed by subscribers that did not exist.

The replay log
--------------
``events`` (migration 3 in `pg_store.MIGRATIONS`) keeps each published event under a
monotonically increasing ``seq``. :meth:`replay` reads the events after a ``seq``,
oldest first, with a limit, and says whether rows the caller has not seen were
already pruned. It is pruned by age and by row count
(``SCADBUDDY_EVENT_LOG_RETENTION_SECONDS``, ``SCADBUDDY_EVENT_LOG_RETENTION_ROWS``)
every `PRUNE_INTERVAL`, by every replica; the deletes are idempotent.

Consuming it (a backend WebSocket gateway, #406)
------------------------------------------------
A gateway in this process uses the bus it finds in ``AppState.events`` and these
calls only, all on the event loop:

1. ``subscription = bus.subscribe(kinds=...)`` **first**, so nothing published while
   it catches up is lost. It is an async iterator of events; ``subscription.dropped``
   counts what it lost by falling behind.
2. A reconnecting client's ``Last-Event-ID``: live events carry their
   :attr:`~.events.BaseEvent.id`, not a ``seq``, so send that id as the frame id and
   turn it back into a place with ``after = await bus.position(last_id)``. ``None``
   (unknown or pruned) means resync.
3. ``page = await bus.replay(after, limit=...)`` until a page comes back empty,
   passing the last ``LoggedEvent.seq`` each time. ``page.gap`` means rows were
   pruned past the client's place: resync instead.
4. Then stream the subscription, skipping the ids the replay already sent (they can
   be in both).
5. A ``bus.resync`` event (:class:`~.events.BusResync`), or ``dropped`` growing,
   means this process missed events: replay from ``position(last_event_id)`` or tell
   the client to resync. Close the subscription (``async with`` or ``close()``) when
   the socket goes.

``subscribe`` is the :class:`~.events.EventBus` protocol, so it works on the
in-process bus too; ``position``/``replay``/``bus.resync`` exist only here, where
there is a database. The agent service LISTENs on ``scadbuddy_events`` itself, on its
own connection, and reads the same ``events`` table.

Channels are per database and the log is per schema: two deployments sharing one
database in different schemas would hear each other's events. Give each its own
database.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection, Connection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from scadbuddy.core.events import (
    DEFAULT_QUEUE_SIZE,
    PG_CHANNEL,
    BusResync,
    Event,
    InProcessEventBus,
    Subscription,
    decode_event,
    encode_event,
)
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.pg_listener import PgListener

logger = logging.getLogger(__name__)

#: Postgres refuses a NOTIFY payload of 8000 bytes or more (default build). Half of
#: it leaves room for any envelope a later version adds; events are ~200 bytes.
POSTGRES_NOTIFY_LIMIT = 8000
MAX_PAYLOAD_BYTES = POSTGRES_NOTIFY_LIMIT // 2

#: The log table (created by `pg_store.MIGRATIONS`).
EVENTS_TABLE = "events"
#: `pg_advisory_xact_lock` key serialising log appends ("SCADEVNT" in ASCII).
EVENT_LOG_LOCK = 0x5343_4144_4556_4E54

#: Events the outbox holds for the database before its oldest are dropped.
DEFAULT_OUTBOX_SIZE = 1024
#: At most this many outbox events go in one transaction.
WRITE_BATCH = 100
#: How often each replica prunes the log.
PRUNE_INTERVAL = 300.0
#: The most events one :meth:`PgNotifyEventBus.replay` returns.
MAX_REPLAY_LIMIT = 1000
#: How long :meth:`PgNotifyEventBus.aclose` waits for the outbox to drain.
CLOSE_TIMEOUT = 5.0

_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s)"
_INSERT_SQL = (
    "INSERT INTO events (event_id, kind, at, payload) VALUES (%s, %s, %s, %s::jsonb) RETURNING seq"
)
_NOTIFY_SQL = "SELECT pg_notify(%s, %s)"


#: An event and its encoded payload, as the outbox carries them.
Outgoing = tuple[Event, str]


class _Outbox:
    """The publish path's queue: bounded, oldest dropped (and counted) first, fed
    from any thread and drained on ``loop``. #321's `Subscription`, holding each
    event with the payload `publish` encoded, so the writer need not encode it
    again."""

    def __init__(self, *, maxsize: int, loop: asyncio.AbstractEventLoop) -> None:
        self.maxsize = maxsize
        #: Events lost by the outbox running more than ``maxsize`` behind.
        self.dropped = 0
        self._loop = loop
        self._queue: deque[Outgoing] = deque()
        self._ready = asyncio.Event()
        self._closed = False

    def offer(self, item: Outgoing) -> None:
        """Hand ``item`` over from any thread. Never blocks, never raises."""
        if self._closed:
            return
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._put(item)
            return
        try:
            self._loop.call_soon_threadsafe(self._put, item)
        except RuntimeError:
            self._closed = True  # the loop is gone: nobody is left to drain

    def _put(self, item: Outgoing) -> None:
        if self._closed:
            return
        if len(self._queue) >= self.maxsize:
            self._queue.popleft()
            self.dropped += 1
        self._queue.append(item)
        self._ready.set()

    def get_nowait(self) -> Outgoing | None:
        return self._queue.popleft() if self._queue else None

    async def get(self) -> Outgoing | None:
        """The next item, or ``None`` once closed and drained."""
        while not self._queue:
            if self._closed:
                return None
            self._ready.clear()
            await self._ready.wait()
        return self._queue.popleft()

    def close(self) -> None:
        """Take nothing more; `get` ends once what is queued is drained."""
        self._closed = True
        self._ready.set()


class EventLogMissingError(RuntimeError):
    """The bus was started on a database the job store has not migrated."""


@dataclass(frozen=True)
class EventLogRetention:
    """How much of the log to keep. 0 is no limit on that dimension."""

    seconds: float = 0.0
    rows: int = 0


@dataclass(frozen=True)
class LoggedEvent:
    #: The event's place in the log: what a client sends back as ``Last-Event-ID``.
    seq: int
    event: Event


@dataclass(frozen=True)
class Replay:
    #: The logged events after the requested ``seq``, oldest first.
    events: list[LoggedEvent]
    #: True when some of what followed that ``seq`` has been pruned (or may have
    #: been: a rolled-back publish also skips a number): the caller cannot catch up
    #: from the log alone and should resync.
    gap: bool


def notify_payload(event: Event, metrics: Metrics | None = None) -> str | None:
    """``encode_event(event)``, or ``None`` (logged and counted) when it is over
    `MAX_PAYLOAD_BYTES` and Postgres could not be relied on to carry it."""
    payload = encode_event(event)
    size = len(payload.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        logger.error(
            "refused to publish an event over the NOTIFY payload cap",
            extra={"kind": event.kind, "bytes": size, "limit": MAX_PAYLOAD_BYTES},
        )
        if metrics is not None:
            metrics.events_dropped.labels("oversize").inc()
        return None
    return payload


def write_event(conn: Connection[Any], event: Event, payload: str) -> int:
    """Append ``event`` to the log and NOTIFY it, in ``conn``'s transaction.
    Returns its ``seq``. Runs the three steps of the module docstring."""
    conn.execute(_LOCK_SQL, (EVENT_LOG_LOCK,))
    row = conn.execute(_INSERT_SQL, (event.id, event.kind, event.at, payload)).fetchone()
    assert row is not None
    conn.execute(_NOTIFY_SQL, (PG_CHANNEL, payload))
    return int(_first(row))


async def _awrite_event(conn: AsyncConnection[Any], event: Event, payload: str) -> int:
    await conn.execute(_LOCK_SQL, (EVENT_LOG_LOCK,))
    cursor = await conn.execute(_INSERT_SQL, (event.id, event.kind, event.at, payload))
    row = await cursor.fetchone()
    assert row is not None
    await conn.execute(_NOTIFY_SQL, (PG_CHANNEL, payload))
    return int(_first(row))


def _first(row: Any) -> Any:
    # Callers' connections may use a tuple or a dict row factory.
    return row["seq"] if isinstance(row, dict) else row[0]


class PgNotifyEventBus:
    """The :class:`~.events.EventBus` on Postgres. See the module docstring."""

    def __init__(
        self,
        conninfo: str,
        *,
        listener: PgListener,
        metrics: Metrics | None = None,
        retention: EventLogRetention | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        outbox_size: int = DEFAULT_OUTBOX_SIZE,
        connect_timeout: float = 30.0,
        prune_interval: float = PRUNE_INTERVAL,
    ) -> None:
        self.metrics = metrics if metrics is not None else Metrics()
        self.retention = retention or EventLogRetention()
        self.listener = listener
        self.connect_timeout = connect_timeout
        self.prune_interval = prune_interval
        self.outbox_size = outbox_size
        #: Delivery within this process, fed only by what the listener hears.
        self.local = InProcessEventBus(queue_size=queue_size)
        #: The last event heard before now; what a ``bus.resync`` names.
        self.last_event_id: str | None = None
        self._pool: AsyncConnectionPool[AsyncConnection[TupleRow]] = AsyncConnectionPool(
            conninfo,
            min_size=1,
            max_size=2,
            open=False,
            kwargs={"autocommit": True},
            name="scadbuddy-events",
        )
        self._lock = threading.Lock()
        self._early: deque[Outgoing] = deque()
        self._outbox: _Outbox | None = None
        self._outbox_dropped = 0
        self._closed = False
        self._tasks: list[asyncio.Task[None]] = []
        self._drainer: asyncio.Task[None] | None = None
        listener.listen(PG_CHANNEL, on_notify=self._received, on_connect=self._connected)

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect, then start draining the outbox, listening and pruning.

        The ``events`` table must exist: open the job store (which migrates) first.
        Raises `EventLogMissingError` when it does not, rather than failing later
        in every write. A failed start closes the pool it opened, so the caller's
        `aclose` -- in a ``finally`` a failed start never reaches -- is not needed
        to release it."""
        try:
            await self._pool.open(wait=True, timeout=self.connect_timeout)
            async with self._pool.connection() as conn:
                cursor = await conn.execute("SELECT to_regclass(%s)", (EVENTS_TABLE,))
                row = await cursor.fetchone()
            if row is None or row[0] is None:
                raise EventLogMissingError(
                    f"the {EVENTS_TABLE!r} table does not exist: open the job store, which "
                    "migrates the database, before starting the event bus"
                )
        except BaseException:
            await self._pool.close()
            raise
        outbox = _Outbox(maxsize=self.outbox_size, loop=asyncio.get_running_loop())
        with self._lock:
            self._outbox = outbox
            early, self._early = list(self._early), deque()
        for item in early:
            outbox.offer(item)
        self._drainer = asyncio.create_task(self._drain(outbox))
        self._tasks = [
            asyncio.create_task(self.listener.run()),
            asyncio.create_task(self._pruner()),
        ]

    async def aclose(self) -> None:
        with self._lock:
            self._closed = True
            outbox = self._outbox
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if outbox is not None:
            outbox.close()  # the drainer finishes what is queued, then ends
        if self._drainer is not None:
            try:
                await asyncio.wait_for(self._drainer, CLOSE_TIMEOUT)
            except (TimeoutError, asyncio.CancelledError):
                logger.warning("closed the event bus with events still unwritten")
            self._drainer = None
        await self._pool.close()
        await self.local.aclose()

    # -- EventBus -------------------------------------------------------------------

    def publish(self, event: Event) -> None:
        """Queue ``event`` for the log and NOTIFY. Non-blocking, thread-safe, and
        never raises for a database problem (that is logged and counted).

        Encodes ``event`` once, here: the payload checked against the cap is the
        one the outbox carries to the log and the NOTIFY."""
        payload = notify_payload(event, self.metrics)
        if payload is None:
            return
        with self._lock:
            outbox = self._outbox
            if self._closed:
                logger.warning(
                    "an event was published after the bus closed", extra={"kind": event.kind}
                )
                return
            if outbox is None:
                if len(self._early) >= self.outbox_size:
                    self._early.popleft()
                    self.metrics.events_dropped.labels("outbox_full").inc()
                self._early.append((event, payload))
                return
        outbox.offer((event, payload))

    def publish_in(self, conn: Connection[Any], event: Event) -> None:
        """Log and NOTIFY ``event`` in ``conn``'s open transaction, so it is heard on
        commit and never after a rollback. A savepoint confines a failure to the
        event: it is logged and counted, and the caller's change goes on. (On a
        connection with no transaction open, it opens and commits its own.)

        Synchronous: call it from the worker thread that holds ``conn``."""
        payload = notify_payload(event, self.metrics)
        if payload is None:
            return
        try:
            with conn.transaction():
                write_event(conn, event, payload)
        except Exception:
            logger.exception("could not publish an event", extra={"kind": event.kind})
            self.metrics.events_dropped.labels("error").inc()
            return
        self.metrics.events_published.inc()

    def subscribe(
        self, *, kinds: Collection[str] | None = None, maxsize: int | None = None
    ) -> Subscription:
        return self.local.subscribe(kinds=kinds, maxsize=maxsize)

    def add_listener(self, listener: Callable[[Event], None]) -> Callable[[], None]:
        """See :meth:`InProcessEventBus.add_listener`: called for each event heard."""
        return self.local.add_listener(listener)

    @property
    def subscriber_count(self) -> int:
        return self.local.subscriber_count

    # -- the replay log -------------------------------------------------------------

    async def replay(self, after: int, *, limit: int = 100) -> Replay:
        """The logged events with ``seq`` > ``after``, oldest first, at most
        ``limit`` (1 to `MAX_REPLAY_LIMIT`). Page by passing the last ``seq``."""
        if not 1 <= limit <= MAX_REPLAY_LIMIT:
            raise ValueError(f"limit must be 1 to {MAX_REPLAY_LIMIT}, not {limit}")
        if after < 0:
            raise ValueError(f"after must be at least 0, not {after}")
        async with self._pool.connection() as conn, conn.transaction():
            # One snapshot for both reads, so the gap check and the page agree.
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor = await conn.execute(
                "SELECT coalesce((SELECT min(seq) FROM events),"
                " coalesce(pg_sequence_last_value(pg_get_serial_sequence('events', 'seq')), 0)"
                " + 1)"
            )
            floor_row = await cursor.fetchone()
            cursor = await conn.execute(
                "SELECT seq, payload::text FROM events WHERE seq > %s ORDER BY seq LIMIT %s",
                (after, limit),
            )
            rows = await cursor.fetchall()
        assert floor_row is not None
        events = [LoggedEvent(seq=int(seq), event=decode_event(payload)) for seq, payload in rows]
        return Replay(events=events, gap=after < int(floor_row[0]) - 1)

    async def position(self, event_id: str) -> int | None:
        """The ``seq`` of the event with this :attr:`~.events.BaseEvent.id`, or
        ``None`` when it was never logged or has been pruned. A live consumer sees
        event ids; this turns the last one into a place to :meth:`replay` from."""
        async with self._pool.connection() as conn:
            cursor = await conn.execute("SELECT seq FROM events WHERE event_id = %s", (event_id,))
            row = await cursor.fetchone()
        return None if row is None else int(row[0])

    async def prune_log(self) -> int:
        """Apply the retention settings; returns the rows removed."""
        removed = 0
        async with self._pool.connection() as conn:
            if self.retention.seconds > 0:
                cursor = await conn.execute(
                    "DELETE FROM events WHERE logged_at < now() - make_interval(secs => %s)",
                    (self.retention.seconds,),
                )
                removed += max(0, cursor.rowcount)
            if self.retention.rows > 0:
                cursor = await conn.execute(
                    "DELETE FROM events WHERE seq <= ("
                    " SELECT seq FROM events ORDER BY seq DESC OFFSET %s LIMIT 1)",
                    (self.retention.rows,),
                )
                removed += max(0, cursor.rowcount)
        if removed:
            self.metrics.event_log_pruned.inc(removed)
        return removed

    # -- internals ------------------------------------------------------------------

    async def _drain(self, outbox: _Outbox) -> None:
        while (item := await outbox.get()) is not None:
            batch = [item]
            while len(batch) < WRITE_BATCH and (more := outbox.get_nowait()) is not None:
                batch.append(more)
            if outbox.dropped != self._outbox_dropped:
                lost, self._outbox_dropped = outbox.dropped - self._outbox_dropped, outbox.dropped
                logger.error("the event outbox overflowed", extra={"dropped": lost})
                self.metrics.events_dropped.labels("outbox_full").inc(lost)
            try:
                await self._write(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("could not publish events", extra={"count": len(batch)})
                self.metrics.events_dropped.labels("error").inc(len(batch))

    async def _write(self, batch: list[Outgoing]) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            for event, payload in batch:  # encoded, and size-checked, in `publish`
                await _awrite_event(conn, event, payload)
        self.metrics.events_published.inc(len(batch))

    async def _pruner(self) -> None:
        while True:
            try:
                await self.prune_log()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("could not prune the event log")
            await asyncio.sleep(self.prune_interval)

    def _received(self, payload: str) -> None:
        try:
            event = decode_event(payload)
        except ValueError:
            # Another version's kind, or something else NOTIFYing on the channel.
            logger.warning("ignored an undecodable event payload", extra={"bytes": len(payload)})
            return
        self.last_event_id = event.id
        self.metrics.events_received.inc()
        self.local.publish(event)

    def _connected(self, reconnected: bool) -> None:
        if not reconnected:
            return
        logger.warning(
            "the event listener reconnected; subscribers are told to resync",
            extra={"last_event_id": self.last_event_id},
        )
        self.metrics.events_resyncs.inc()
        self.local.publish(BusResync(last_event_id=self.last_event_id))
