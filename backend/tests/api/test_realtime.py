"""``WS /api/v1/ws`` (#266): subscribe, events by topic, resync, Origin, caps."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from scadbuddy.api import realtime, runtime
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.events import (
    AnalyzerDecisionEvent,
    BusResync,
    Event,
    EventBus,
    FlowRunEvent,
    FontInstalled,
    InProcessEventBus,
    JobEvent,
    JobProgress,
    LibraryRemoved,
    ModelEvent,
    OutputEvent,
    PrintEvent,
    PrintRunEvent,
    SessionBusEvent,
    SettingsChanged,
    Subscription,
)
from scadbuddy.core.pg_events import LoggedEvent, Replay
from scadbuddy.core.settings import Settings
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH
from scadbuddy.main import create_app
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS
from tests.support.operations import press

WS = "/api/v1/ws"
JOB_ID = "a" * 32
OUTPUT_ID = "b" * 32


@pytest.fixture
def bus(app: FastAPI) -> EventBus:
    events: EventBus = getattr(app.state, STATE_ATTR).events
    return events


def subscribe(ws: WebSocketTestSession, *topics: str) -> None:
    ws.send_json({"type": "subscribe", "topics": list(topics)})
    ack = ws.receive_json()
    ack.pop("last", None)  # on a bus with a log (#1039)
    assert ack == {"type": "subscribed", "topics": list(topics)}


def test_an_event_reaches_a_socket_following_its_topic(client: TestClient, bus: EventBus) -> None:
    with client.websocket_connect(WS) as ws:
        subscribe(ws, f"job:{JOB_ID}")
        event = JobEvent(kind="job.done", job_id=JOB_ID, slug="demo")
        bus.publish(event)
        assert ws.receive_json() == {
            "type": "event",
            "id": event.id,
            "kind": "job.done",
            "topics": [f"job:{JOB_ID}"],
            "data": {"job_id": JOB_ID, "slug": "demo"},
        }


FLOW_RUN_ID = "0b0e2a0c-1111-4222-8333-444455556666"


def test_a_flow_run_reaches_a_socket_following_the_runs(client: TestClient, bus: EventBus) -> None:
    """#1057: the Workflows page follows `workflow-runs` and `workflow-run:<id>` with the
    others in one frame, as a reconnect resubscribes them."""
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "models", "workflow-runs", f"workflow-run:{FLOW_RUN_ID}")
        bus.publish(FlowRunEvent(run_id=FLOW_RUN_ID, definition_id="d1", status="running"))
        event = ws.receive_json()
        assert (event["kind"], event["topics"]) == (
            "flow_run.changed",
            ["workflow-runs", f"workflow-run:{FLOW_RUN_ID}"],
        )


def test_only_followed_topics_are_sent(client: TestClient, bus: EventBus) -> None:
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "model:demo")
        bus.publish(JobEvent(kind="job.done", job_id=JOB_ID, slug="demo"))
        bus.publish(FontInstalled(family="Lobster Two"))
        bus.publish(ModelEvent(kind="model.updated", slug="other"))
        bus.publish(ModelEvent(kind="model.updated", slug="demo"))
        frame = ws.receive_json()
        assert (frame["kind"], frame["topics"]) == ("model.updated", ["model:demo"])


def test_an_event_names_every_followed_topic_it_is_news_for(
    client: TestClient, bus: EventBus
) -> None:
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "outputs", "model:demo")
        bus.publish(OutputEvent(kind="output.created", output_id=OUTPUT_ID, slug="demo"))
        assert ws.receive_json()["topics"] == ["outputs", "model:demo"]


def test_unsubscribe_stops_delivery(client: TestClient, bus: EventBus) -> None:
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "settings", "fonts")
        ws.send_json({"type": "unsubscribe", "topics": ["settings"]})
        # A frame round trip, so the unsubscribe is applied before publishing.
        subscribe(ws, "fonts")
        bus.publish(SettingsChanged(section="connection"))
        bus.publish(FontInstalled(family="DejaVu Sans"))
        assert ws.receive_json()["kind"] == "font.installed"


def test_a_real_mutation_is_delivered(client: TestClient) -> None:
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "models")
        response = client.post(
            "/api/v1/models", json={"name": "Widget", "source": "x = 1;\n"}, headers=press()
        )
        assert response.status_code == 201, response.text
        frame = ws.receive_json()
        assert (frame["kind"], frame["data"]) == ("model.created", {"slug": "widget"})


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "subscribe", "topics": ["job:not-an-id"]},
        {"type": "subscribe", "topics": ["model:../etc"]},
        {"type": "subscribe", "topics": ["everything"]},
        # The slug's characters are fine; its length is past MAX_MODEL_ID_LENGTH.
        {"type": "subscribe", "topics": [f"model:{'a' * (MAX_MODEL_ID_LENGTH + 1)}"]},
        {"type": "subscribe", "topics": "models"},
        {"type": "shout"},
        [1, 2],
    ],
)
def test_a_bad_frame_is_answered_and_the_socket_stays_open(client: TestClient, frame: Any) -> None:
    with client.websocket_connect(WS) as ws:
        ws.send_json(frame)
        assert ws.receive_json()["type"] == "error"
        subscribe(ws, "models")


def test_a_binary_frame_is_answered_and_the_socket_stays_open(client: TestClient) -> None:
    with client.websocket_connect(WS) as ws:
        ws.send_bytes(b'{"type": "subscribe", "topics": ["models"]}')
        assert ws.receive_json() == {"type": "error", "message": "expected a text frame"}
        subscribe(ws, "models")


def test_not_json_is_answered(client: TestClient) -> None:
    with client.websocket_connect(WS) as ws:
        ws.send_text("{nope")
        assert ws.receive_json() == {"type": "error", "message": "not JSON"}


def test_topics_are_capped(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realtime, "MAX_TOPICS", 2)
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "models", "fonts")
        ws.send_json({"type": "subscribe", "topics": ["settings"]})
        assert ws.receive_json() == {
            "type": "error",
            "message": "at most 2 topics; not following ['settings']",
        }


def test_a_subscribe_past_the_cap_follows_the_topics_that_fit(
    client: TestClient, bus: EventBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that resubscribes everything in one frame after a reconnect keeps what
    fits, rather than losing every topic to the one frame's refusal."""
    monkeypatch.setattr(realtime, "MAX_TOPICS", 2)
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "subscribe", "topics": ["models", "fonts", "settings"]})
        assert ws.receive_json() == {"type": "subscribed", "topics": ["models", "fonts"]}
        assert ws.receive_json()["type"] == "error"
        bus.publish(FontInstalled(family="DejaVu Sans"))
        assert ws.receive_json()["kind"] == "font.installed"


def test_a_topic_already_followed_does_not_count_twice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(realtime, "MAX_TOPICS", 2)
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "models", "fonts")
        subscribe(ws, "models", "fonts")


def test_sockets_past_the_cap_are_refused_until_one_closes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings.model_copy(update={"realtime_sockets": 1}))
    with TestClient(app) as client:
        with client.websocket_connect(WS) as first:
            subscribe(first, "models")
            with (
                pytest.raises(WebSocketDisconnect) as refused,
                client.websocket_connect(WS),
            ):
                pass
            assert refused.value.code == 1013
        with client.websocket_connect(WS) as again:
            subscribe(again, "models")


def test_a_flood_of_frames_closes_the_socket(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(realtime, "RATE_BURST", 3)
    monkeypatch.setattr(realtime, "RATE_PER_SECOND", 0.001)
    with client.websocket_connect(WS) as ws:
        for _ in range(3):
            ws.send_json({"type": "pong"})
        ws.send_json({"type": "pong"})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008


def test_the_server_pings(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realtime, "PING_SECONDS", 0.01)
    with client.websocket_connect(WS) as ws:
        assert ws.receive_json() == {"type": "ping"}


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:5173", "http://127.0.0.1:8080", "http://[::1]:8080"],
)
def test_a_loopback_origin_is_accepted(client: TestClient, origin: str) -> None:
    with client.websocket_connect(WS, headers={"origin": origin}) as ws:
        subscribe(ws, "models")


def test_the_public_url_origin_is_accepted(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"public_url": "https://scad.example.com/"})
    assert response.status_code == 200, response.text
    with client.websocket_connect(WS, headers={"origin": "https://scad.example.com:443"}) as ws:
        subscribe(ws, "models")


@pytest.mark.parametrize(
    "origin",
    ["https://evil.test", "http://scad.example.com", "null", "file://", "not a url"],
)
def test_any_other_origin_is_refused(client: TestClient, origin: str) -> None:
    """DNS rebinding sends the attacker's own name in both Host and Origin, so only
    the allowlist tells it apart (``agent/src/http/origins.ts``)."""
    client.put("/api/v1/settings", json={"public_url": "https://scad.example.com"})
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(WS, headers={"origin": origin}),
    ):
        pass
    assert refused.value.code == 1008


def test_an_origin_in_allowed_origins_is_accepted_beside_the_public_url(
    app: FastAPI, client: TestClient
) -> None:
    """One deployment answering on two hostnames (a LAN host and an SSO proxy, say)
    keeps live updates on both: the other name goes in ``SCADBUDDY_ALLOWED_ORIGINS``."""
    state = getattr(app.state, STATE_ATTR)
    state.settings = state.settings.model_copy(
        update={"allowed_origins": " https://scad.internal.example , https://scad.lan:8443"}
    )
    client.put("/api/v1/settings", json={"public_url": "https://scad.example.com"})
    for origin in (
        "https://scad.example.com",
        "https://scad.internal.example",
        "https://scad.lan:8443",
    ):
        with client.websocket_connect(WS, headers={"origin": origin}) as ws:
            subscribe(ws, "models")
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(WS, headers={"origin": "https://scad.lan"}),
    ):
        pass
    assert refused.value.code == 1008


@pytest.mark.parametrize(
    ("origin", "public_url", "allowed"),
    [
        (None, None, True),
        ("http://LOCALHOST:5173", None, True),
        ("https://scad.example.com", "https://scad.example.com/path", True),
        ("http://scad.example.com:8080", "http://scad.example.com:8080", True),
        ("http://scad.example.com", "http://scad.example.com:80", True),
        ("https://scad.example.com", None, False),
        ("https://scad.example.com:8443", "https://scad.example.com", False),
        ("http://localhost.evil.test", None, False),
        ("https://x:bad", "https://x", False),
    ],
)
def test_origin_allowed(origin: str | None, public_url: str | None, allowed: bool) -> None:
    assert realtime.origin_allowed(origin, public_url) is allowed


@pytest.mark.parametrize(
    ("origin", "public_url", "allowed_origins", "allowed"),
    [
        ("https://scad.internal.example", None, ["https://scad.internal.example"], True),
        (
            "https://scad.internal.example",
            "https://scad.example.com",
            ["https://scad.internal.example/"],
            True,
        ),
        (
            "https://scad.example.com",
            "https://scad.example.com",
            ["https://scad.internal.example"],
            True,
        ),
        (
            "http://scad.lan:8080",
            None,
            ["https://scad.internal.example", "http://SCAD.lan:8080"],
            True,
        ),
        ("https://scad.lan", None, ["http://scad.lan"], False),
        ("https://evil.test", "https://scad.example.com", ["https://scad.internal.example"], False),
        ("https://scad.internal.example", None, ["not a url"], False),
    ],
)
def test_origin_allowed_with_extra_origins(
    origin: str, public_url: str | None, allowed_origins: list[str], allowed: bool
) -> None:
    assert realtime.origin_allowed(origin, public_url, allowed_origins) is allowed


def test_allowed_origins_env_is_split_on_commas() -> None:
    settings = Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        allowed_origins=" https://scad.internal.example ,, https://scad.lan:8443 , ",
    )
    assert settings.allowed_origin_list == [
        "https://scad.internal.example",
        "https://scad.lan:8443",
    ]
    assert (
        Settings(
            database_url=UNUSED_DATABASE_URL, temporal_address=UNUSED_TEMPORAL_ADDRESS
        ).allowed_origin_list
        == []
    )


@pytest.mark.parametrize(
    ("event", "topics"),
    [
        (JobEvent(kind="job.running", job_id=JOB_ID, slug="demo"), [f"job:{JOB_ID}"]),
        (JobProgress(job_id=JOB_ID, slug="demo", stage="solids"), [f"job:{JOB_ID}"]),
        (
            PrintEvent(kind="print.progress", output_id=OUTPUT_ID, slug="demo"),
            [f"print:{OUTPUT_ID}"],
        ),
        (
            PrintRunEvent(output_id="library:89", slug="library-89", run_id="r1"),
            ["print:library:89"],
        ),
        (LibraryRemoved(name="BOSL2", commits=["c" * 40]), ["libraries"]),
        (SettingsChanged(section="connection"), ["settings"]),
        (
            AnalyzerDecisionEvent(
                decision_id="d" * 32,
                diagnostic_id="SB2001",
                scope="template",
                scope_key="demo",
                action="recorded",
            ),
            ["analyzers"],
        ),
        (SessionBusEvent(kind="session.message", session_id="s", seq=3), []),
    ],
)
def test_topics_of(event: Any, topics: list[str]) -> None:
    assert realtime.topics_of(event) == topics


def test_every_topic_an_event_names_is_one_a_client_may_follow() -> None:
    events: list[Event] = [
        JobEvent(kind="job.done", job_id=JOB_ID, slug="demo"),
        ModelEvent(kind="model.created", slug="demo"),
        OutputEvent(kind="output.deleted", output_id=OUTPUT_ID, slug="demo"),
        PrintEvent(kind="print.settled", output_id=OUTPUT_ID, slug="demo"),
        PrintRunEvent(output_id=OUTPUT_ID, slug="demo", run_id="r1"),
        PrintRunEvent(output_id="library:89", slug="library-89", run_id="r2"),
        LibraryRemoved(name="BOSL2", commits=[]),
        FontInstalled(family="DejaVu Sans"),
        SettingsChanged(section="connection"),
        FlowRunEvent(run_id=FLOW_RUN_ID, definition_id="d1", status="running"),
    ]
    for event in events:
        for topic in realtime.topics_of(event):
            assert realtime.valid_topic(topic), topic


def test_a_subscription_that_fell_behind_is_told_to_resync() -> None:
    async def scenario() -> list[dict[str, Any]]:
        bus = InProcessEventBus()
        subscription: Subscription = bus.subscribe(maxsize=1)
        for _ in range(3):
            bus.publish(FontInstalled(family="DejaVu Sans"))
        sent: list[dict[str, Any]] = []

        async def send(frame: dict[str, Any]) -> None:
            sent.append(frame)
            subscription.close()

        await realtime.pump(subscription, {"fonts"}, send)
        return sent

    sent = asyncio.run(scenario())
    assert sent[0] == {"type": "resync"}


def test_a_bus_resync_is_told_to_the_client_whatever_it_follows() -> None:
    async def scenario() -> list[dict[str, Any]]:
        bus = InProcessEventBus()
        subscription: Subscription = bus.subscribe()
        bus.publish(BusResync(last_event_id=None))
        sent: list[dict[str, Any]] = []

        async def send(frame: dict[str, Any]) -> None:
            sent.append(frame)
            subscription.close()

        await realtime.pump(subscription, {"fonts"}, send)
        return sent

    assert asyncio.run(scenario()) == [{"type": "resync"}]
    assert realtime.topics_of(BusResync()) == []


def test_rate_limit_refills() -> None:
    now = [0.0]
    limit = realtime.RateLimit(2, 1.0, lambda: now[0])
    assert limit.take() and limit.take()
    assert not limit.take()
    now[0] = 1.0
    assert limit.take()


class LoggedBus(InProcessEventBus):
    """The Postgres bus's cursor surface (#1039) in memory: ``last_event_id``, and a log
    with ``position`` and ``replay``, of which the first ``pruned`` rows are gone."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list[Event] = []
        self.last_event_id: str | None = None
        self.live_event_id: str | None = None
        self.pruned = 0
        self.settling = 0

    def settle(self) -> Callable[[], None]:
        self.settling += 1
        released = False

        def release() -> None:  # once, however often called, as the Postgres bus's
            nonlocal released
            if not released:
                released = True
                self.settling -= 1

        return release

    def publish(self, event: Event) -> None:
        self.log.append(event)
        self.last_event_id = self.live_event_id = event.id
        super().publish(event)

    async def position(self, event_id: str) -> int | None:
        for seq, event in enumerate(self.log, start=1):
            if event.id == event_id and seq > self.pruned:
                return seq
        return None

    async def positions(self, event_ids: list[str]) -> dict[str, int]:
        places = {event_id: await self.position(event_id) for event_id in event_ids}
        return {event_id: seq for event_id, seq in places.items() if seq is not None}

    async def replay(self, after: int, *, limit: int = 100) -> Replay:
        logged = [
            LoggedEvent(seq=seq, event=event)
            for seq, event in enumerate(self.log, start=1)
            if seq > max(after, self.pruned)
        ]
        return Replay(events=logged[:limit], gap=after < self.pruned)


@pytest.fixture
def logged(app: FastAPI) -> LoggedBus:
    bus = LoggedBus()
    getattr(app.state, STATE_ATTR).events = bus
    return bus


def check(ws: WebSocketTestSession, since: list[str], *topics: str) -> list[str]:
    ws.send_json({"type": "check", "id": 7, "since": since, "topics": list(topics)})
    answer = ws.receive_json()
    while answer["type"] == "event":  # one published meanwhile
        answer = ws.receive_json()
    assert answer["type"] == "changed" and answer["id"] == 7
    changed: list[str] = answer["topics"]
    return changed


def test_a_read_carries_the_last_event_its_process_heard(
    client: TestClient, logged: LoggedBus
) -> None:
    assert realtime.CURSOR_HEADER not in client.get("/api/v1/settings").headers
    event = SettingsChanged(section="connection")
    logged.publish(event)
    assert client.get("/api/v1/settings").headers[realtime.CURSOR_HEADER] == event.id


def test_the_in_process_bus_gives_no_cursor(client: TestClient) -> None:
    """It has no log to check one against, so the client re-reads as before."""
    assert realtime.CURSOR_HEADER not in client.get("/api/v1/settings").headers


def test_subscribed_carries_the_last_event_heard(client: TestClient, logged: LoggedBus) -> None:
    event = SettingsChanged(section="connection")
    logged.publish(event)
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "subscribe", "topics": ["settings"]})
        assert ws.receive_json() == {"type": "subscribed", "topics": ["settings"], "last": event.id}


def test_a_check_names_only_the_topics_an_event_since_touched(
    client: TestClient, logged: LoggedBus
) -> None:
    seen = SettingsChanged(section="connection")
    logged.publish(seen)
    logged.publish(FontInstalled(family="Inter"))
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "settings", "fonts", "models")
        assert check(ws, [seen.id], "settings", "fonts", "models") == ["fonts"]
        # Nothing after the last event: nothing changed.
        assert check(ws, [logged.log[-1].id], "settings", "fonts") == []
        # The earliest of several cursors counts.
        assert check(ws, [logged.log[-1].id, seen.id], "settings", "fonts") == ["fonts"]


@pytest.mark.parametrize("why", ["unknown", "pruned", "unfollowed", "too many"])
def test_a_check_that_cannot_tell_names_every_topic(
    client: TestClient, logged: LoggedBus, monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    seen = SettingsChanged(section="connection")
    logged.publish(seen)
    logged.publish(SettingsChanged(section="connection"))
    since = [seen.id]
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "fonts", "settings")
        topics = ["fonts", "settings"]
        expected = ["fonts", "settings"]
        if why == "unknown":
            since = ["0" * 32]
        elif why == "pruned":
            logged.pruned = 2
        elif why == "unfollowed":
            topics = ["fonts", "models"]
            expected = ["models"]
        else:
            monkeypatch.setattr(realtime, "CHECK_MAX_EVENTS", 1)
        assert check(ws, since, *topics) == expected


def test_after_a_reconnect_subscribed_leaves_out_last_until_a_fresh_event(
    client: TestClient, logged: LoggedBus
) -> None:
    """A NOTIFY sent while the listener was down was never heard here: a read stamped
    before the gap must ``check`` rather than take ``last`` at its word."""
    logged.publish(SettingsChanged(section="connection"))
    logged.live_event_id = None  # what `PgNotifyEventBus._connected` does on a reconnect
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "subscribe", "topics": ["settings"]})
        assert "last" not in ws.receive_json()
    fresh = SettingsChanged(section="connection")
    logged.publish(fresh)
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "subscribe", "topics": ["settings"]})
        assert ws.receive_json()["last"] == fresh.id


def test_a_check_the_log_cannot_answer_names_every_topic(
    client: TestClient, logged: LoggedBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = SettingsChanged(section="connection")
    logged.publish(seen)

    async def down(_after: int, *, limit: int = 100) -> Replay:
        raise OSError("the database went away")

    monkeypatch.setattr(logged, "replay", down)
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "settings")
        assert check(ws, [seen.id], "settings") == ["settings"]
        # The socket stays open.
        assert check(ws, [seen.id], "settings") == ["settings"]


def test_a_slow_check_holds_up_no_subscribe_and_later_checks_wait_their_turn(
    client: TestClient, logged: LoggedBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checks run on a worker beside the frame loop, in order: a subscribe is confirmed
    while one waits on the log, and a second gets a real answer once the first is done."""
    seen = SettingsChanged(section="connection")
    logged.publish(seen)
    release = threading.Event()
    replay = logged.replay

    async def slow(after: int, *, limit: int = 100) -> Replay:
        while not release.is_set():
            await asyncio.sleep(0.01)
        return await replay(after, limit=limit)

    monkeypatch.setattr(logged, "replay", slow)
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "settings")
        ws.send_json({"type": "check", "id": 1, "since": [seen.id], "topics": ["settings"]})
        ws.send_json({"type": "check", "id": 2, "since": [seen.id], "topics": ["settings"]})
        subscribe(ws, "fonts")
        release.set()
        assert ws.receive_json() == {"type": "changed", "id": 1, "topics": []}
        assert ws.receive_json() == {"type": "changed", "id": 2, "topics": []}


def test_checks_past_the_queue_are_answered_every_topic_changed(
    client: TestClient, logged: LoggedBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = SettingsChanged(section="connection")
    logged.publish(seen)
    release = threading.Event()
    replay = logged.replay

    async def slow(after: int, *, limit: int = 100) -> Replay:
        while not release.is_set():
            await asyncio.sleep(0.01)
        return await replay(after, limit=limit)

    monkeypatch.setattr(logged, "replay", slow)
    monkeypatch.setattr(realtime, "CHECK_QUEUE", 1)
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "settings")

        def send_check(check_id: int) -> None:
            ws.send_json(
                {"type": "check", "id": check_id, "since": [seen.id], "topics": ["settings"]}
            )

        send_check(1)
        subscribe(ws, "fonts")  # a round trip: the worker has taken check 1 by now
        for check_id in (2, 3, 4):  # 2 waits; 3 and 4 are over the cap
            send_check(check_id)
        assert [ws.receive_json() for _ in range(2)] == [
            {"type": "changed", "id": 3, "topics": ["settings"]},
            {"type": "changed", "id": 4, "topics": ["settings"]},
        ]
        release.set()
        assert [ws.receive_json() for _ in range(2)] == [
            {"type": "changed", "id": 1, "topics": []},
            {"type": "changed", "id": 2, "topics": []},
        ]


def test_a_malformed_check_is_answered_with_an_error(client: TestClient, logged: LoggedBus) -> None:
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "check", "id": 1, "since": [], "topics": ["fonts"]})
        assert ws.receive_json()["type"] == "error"


def _heard(client: TestClient) -> None:
    """Waits until a read carries a cursor: the bus has heard an event."""
    for _ in range(200):
        if realtime.CURSOR_HEADER in client.get("/api/v1/settings").headers:
            return
        time.sleep(0.05)
    raise AssertionError("the bus heard no event")


@pytest.mark.requires_postgres
def test_a_read_s_cursor_clears_the_re_read_on_the_postgres_bus(client: TestClient) -> None:
    """#1039 end to end: a read's cursor, the ack's ``last`` and ``check`` agree on the
    real log, and a change after the read is still named."""
    client.put("/api/v1/settings", json={"public_url": "https://one.example"})
    _heard(client)
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "subscribe", "topics": ["settings", "fonts"]})
        last = ws.receive_json()["last"]
        assert check(ws, [last], "settings", "fonts") == []
        client.put("/api/v1/settings", json={"public_url": "https://two.example"})
        assert ws.receive_json()["topics"] == ["settings"]  # the event itself
        assert check(ws, [last], "settings", "fonts") == ["settings"]


def test_a_read_carries_no_cursor_while_a_listener_is_settling(
    client: TestClient, logged: LoggedBus
) -> None:
    logged.publish(SettingsChanged(section="connection"))
    release = logged.settle()
    assert realtime.CURSOR_HEADER not in client.get("/api/v1/settings").headers
    release()
    assert realtime.CURSOR_HEADER in client.get("/api/v1/settings").headers


async def test_a_heard_settings_change_holds_cursors_until_it_is_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1039: the runtime settings are applied in a task after the event is heard. Until
    then a read would show the old ones, so it must carry no cursor."""
    bus = LoggedBus()
    reread = threading.Event()
    applied: list[str] = []

    def snapshot() -> SimpleNamespace:
        reread.wait(5)
        return SimpleNamespace(runtime="new")

    state = SimpleNamespace(events=bus, settings_store=SimpleNamespace(snapshot=snapshot))
    monkeypatch.setattr(runtime, "apply_runtime", lambda _state, value: applied.append(value))
    stop = runtime.follow_changes(state)  # type: ignore[arg-type]
    try:
        bus.publish(SettingsChanged(section="connection"))
        assert realtime.last_heard(bus) is None
        reread.set()
        for _ in range(200):
            if applied:
                break
            await asyncio.sleep(0.01)
        assert applied == ["new"]
        assert realtime.last_heard(bus) == bus.log[-1].id
    finally:
        reread.set()
        stop()
