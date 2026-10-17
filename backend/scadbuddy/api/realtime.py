"""``WS /api/v1/ws``: the UI's realtime channel (#266).

The browser subscribes to *topics* and is told when something under one changed;
it then re-reads through the REST route it already uses. Frames carry the event's
kind and ids, never content (``core/events.py``), so a lost or repeated frame costs
one extra fetch and never shows stale data.

This socket is the backend's own consumer of the event bus. The agent service keeps
its own listener and routes for sessions and the browser bridge; both hear the same
``pg_notify`` channel independently (spec §7).

Protocol (JSON text frames)
---------------------------
Client → server::

    {"type": "subscribe", "topics": ["job:<id>", "models"]}
    {"type": "unsubscribe", "topics": ["models"]}
    {"type": "check", "id", "since": [<event id>...], "topics": [...]}

Server → client::

    {"type": "subscribed", "topics": [...], "last"}  # the bus is listening for these now
    {"type": "changed", "id", "topics": [...]}   # the answer to a ``check``
    {"type": "event", "id", "kind", "topics", "data"}
    {"type": "resync"}                           # events were lost: re-read everything
    {"type": "ping"}                             # every PING_SECONDS
    {"type": "error", "message"}                 # a bad frame; the socket stays open

``subscribed`` is sent only after the bus subscription exists, so a client that
fetches when it arrives cannot miss a change made between its fetch and its first
event. That stands in for a server-side snapshot: the client's fetch *is* the
snapshot, through the route that already shapes it.

Resume after a reconnect is the same move: the client resubscribes and re-reads on
``subscribed``. Replay from an event log (``Last-Event-ID``) is for MCP (#264).

The cursor (#1039)
------------------
On a cold page load the first read starts before the socket is open, so the re-read
on ``subscribed`` usually reads the same thing again. A read can say what it saw
instead: every API response carries `CURSOR_HEADER`, the id of the last event this
process had heard when the request arrived. Postgres delivers a NOTIFY only after its
log row commits, and a change is published after it is made, so that event and
everything before it in the log is reflected in the read.

``subscribed`` carries ``last``, the last event this process had heard once the
topics were followed; anything after it reaches the client as an ``event`` frame. A
client whose read saw ``last`` itself has missed nothing. After the bus's listener
reconnects, ``last`` is left out until it hears a fresh event: a NOTIFY sent during the
gap was never heard, so the client must ``check``. Otherwise it sends
``check`` with the ids its read saw, and the server replays the log after the
earliest of them and answers ``changed`` with the topics an event since touched. An
unknown or pruned id, a gap, more events than `CHECK_MAX_EVENTS`, a topic this socket
does not follow, a log that cannot be read, or a bus with no log answers every topic:
the client re-reads, as it did before.

Replaying by ``seq`` is sound because ``seq`` order is commit order. Every log write is
`~scadbuddy.core.pg_events.write_event`, which takes ``EVENT_LOG_LOCK``
(``pg_advisory_xact_lock``) before its INSERT. A transaction-level lock is held until
the caller's transaction commits, savepoint or not, so a publish that starts while
another's transaction is open waits for it. No event can commit after one with a
larger ``seq`` (``tests/test_pg_event_bus.py::test_log_order_is_commit_order``).

An event the publisher drops before it is logged (``scadbuddy_events_dropped_total``:
the outbox full, or a failed write) never reaches the log or any socket. Without a
cursor, the cold load's re-read happened to cover such a drop. With one, the drop goes
unseen there too, as it always did for a page already open.

A socket answers its ``check`` frames in order, on a worker beside the loop that reads
its frames, so a slow log never holds up its subscribes. Past `CHECK_QUEUE` waiting, a
``check`` is answered "every topic changed".

Security
--------
- ``Origin`` must be the configured public URL's origin, one of
  ``SCADBUDDY_ALLOWED_ORIGINS`` (the other hostnames the same deployment answers on,
  e.g. the LAN host beside an SSO proxy), or a loopback origin. An allowlist, not
  "Origin equals Host": under DNS rebinding the attacker's page sends its own name
  in both (the same reasoning as ``agent/src/http/origins.ts``). A frame with no
  ``Origin`` is not from a browser page and is as trusted as a REST call; the
  backend has no auth today (spec §4.3).
- Topics are authorised as their REST routes are: everything the UI can GET, it may
  follow.
- Per-connection caps on topics and on inbound frame rate, and a cap on open sockets
  (``SCADBUDDY_REALTIME_SOCKETS``): each one is a standing bus subscription.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, assert_never
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope
from starlette.types import Send as ASGISend

from scadbuddy.api.deps import HEX_ID_PATTERN, AppState, StateDep
from scadbuddy.core.events import (
    ActivityEvent,
    AgentOperationEvent,
    AnalyzerDecisionEvent,
    BusResync,
    Event,
    FlowRunEvent,
    FontInstalled,
    JobEvent,
    JobProgress,
    LibraryChanged,
    LibraryRemoved,
    ModelEvent,
    OperationEvent,
    OutputEvent,
    PresetsChanged,
    PrintEvent,
    PrintRunEvent,
    SessionBusEvent,
    SettingsChanged,
    SourceChanged,
    Subscription,
    UpstreamAvailable,
    VersionCommitted,
)
from scadbuddy.library.outputs import OUTPUT_ID_PATTERN
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])

#: How often the server pings; a client that hears nothing for twice this reconnects.
PING_SECONDS = 25.0
#: Topics one connection may follow at once.
MAX_TOPICS = 64
#: Inbound frames: a burst of RATE_BURST, refilled at RATE_PER_SECOND.
RATE_BURST = 40
RATE_PER_SECOND = 20.0
#: The largest inbound frame this route acts on; subscribe frames are small. The
#: server refuses anything over ``--ws-max-size`` (the Dockerfile's CMD, 8 MiB, shared
#: with the LSP bridge's whole-source messages) before it reaches here.
MAX_FRAME_CHARS = 16_384

#: The response header that carries the read's cursor (#1039; see the module docstring).
CURSOR_HEADER = "X-ScadBuddy-Last-Event"
#: The most events a ``check`` replays before it answers "every topic changed".
CHECK_MAX_EVENTS = 1000
#: The most cursors one ``check`` may carry: one per request of a read.
CHECK_MAX_SINCE = 8
#: The most ``check`` frames one socket keeps waiting for its worker.
CHECK_QUEUE = 32

#: As ``urlsplit(...).hostname`` gives them: an IPv6 literal without its brackets.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

COLLECTION_TOPICS = frozenset(
    {
        "models",
        "outputs",
        "libraries",
        "fonts",
        "settings",
        "analyzers",
        "workflow-runs",
        "activity",
    }
)
#: A flow run's id (#1057): a UUID, as ``api/flows.py`` makes them.
FLOW_RUN_ID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _strip_anchors(pattern: str) -> str:
    return pattern.removeprefix("^").removesuffix("$")


_TOPIC = re.compile(
    "^(?:"
    + "|".join(
        [
            f"job:{_strip_anchors(HEX_ID_PATTERN)}",
            f"model:{_strip_anchors(MODEL_ID_PATTERN)}",
            f"print:{_strip_anchors(OUTPUT_ID_PATTERN)}",
            # A library file's print run (#742): its ``output_id`` is ``library:<file id>``.
            r"print:library:[1-9][0-9]{0,17}",
            f"workflow-run:{FLOW_RUN_ID_PATTERN}",
            # An activity row (spec 2026-10-09): ``activity:<source>:<source id>``.
            r"activity:(?:render|operation|print_run|agent_operation):[A-Za-z0-9_-]{1,64}",
            *sorted(COLLECTION_TOPICS),
        ]
    )
    + ")$"
)


def valid_topic(topic: object) -> bool:
    if not isinstance(topic, str) or not _TOPIC.match(topic):
        return False
    return not topic.startswith("model:") or len(topic) - 6 <= MAX_MODEL_ID_LENGTH


def topics_of(event: Event) -> list[str]:
    """Every topic ``event`` is news for."""
    match event:
        case JobEvent() | JobProgress():
            return [f"job:{event.job_id}"]
        case ModelEvent():
            return ["models", f"model:{event.slug}"]
        case SourceChanged() | VersionCommitted() | UpstreamAvailable() | PresetsChanged():
            return [f"model:{event.slug}"]
        case OutputEvent():
            return ["outputs", f"model:{event.slug}"]
        case PrintEvent() | PrintRunEvent():
            return [f"print:{event.output_id}"]
        case FlowRunEvent():
            return ["workflow-runs", f"workflow-run:{event.run_id}"]
        case LibraryChanged():
            return ["libraries", f"model:{event.slug}"]
        case LibraryRemoved():
            return ["libraries"]
        case FontInstalled():
            return ["fonts"]
        case SettingsChanged():
            return ["settings"]
        case AnalyzerDecisionEvent():
            return ["analyzers"]
        case ActivityEvent():
            return ["activity", f"activity:{event.activity_id}"]
        case AgentOperationEvent():
            return []
        case OperationEvent():
            # Followed by `GET /operations/{id}` (#1053); the event log keeps it.
            return []
        case SessionBusEvent():
            # The agent's own (#300): the UI follows sessions over the agent's
            # chat socket, which knows who may see which.
            return []
        case BusResync():
            # Not news for a topic: `pump` turns it into a ``resync`` frame.
            return []
        case _:
            assert_never(event)


def event_frame(event: Event, topics: list[str]) -> dict[str, Any]:
    data = event.model_dump(mode="json", exclude={"id", "at", "kind"})
    return {"type": "event", "id": event.id, "kind": event.kind, "topics": topics, "data": data}


def _origin(value: str) -> str | None:
    """``scheme://host[:port]``, lower-cased, default port dropped; None if not http(s)."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def origin_allowed(
    origin: str | None, public_url: str | None, allowed_origins: Iterable[str] = ()
) -> bool:
    if origin is None:
        return True
    normalised = _origin(origin)
    if normalised is None:
        return False
    if urlsplit(normalised).hostname in LOOPBACK_HOSTS:
        return True
    candidates = [public_url, *allowed_origins]
    return any(
        candidate is not None and normalised == _origin(candidate) for candidate in candidates
    )


class RateLimit:
    """A token bucket: ``burst`` frames at once, refilled at ``per_second``."""

    def __init__(self, burst: int, per_second: float, clock: Callable[[], float]) -> None:
        self.burst = burst
        self.per_second = per_second
        self._clock = clock
        self._tokens = float(burst)
        self._at = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.per_second)
        self._at = now

    def take(self) -> bool:
        self._refill()
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    def retry_after(self) -> float:
        """Seconds until one more frame is allowed; 0 when one is now (a ``Retry-After``)."""
        self._refill()
        return max(0.0, (1 - self._tokens) / self.per_second)


Send = Callable[[dict[str, Any]], Awaitable[None]]


async def pump(subscription: Subscription, topics: set[str], send: Send) -> None:
    """Forward the bus to the socket until the subscription ends.

    Only events under a followed topic are sent. When the subscription has dropped
    events (it fell ``maxsize`` behind), or the Postgres bus reports a gap in its
    LISTEN connection (``BusResync``, #374), the client is told to ``resync``
    instead of being left with a gap."""
    dropped = 0
    async for event in subscription:
        if subscription.dropped != dropped:
            dropped = subscription.dropped
            await send({"type": "resync"})
        if isinstance(event, BusResync):
            await send({"type": "resync"})
            continue
        matched = [topic for topic in topics_of(event) if topic in topics]
        if matched:
            await send(event_frame(event, matched))


async def _ping(send: Send) -> None:
    while True:
        await asyncio.sleep(PING_SECONDS)
        await send({"type": "ping"})


def last_heard(events: object) -> str | None:
    """The id of the last event this process heard, or None: none yet, or a bus with no
    log to check a cursor against (the in-process one). A read's cursor."""
    last = getattr(events, "last_event_id", None)
    return last if isinstance(last, str) and hasattr(events, "replay") else None


def live_since(events: object) -> str | None:
    """`last_heard`, but only when every event after it reaches this process's
    subscriptions: None after the listener reconnects until it hears a fresh event,
    since a NOTIFY sent during the gap was never heard here. ``subscribed``'s ``last``."""
    live = getattr(events, "live_event_id", None)
    return live if isinstance(live, str) and last_heard(events) is not None else None


async def changed_topics(events: object, since: list[str], topics: list[str]) -> list[str]:
    """Which of ``topics`` an event logged after the earliest of ``since`` touched; all
    of them when that cannot be told (see "The cursor" in the module docstring)."""
    replay = getattr(events, "replay", None)
    positions = getattr(events, "positions", None)
    if replay is None or positions is None or not since:
        return topics
    places = await positions(list(dict.fromkeys(since)))
    if any(event_id not in places for event_id in since):
        return topics
    after = min(places.values())
    wanted = set(topics)
    changed: set[str] = set()
    seen = 0
    while True:
        page = await replay(after, limit=min(500, CHECK_MAX_EVENTS))
        if page.gap:
            return topics
        for logged in page.events:
            changed.update(topic for topic in topics_of(logged.event) if topic in wanted)
            after = logged.seq
        seen += len(page.events)
        if not page.events or changed == wanted:
            break
        if seen >= CHECK_MAX_EVENTS:
            return topics
    return [topic for topic in topics if topic in changed]


async def _check(frame: dict[str, Any], topics: set[str], events: object, send: Send) -> None:
    check_id = frame.get("id")
    since = frame.get("since")
    requested = frame.get("topics")
    if (
        not isinstance(check_id, int)
        or not isinstance(since, list)
        or not 1 <= len(since) <= CHECK_MAX_SINCE
        or not all(isinstance(e, str) and 0 < len(e) <= 64 for e in since)
        or not isinstance(requested, list)
        or not all(valid_topic(topic) for topic in requested)
    ):
        await send({"type": "error", "message": "a check needs an id, since and topics"})
        return
    requested = list(dict.fromkeys(requested))
    # Only a followed topic's later events reach the client, so only one may be cleared.
    followed = [topic for topic in requested if topic in topics]
    try:
        changed = set(await changed_topics(events, since, followed)) if followed else set()
    except Exception:
        # The log could not be read: the client reads again, as without a cursor.
        logger.warning("could not check a realtime cursor", exc_info=True)
        changed = set(followed)
    changed.update(topic for topic in requested if topic not in topics)
    await send(
        {"type": "changed", "id": check_id, "topics": [t for t in requested if t in changed]}
    )


class _Checks:
    """A socket's ``check`` frames, answered in order by one worker beside its frame
    loop: a slow log never holds up a subscribe, and several checks sent together (a
    cold load's views) each get a real answer. Past `CHECK_QUEUE` waiting, a check is
    answered "every topic changed" at once."""

    def __init__(self, topics: set[str], events: object, send: Send) -> None:
        self.topics = topics
        self.events = events
        self.send = send
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(CHECK_QUEUE)

    async def submit(self, frame: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            requested = frame.get("topics")
            if isinstance(frame.get("id"), int) and isinstance(requested, list):
                named = [t for t in dict.fromkeys(requested) if valid_topic(t)]
                await self.send({"type": "changed", "id": frame["id"], "topics": named})
            else:
                await _check(frame, self.topics, self.events, self.send)  # refused, no read

    async def work(self) -> None:
        while True:
            await _check(await self.queue.get(), self.topics, self.events, self.send)


async def _read(
    websocket: WebSocket,
    topics: set[str],
    send: Send,
    events: object = None,
    checks: _Checks | None = None,
) -> None:
    limit = RateLimit(RATE_BURST, RATE_PER_SECOND, time.monotonic)
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
        if not limit.take():
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="too many frames")
            return
        raw = message.get("text")
        if raw is None:
            await send({"type": "error", "message": "expected a text frame"})
            continue
        if len(raw) > MAX_FRAME_CHARS:
            await send({"type": "error", "message": "frame too large"})
            continue
        try:
            frame = json.loads(raw)
        except ValueError:
            await send({"type": "error", "message": "not JSON"})
            continue
        kind = frame.get("type") if isinstance(frame, dict) else None
        if kind == "pong":
            continue
        if kind == "check" and isinstance(frame, dict):
            if checks is None:
                await send({"type": "error", "message": "this socket answers no checks"})
            else:
                await checks.submit(frame)
            continue
        requested = frame.get("topics") if isinstance(frame, dict) else None
        if kind not in ("subscribe", "unsubscribe") or not isinstance(requested, list):
            await send({"type": "error", "message": "expected subscribe or unsubscribe"})
            continue
        bad = [topic for topic in requested if not valid_topic(topic)]
        if bad:
            await send({"type": "error", "message": f"unknown topics: {bad[:5]}"})
            continue
        if kind == "unsubscribe":
            topics.difference_update(requested)
            continue
        # What fits is followed and the rest refused, so a client that resubscribes
        # everything in one frame after a reconnect never loses all of it to the cap.
        accepted: list[str] = []
        refused: list[str] = []
        for topic in dict.fromkeys(requested):
            if topic in topics or len(topics) < MAX_TOPICS:
                topics.add(topic)
                accepted.append(topic)
            else:
                refused.append(topic)
        if accepted:
            ack: dict[str, Any] = {"type": "subscribed", "topics": accepted}
            # Read with the topics already followed: any event after it is sent (#1039).
            last = live_since(events)
            if last is not None:
                ack["last"] = last
            await send(ack)
        if refused:
            await send(
                {
                    "type": "error",
                    "message": f"at most {MAX_TOPICS} topics; not following {refused[:5]}",
                }
            )


async def refuse_foreign_origin(websocket: WebSocket, state: AppState, what: str) -> bool:
    """Close ``websocket`` with 1008 before accepting it, and say True, when its
    ``Origin`` is not `origin_allowed`. Browsers do not apply the same-origin policy to
    sockets, so this is all that keeps another site's page off every socket (the
    realtime one, #266; the language servers', #1317). Call it before taking a permit."""
    origin = websocket.headers.get("origin")
    public_url = await asyncio.to_thread(lambda: state.settings_store.load().public_url)
    if origin_allowed(origin, public_url, state.settings.allowed_origin_list):
        return False
    logger.warning(
        "refused a %s socket from origin %r: not the public URL's origin (%r) "
        "and not in SCADBUDDY_ALLOWED_ORIGINS (%r)",
        what,
        origin,
        public_url,
        state.settings.allowed_origins,
    )
    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    return True


@router.websocket("/ws")
async def realtime(websocket: WebSocket, state: StateDep) -> None:
    if await refuse_foreign_origin(websocket, state, "realtime"):
        return
    # No await between the check and the acquire, so nothing can take the permit in
    # between (the same pattern as `api/lsp.py`). Refused sockets reconnect with
    # back-off, and the UI polls meanwhile.
    if state.realtime_sockets.locked():
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
        return
    async with state.realtime_sockets:
        await websocket.accept()
        await _serve(websocket, state)


async def _serve(websocket: WebSocket, state: AppState) -> None:
    lock = asyncio.Lock()

    async def send(frame: dict[str, Any]) -> None:
        async with lock:
            await websocket.send_json(frame)

    topics: set[str] = set()
    # Subscribed before the first frame is read, so every `subscribed` ack is sent
    # with the bus already listening.
    async with state.events.subscribe() as subscription, anyio.create_task_group() as group:

        async def run(part: Awaitable[None]) -> None:
            # The client went, or the socket closed under a send.
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await part
            # Whichever part ends first ends the session.
            group.cancel_scope.cancel()

        checks = _Checks(topics, state.events, send)
        group.start_soon(run, _read(websocket, topics, send, state.events, checks))
        group.start_soon(run, checks.work())
        group.start_soon(run, pump(subscription, topics, send))
        group.start_soon(run, _ping(send))


class EventCursor:
    """Pure ASGI: stamps every API response with `CURSOR_HEADER`, the last event this
    process had heard when the request arrived (#1039; "The cursor" above)."""

    def __init__(self, app: ASGIApp, events: Callable[[], object]) -> None:
        self.app = app
        self.events = events

    async def __call__(self, scope: Scope, receive: Receive, send: ASGISend) -> None:
        last = last_heard(self.events()) if scope["type"] == "http" else None
        if last is None or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def stamped(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[CURSOR_HEADER] = last
            await send(message)

        await self.app(scope, receive, stamped)
