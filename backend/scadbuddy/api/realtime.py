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

Server → client::

    {"type": "subscribed", "topics": [...]}      # the bus is listening for these now
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

from scadbuddy.api.deps import JOB_ID_PATTERN, AppState, StateDep
from scadbuddy.core.events import (
    AnalyzerDecisionEvent,
    BusResync,
    Event,
    FontInstalled,
    JobEvent,
    JobProgress,
    LibraryChanged,
    LibraryRemoved,
    ModelEvent,
    OutputEvent,
    PrintEvent,
    PrintRunEvent,
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

#: As ``urlsplit(...).hostname`` gives them: an IPv6 literal without its brackets.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

COLLECTION_TOPICS = frozenset({"models", "outputs", "libraries", "fonts", "settings", "analyzers"})


def _strip_anchors(pattern: str) -> str:
    return pattern.removeprefix("^").removesuffix("$")


_TOPIC = re.compile(
    "^(?:"
    + "|".join(
        [
            f"job:{_strip_anchors(JOB_ID_PATTERN)}",
            f"model:{_strip_anchors(MODEL_ID_PATTERN)}",
            f"print:{_strip_anchors(OUTPUT_ID_PATTERN)}",
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
        case SourceChanged() | VersionCommitted() | UpstreamAvailable():
            return [f"model:{event.slug}"]
        case OutputEvent():
            return ["outputs", f"model:{event.slug}"]
        case PrintEvent() | PrintRunEvent():
            return [f"print:{event.output_id}"]
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

    def take(self) -> bool:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.per_second)
        self._at = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True


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


async def _read(websocket: WebSocket, topics: set[str], send: Send) -> None:
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
            await send({"type": "subscribed", "topics": accepted})
        if refused:
            await send(
                {
                    "type": "error",
                    "message": f"at most {MAX_TOPICS} topics; not following {refused[:5]}",
                }
            )


@router.websocket("/ws")
async def realtime(websocket: WebSocket, state: StateDep) -> None:
    origin = websocket.headers.get("origin")
    public_url = await asyncio.to_thread(lambda: state.settings_store.load().public_url)
    if not origin_allowed(origin, public_url, state.settings.allowed_origin_list):
        logger.warning(
            "refused a realtime socket from origin %r: not the public URL's origin (%r) "
            "and not in SCADBUDDY_ALLOWED_ORIGINS (%r)",
            origin,
            public_url,
            state.settings.allowed_origins,
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
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

        group.start_soon(run, _read(websocket, topics, send))
        group.start_soon(run, pump(subscription, topics, send))
        group.start_soon(run, _ping(send))
