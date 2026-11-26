"""The agent's change stream (#5299 D4, step 11): what the agent's own ``LISTEN`` connection
used to hear (the agent opens none now), served by the CRUD service as one server-sent event
stream per agent process (`api/crud_agent_changes.py`, ``GET …/crud/agent/changes``).

**One LISTEN per API process.** The hub registers the agent's five channels
(:data:`CHANNELS`) on the process's one `PgListener` connection, and taps the event bus for
``scadbuddy_events`` (`PgNotifyEventBus.taps`), so no new connection is opened. Postgres
delivers a NOTIFY to every session listening on its channel
(https://www.postgresql.org/docs/current/sql-notify.html), so each API replica hears every
NOTIFY, whichever process sent it, and fans it out to the streams it serves. That connection
hands on one notification at a time, in the order Postgres delivered them (commit order), and
each stream keeps that order: the MCP relay relies on it (``agent/src/mcp/sessionRelay.ts``).

**Never blocking a writer.** The listener's callback only appends to each stream's bounded
queue (:data:`QUEUE_MAX`). A stream whose reader falls behind past it loses its queue and is
marked with a gap, exactly as if its LISTEN connection had dropped; so does every stream when
this process's listener reconnects (anything NOTIFYed while it was down reached nobody here).
The stream's reader (:func:`frames`) then replays the event log after its place and tells the
agent ``reconnected``, so what had no row in the log (the other channels, the agent's own
``session.*``) is read again by whoever follows it, as the agent's listener did after a drop.

**Replay.** Each logged event (every one the backend publishes is in the ``events`` log,
`core/pg_events.py`) goes out with its event id as the frame's ``id``, and the ``ready`` frame
carries the newest logged id, or :data:`START` for an empty log. The kinds the agent NOTIFYs
itself and the log never holds (:data:`NOTIFY_ONLY`: ``session.*``, ``ai_operation.changed``),
and a payload the bus cannot decode, go out with no ``id``, so the agent's place stays on a
logged event. A reconnect
sends the last one as ``Last-Event-ID``; the stream subscribes first, then replays the log after
it (`PgNotifyEventBus.position`, `replay`), at most :data:`REPLAY_MAX` events, and skips the live
copies of what it replayed. An id it cannot place (pruned, malformed, never logged), a pruned
gap or a longer replay is a ``resync``: the agent re-reads everything it follows.

**Commit order.** The replay reads ``seq > position(last_id)``, so it would skip a row that
committed below the place after the agent was sent the place's event. That cannot happen:
every writer of ``events`` holds `EVENT_LOG_LOCK` until it commits (`core/pg_events.py`,
"Publishing"), so rows commit in ``seq`` order, and the place is the last logged event this
stream sent, never a guess ahead of it. The agent's old listener adopted ``max(seq)`` one
check late and so needed a guard for a transaction open longer than that check (a replayed
row's ``logged_at`` too old); this stream needs none while the lock holds. A writer of
``events`` that does not take the lock breaks this replay.

**Filtered streams.** A stream may carry a ``keep`` filter (`ChangeStream.keep`), applied in
:meth:`ChangeStream.offer` and in replay, so a replay never returns a change that live
delivery drops. A live change it drops moves no place. A replay moves the place past every
event it scans, kept or not, and counts every scanned event against :data:`REPLAY_MAX`.
Every frame id is a logged event's id, so ids stay monotonic and a ``Last-Event-ID`` always
names a place in the log. Because a dropped live change moves no place, a filtered client
usually resyncs after a long quiet run, so its controllers must not depend on replay.

**Bounds.** A ``group`` of streams (the camera callers') has its own cap, counted by group
and not by credential, and is outside the figures below. At most
:data:`STREAMS_PER_CREDENTIAL` streams per service credential and peer
address (`api/crud_agent_changes.py` `stream_key`) and :data:`STREAMS_MAX` in all are open at
once; another is refused 429. The process's shutdown ends
every stream (:meth:`ChangeHub.shutdown`), so a stream never holds the server's drain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Protocol

from scadbuddy.core.events import (
    PG_CHANNEL,
    AgentOperationEvent,
    AnyEvent,
    SessionBusEvent,
    encode_event,
)
from scadbuddy.core.pg_events import MAX_REPLAY_LIMIT, Replay

logger = logging.getLogger(__name__)

#: The agent's channels besides the event bus's: the gate's input (`agent_data/gate.py`), the
#: pending-input tables' ticks (migration ``20261103T1944Z_pending_input_notify.sql``), the MCP
#: relay (`api/crud_agent_mcp.py`), the warm process cache's evictions and the runtime settings
#: (`agent_data/settings.py`).
CHANNELS: Final = (
    "scadbuddy_input",
    "scadbuddy_pending",
    "scadbuddy_mcp",
    "scadbuddy_warm_evict",
    "scadbuddy_settings",
)
#: The bus's kinds that only the agent NOTIFYs, never in the ``events`` log: their frames carry
#: no ``id``, since a ``Last-Event-ID`` naming one could not be placed (a resync).
NOTIFY_ONLY: Final = (SessionBusEvent, AgentOperationEvent)
#: The stream's place before the first logged event: replay from the log's start.
START: Final = "0"
#: An event's id as `core/events.py` makes it (32 hex digits), or one a newer build might: a
#: ``Last-Event-ID`` outside this names no place.
EVENT_ID: Final = re.compile(r"[0-9A-Za-z-]{1,64}")
#: A stream's queue at most; past it the stream is marked with a gap (see above).
QUEUE_MAX: Final = 10_000
#: Streams at once per service credential and peer address (an agent process holds one; a
#: reconnect may overlap the old one until the server sees it gone), and in all.
STREAMS_PER_CREDENTIAL: Final = 4
STREAMS_MAX: Final = 64
#: The most logged events one reconnect replays before it resyncs instead.
REPLAY_MAX: Final = 1000
#: A comment line every so often, so the agent can tell a live stream from a half-open one.
KEEPALIVE_SECONDS: Final = 15.0


@dataclass(frozen=True)
class Change:
    channel: str
    payload: str
    #: The logged event's id (``scadbuddy_events`` only), else None.
    event_id: str | None = None


class _Gap:
    """Changes were lost here: replay the log, then tell the agent ``reconnected``."""


class _End:
    """The process is shutting down: end the stream."""


GAP: Final = _Gap()
END: Final = _End()
type Item = Change | _Gap | _End


class StreamsFullError(Exception):
    """No room for another stream: this credential's, or the process's, are all open."""


class ChangeStream:
    """One open stream's queue. Fed on the event loop, read by its :func:`frames`."""

    def __init__(
        self, key: str, limit: int = QUEUE_MAX, keep: Callable[[Change], bool] | None = None
    ) -> None:
        self.key = key
        self._limit = limit
        #: Which changes this stream carries, live and in replay; None keeps every one.
        self.keep = keep
        #: The budget this stream counts against, or None for the agent's own (set by `open`).
        self.group: str | None = None
        self._items: deque[Item] = deque()
        self._wake = asyncio.Event()
        #: Times its reader fell behind past the queue's bound (tests, logs).
        self.overflows = 0

    def offer(self, item: Item) -> None:
        if isinstance(item, Change) and self.keep is not None and not self.keep(item):
            return
        if self._items and self._items[0] is END:
            return  # told to end: nothing after it is read, and nothing may push it out
        if isinstance(item, _End):
            # Past the bound too: a stream told to end must end, or it holds the server's drain.
            self._items.clear()
            self._items.append(END)
        elif len(self._items) >= self._limit:
            self.overflows += 1
            self._items.clear()
            self._items.append(GAP)
        elif isinstance(item, _Gap) and self._items and self._items[-1] is GAP:
            pass  # one replay covers both
        else:
            self._items.append(item)
        self._wake.set()

    async def next(self, timeout: float) -> Item | None:
        """The next item, or None after ``timeout`` with nothing."""
        if not self._items:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                return None
        return self._items.popleft() if self._items else None


class ChangeHub:
    """Every stream this process serves; see the module docstring."""

    def __init__(
        self,
        *,
        queue_max: int = QUEUE_MAX,
        per_credential: int = STREAMS_PER_CREDENTIAL,
        total: int = STREAMS_MAX,
    ) -> None:
        self._queue_max = queue_max
        self._per_credential = per_credential
        self._total = total
        self._streams: set[ChangeStream] = set()
        self._closed = False

    @property
    def streams(self) -> int:
        return len(self._streams)

    def attach(self, listener: _Listener, bus: _Bus) -> None:
        """Follow the agent's channels on ``listener`` and the bus's through its tap. Before
        the listener runs: a channel registered later would miss what came first."""
        for index, channel in enumerate(CHANNELS):
            listener.listen(
                channel,
                on_notify=self._heard_on(channel),
                on_connect=self._connected if index == 0 else None,
            )
        bus.taps.append(self._heard_event)

    def _heard_on(self, channel: str) -> Callable[[str], None]:
        def heard(payload: str) -> None:
            self.publish(Change(channel, payload))

        return heard

    def open(
        self,
        key: str,
        keep: Callable[[Change], bool] | None = None,
        *,
        group: tuple[str, int] | None = None,
    ) -> ChangeStream:
        """A stream counted by ``key`` and the process total. A ``group`` (name, most) is its own
        budget instead: its streams count only against that group, whatever their ``key``, and
        never against the total, so a group cannot use up the agent's streams."""
        if self._closed:
            raise StreamsFullError("the process is shutting down")
        if group is not None:
            name, most = group
            if sum(1 for s in self._streams if s.group == name) >= most:
                raise StreamsFullError(f"{most} change streams are open for {name}")
        else:
            if sum(1 for s in self._streams if s.group is None) >= self._total:
                raise StreamsFullError(f"{self._total} change streams are open")
            if (
                sum(1 for s in self._streams if s.group is None and s.key == key)
                >= self._per_credential
            ):
                raise StreamsFullError(f"this credential has {self._per_credential} streams open")
        stream = ChangeStream(key, self._queue_max, keep)
        stream.group = group[0] if group else None
        self._streams.add(stream)
        return stream

    def close(self, stream: ChangeStream) -> None:
        self._streams.discard(stream)

    def publish(self, change: Change) -> None:
        for stream in list(self._streams):
            stream.offer(change)

    def shutdown(self) -> None:
        """End every stream and open none from now on."""
        self._closed = True
        for stream in list(self._streams):
            stream.offer(END)

    def _heard_event(self, payload: str, event: AnyEvent | None) -> None:
        logged = None if event is None or isinstance(event, NOTIFY_ONLY) else event.id
        self.publish(Change(PG_CHANNEL, payload, logged))

    def _connected(self, reconnected: bool) -> None:
        if not reconnected:
            return
        logger.warning("the LISTEN connection is back; every change stream replays its gap")
        for stream in list(self._streams):
            stream.offer(GAP)


class _Listener(Protocol):
    def listen(
        self,
        channel: str,
        *,
        on_notify: Callable[[str], Awaitable[None] | None],
        on_connect: Callable[[bool], Awaitable[None] | None] | None = None,
    ) -> None: ...


class _Bus(Protocol):
    taps: list[Callable[[str, AnyEvent | None], None]]


class Log(Protocol):
    """What the stream reads of the event log (`PgNotifyEventBus`)."""

    async def newest(self) -> str | None: ...
    async def position(self, event_id: str) -> int | None: ...
    async def replay(self, after: int, *, limit: int = 100) -> Replay: ...


def frame(kind: str, data: object, event_id: str | None = None) -> bytes:
    """One server-sent event (https://html.spec.whatwg.org/multipage/server-sent-events.html):
    its data is one line of JSON, so no payload's newline can end it early."""
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def _placeable(last_id: str | None) -> bool:
    return last_id is not None and (last_id == START or EVENT_ID.fullmatch(last_id) is not None)


@dataclass
class _Place:
    """The stream's place in the log: the last logged id sent, and the ids its replay sent."""

    last_id: str
    replayed: set[str]


async def _replay(
    log: Log, last_id: str | None, keep: Callable[[Change], bool] | None = None
) -> tuple[list[bytes], _Place, bool]:
    """The frames of the log after ``last_id``, the place after them, and whether the agent
    must resync instead (its place unknown, pruned past, or too far behind)."""
    if not _placeable(last_id):
        return [], _Place(await log.newest() or START, set()), True
    assert last_id is not None
    after = 0 if last_id == START else await log.position(last_id)
    if after is None:
        return [], _Place(await log.newest() or START, set()), True
    frames: list[bytes] = []
    place = _Place(last_id, set())
    while True:
        try:
            page = await log.replay(after, limit=MAX_REPLAY_LIMIT)
        except ValueError:
            # Only a row whose envelope is broken raises (a routable unknown kind replays as an
            # `OpaqueEvent`): replaying past it would fail the same way at every reconnect, so
            # the agent re-reads instead.
            logger.warning("the event log holds an event this build cannot decode; resyncing")
            return [], _Place(await log.newest() or START, set()), True
        if page.gap or len(place.replayed) + len(page.events) > REPLAY_MAX:
            return [], _Place(await log.newest() or START, set()), True
        if not page.events:
            return frames, place, False
        for logged in page.events:
            place.last_id = logged.event.id
            place.replayed.add(logged.event.id)
            payload = encode_event(logged.event)
            if keep is not None and not keep(Change(PG_CHANNEL, payload, logged.event.id)):
                continue  # a filtered stream's place still moves past it, but no frame goes out
            frames.append(frame("change", {"c": PG_CHANNEL, "p": payload}, logged.event.id))
        after = page.events[-1].seq


async def frames(
    stream: ChangeStream,
    log: Log,
    last_event_id: str | None,
    *,
    keepalive: float = KEEPALIVE_SECONDS,
    start: str | None = None,
) -> AsyncIterator[bytes]:
    """The stream's bytes: a reconnect's replay, ``ready``, then each change as heard.

    ``ready`` says whether the agent must ``resync`` and whether this is a reconnect (it sent
    ``Last-Event-ID``), after which it re-reads what it follows on the other channels; a
    ``reconnected`` frame says the same after a gap here. ``stream`` must be open (subscribed)
    before this runs, so nothing heard during the replay is lost. A fresh stream's place is
    ``start``, the newest logged id read before it subscribed: every live event then comes after
    it, so none moves the place back (read here when not given)."""
    reconnect = last_event_id is not None
    if reconnect:
        replayed, place, resync = await _replay(log, last_event_id, stream.keep)
        for chunk in replayed:
            yield chunk
    else:
        place = _Place(start if start is not None else await log.newest() or START, set())
        resync = False
    yield frame("ready", {"resync": resync, "reconnect": reconnect}, place.last_id)
    while True:
        item = await stream.next(keepalive)
        if item is None:
            yield b": keepalive\n\n"
        elif isinstance(item, _End):
            return
        elif isinstance(item, _Gap):
            replayed, gap_place, resync = await _replay(log, place.last_id, stream.keep)
            for chunk in replayed:
                yield chunk
            place = gap_place
            yield frame("reconnected", {"resync": resync}, place.last_id)
        elif item.event_id is not None:
            if item.event_id in place.replayed:
                continue
            place.last_id = item.event_id
            yield frame("change", {"c": item.channel, "p": item.payload}, item.event_id)
        else:
            yield frame("change", {"c": item.channel, "p": item.payload})
