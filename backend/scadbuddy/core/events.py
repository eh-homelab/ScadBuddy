"""The backend's event bus (spec §7; #264, #266).

Every state change publishes a small typed event: its ``kind`` plus the ids of what
changed, and **never content**. A consumer that wants the new state re-reads it
through the API it already has (an MCP resource, a REST route), so an event can be
dropped, repeated or reordered without anyone acting on a stale copy of the data.

Publishing is fire-and-forget and must never fail the request that caused it: a
route calls :func:`emit`, which logs and carries on whatever the bus does.

Transport
---------
:class:`InProcessEventBus` fans events out to subscribers in this process. Each
subscriber has a bounded queue; a subscriber that falls behind loses its *oldest*
events and the loss is counted (:attr:`Subscription.dropped`), so a slow WebSocket
never holds memory or back-pressures a render worker. Events are ids-only, so a
consumer that sees ``dropped`` grow resyncs by re-reading, which is what #266's
``resync`` answer is for.

On Postgres (#241, #264)
------------------------
With ``SCADBUDDY_DATABASE_URL`` set, ``build_state`` uses
:class:`~scadbuddy.core.pg_events.PgNotifyEventBus` instead, behind the same
:class:`EventBus` protocol; without it, this in-process bus, so the UI works with no
database (#266's "without the database"). The Postgres bus:

- appends each event to the ``events`` log table and sends
  ``pg_notify(PG_CHANNEL, encode_event(event))`` in **one transaction**, so no event
  is heard before its log row is readable, and a job change's event commits (or
  rolls back) with the change itself;
- does **not** deliver locally -- the process hears its own NOTIFY like every other
  replica, so each subscriber sees each event exactly once whichever replica
  published it;
- decodes each payload heard on its process's one LISTEN connection
  (`scadbuddy.core.pg_listener`) with :func:`decode_event`
  into an embedded :class:`InProcessEventBus`, whose
  :meth:`~InProcessEventBus.subscribe` it exposes unchanged;
- delivers :class:`BusResync` (``bus.resync``) to every subscription when that
  connection comes back after a drop, since what was NOTIFYed meanwhile is lost;
- replays the log after a ``seq`` (``Last-Event-ID``, #264's MCP resumability and
  #266's WebSocket), pruned by ``SCADBUDDY_EVENT_LOG_RETENTION_*``.

Payloads are ids only, far under NOTIFY's 8000-byte limit; one over half of it is
refused and logged. The details are in `scadbuddy.core.pg_events`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections import deque
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from types import TracebackType
from typing import Annotated, Literal, Protocol, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from scadbuddy.core.metrics import RenderStage

logger = logging.getLogger(__name__)

#: The channel a Postgres backend NOTIFYs and LISTENs on (spec §7).
PG_CHANNEL = "scadbuddy_events"

#: Events a subscriber may fall behind by before its oldest are dropped.
DEFAULT_QUEUE_SIZE = 256


def _event_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class BaseEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=_event_id, description="Unique per event")
    at: datetime = Field(default_factory=_now, description="When it was published")


JobKind = Literal["job.pending", "job.running", "job.done", "job.failed", "job.superseded"]


class JobEvent(BaseEvent):
    """A render job changed state; the kind is ``job.<its new state>``.

    ``job.pending`` is published for a new job and again for one requeued after its
    worker was lost; a submit coalesced onto a waiting job publishes nothing, as that
    job did not change. ``job.superseded`` ends a job a newer render replaced before
    it started (its stored state is ``failed``), so nobody following it waits on.
    Expired jobs are ``job.failed``."""

    kind: JobKind
    job_id: str
    slug: str


class JobProgress(BaseEvent):
    """A running render started one of its steps (#267): the same steps
    ``scadbuddy_render_stage_seconds`` times (``core/metrics.py`` ``RenderStage``)."""

    kind: Literal["job.progress"] = "job.progress"
    job_id: str
    slug: str
    stage: RenderStage


class ModelEvent(BaseEvent):
    """A template was added, changed (metadata, thumbnail, README, pins, upstream) or
    removed. ``source.changed`` is published beside ``model.updated`` for the source."""

    kind: Literal["model.created", "model.updated", "model.deleted"]
    slug: str


class SourceChanged(BaseEvent):
    kind: Literal["source.changed"] = "source.changed"
    slug: str


class VersionCommitted(BaseEvent):
    """A revision was recorded in the models repository."""

    kind: Literal["version.committed"] = "version.committed"
    slug: str
    commit: str


class UpstreamAvailable(BaseEvent):
    """``slug`` is a duplicate whose upstream just got a revision it can merge."""

    kind: Literal["upstream.available"] = "upstream.available"
    slug: str
    upstream: str
    commit: str


class OutputEvent(BaseEvent):
    kind: Literal["output.created", "output.deleted"]
    output_id: str
    slug: str


class PrintEvent(BaseEvent):
    """An output's print moved on (``print.progress``), or reached a state nothing
    changes without another print (``print.settled``)."""

    kind: Literal["print.progress", "print.settled"]
    output_id: str
    slug: str


class LibraryChanged(BaseEvent):
    """A library was pinned to, re-pinned on, or removed from a model."""

    kind: Literal["library.changed"] = "library.changed"
    slug: str
    name: str


class LibraryRemoved(BaseEvent):
    """Checkouts of a library were deleted from the volume (#253). No model pinned
    them -- the removal is refused while one does -- so no model changed."""

    kind: Literal["library.removed"] = "library.removed"
    name: str
    commits: list[str]


class FontInstalled(BaseEvent):
    kind: Literal["font.installed"] = "font.installed"
    family: str


SettingsSection = Literal[
    "connection",
    "print_options",
    "model_pipeline",
    "model_choices",
    "printer_bed_type",
    "last_project",
]


class SettingsChanged(BaseEvent):
    kind: Literal["settings.changed"] = "settings.changed"
    section: SettingsSection


class AnalyzerDecisionEvent(BaseEvent):
    """A print-analyzer decision (accept, ignore, suppress) was recorded or removed
    (#284). The ids say which rule at which scope; re-read the decisions for the rest."""

    kind: Literal["analyzer.decision"] = "analyzer.decision"
    decision_id: str
    diagnostic_id: str
    scope: str
    scope_key: str
    action: Literal["recorded", "removed"]


#: The resync marker's kind. Every subscription receives it, whatever its filter.
RESYNC_KIND = "bus.resync"


class BusResync(BaseEvent):
    """Events may have been missed: re-read whatever you follow.

    Never published by a state change and never sent over NOTIFY: the Postgres bus
    delivers it locally when its LISTEN connection comes back after a drop, since
    whatever was NOTIFYed while it was down reached nobody in this process. It goes
    to every subscription, whatever kinds it filters on. ``last_event_id`` is the
    last event this process received before the gap, or ``None``: a consumer may
    replay the event log after it rather than re-read everything."""

    kind: Literal["bus.resync"] = "bus.resync"
    last_event_id: str | None = None


Event = Annotated[
    JobEvent
    | JobProgress
    | ModelEvent
    | SourceChanged
    | VersionCommitted
    | UpstreamAvailable
    | OutputEvent
    | PrintEvent
    | LibraryChanged
    | LibraryRemoved
    | FontInstalled
    | SettingsChanged
    | AnalyzerDecisionEvent
    | BusResync,
    Field(discriminator="kind"),
]

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)

EventKind = str

#: Every kind, for validating a subscriber's filter.
EVENT_KINDS: frozenset[str] = frozenset(
    kind
    for model in get_args(get_args(Event)[0])
    for kind in get_args(model.model_fields["kind"].annotation)
)


def encode_event(event: Event) -> str:
    """The wire form: what a NOTIFY payload or a WebSocket frame carries."""
    return event.model_dump_json()


def decode_event(payload: str | bytes) -> Event:
    return _EVENT_ADAPTER.validate_json(payload)


class Subscription:
    """One consumer's view of the bus: a bounded queue, oldest dropped first.

    Bound to the event loop it was created on; the bus delivers to it from any
    thread. Iterate it (``async for event in subscription``) or call :meth:`get`;
    both end once it is closed and drained.
    """

    def __init__(
        self,
        *,
        maxsize: int,
        kinds: Collection[str] | None,
        loop: asyncio.AbstractEventLoop,
        on_close: Callable[[Subscription], None],
    ) -> None:
        if maxsize < 1:
            raise ValueError("a subscription needs room for at least one event")
        unknown = set(kinds or ()) - EVENT_KINDS
        if unknown:
            raise ValueError(f"unknown event kinds: {sorted(unknown)}")
        self.maxsize = maxsize
        self.kinds = frozenset(kinds) if kinds is not None else None
        #: Events this subscriber lost by falling more than ``maxsize`` behind.
        self.dropped = 0
        self._loop = loop
        self._queue: deque[Event] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        self._on_close = on_close

    @property
    def closed(self) -> bool:
        return self._closed

    def wants(self, event: Event) -> bool:
        # A resync concerns every kind: a filtered subscriber missed its own too.
        return self.kinds is None or event.kind in self.kinds or event.kind == RESYNC_KIND

    def offer(self, event: Event) -> None:
        """Hand ``event`` over from any thread. Never blocks, never raises."""
        if self._closed or not self.wants(event):
            return
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._put(event)
            return
        try:
            self._loop.call_soon_threadsafe(self._put, event)
        except RuntimeError:
            # The subscriber's loop has closed under it: nobody is left to read.
            self._closed = True

    def _put(self, event: Event) -> None:
        if self._closed:
            return
        if len(self._queue) >= self.maxsize:
            self._queue.popleft()
            self.dropped += 1
        self._queue.append(event)
        self._ready.set()

    def get_nowait(self) -> Event | None:
        if not self._queue:
            return None
        return self._queue.popleft()

    async def get(self) -> Event:
        """The next event; ``StopAsyncIteration`` once closed and drained."""
        while not self._queue:
            if self._closed:
                raise StopAsyncIteration
            self._ready.clear()
            await self._ready.wait()
        return self._queue.popleft()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        return await self.get()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready.set()
        self._on_close(self)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class EventBus(Protocol):
    """What publishers and subscribers see, whatever carries the events."""

    def publish(self, event: Event) -> None:
        """Deliver ``event`` to every subscriber. Non-blocking and thread-safe."""
        ...

    def subscribe(
        self, *, kinds: Collection[str] | None = None, maxsize: int | None = None
    ) -> Subscription:
        """A new subscription, on the running event loop. ``kinds`` filters; None is all."""
        ...

    async def aclose(self) -> None:
        """Close every subscription, ending each consumer's iteration."""
        ...


Listener = Callable[[Event], None]


class InProcessEventBus:
    """Fan-out within this process. Safe to publish from worker threads, which is
    where the git-backed catalogue writes and the sync routes run."""

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.queue_size = queue_size
        self._lock = threading.Lock()
        self._subscriptions: set[Subscription] = set()
        self._listeners: list[Listener] = []

    def publish(self, event: Event) -> None:
        with self._lock:
            subscriptions = tuple(self._subscriptions)
            listeners = tuple(self._listeners)
        for subscription in subscriptions:
            subscription.offer(event)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("an event listener failed", extra={"kind": event.kind})

    def subscribe(
        self, *, kinds: Collection[str] | None = None, maxsize: int | None = None
    ) -> Subscription:
        subscription = Subscription(
            maxsize=maxsize or self.queue_size,
            kinds=kinds,
            loop=asyncio.get_running_loop(),
            on_close=self._forget,
        )
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` synchronously, on the publishing thread, for every event.

        For in-process taps that must not miss anything -- tests, and the forwarder
        a Postgres backend would add. It must be quick and must not block. Returns
        the function that removes it.
        """
        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def _forget(self, subscription: Subscription) -> None:
        with self._lock:
            self._subscriptions.discard(subscription)

    async def aclose(self) -> None:
        with self._lock:
            subscriptions = tuple(self._subscriptions)
        for subscription in subscriptions:
            subscription.close()


def emit(bus: EventBus | None, event: Event) -> None:
    """Publish ``event``, and never let publishing break the caller.

    The change the event describes has already happened; losing the notification
    is the smaller harm, and consumers can always re-read.
    """
    if bus is None:
        return
    try:
        bus.publish(event)
    except Exception:
        logger.exception("could not publish an event", extra={"kind": event.kind})
