"""``WS /api/v1/ws`` (#266): subscribe, events by topic, resync, Origin, caps."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from scadbuddy.api import realtime
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.events import (
    Event,
    EventBus,
    FontInstalled,
    InProcessEventBus,
    JobEvent,
    JobProgress,
    LibraryRemoved,
    ModelEvent,
    OutputEvent,
    PrintEvent,
    SettingsChanged,
    Subscription,
)

WS = "/api/v1/ws"
JOB_ID = "a" * 32
OUTPUT_ID = "b" * 32


@pytest.fixture
def bus(app: FastAPI) -> EventBus:
    events: EventBus = getattr(app.state, STATE_ATTR).events
    return events


def subscribe(ws: WebSocketTestSession, *topics: str) -> None:
    ws.send_json({"type": "subscribe", "topics": list(topics)})
    assert ws.receive_json() == {"type": "subscribed", "topics": list(topics)}


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
        response = client.post("/api/v1/models", json={"name": "Widget", "source": "x = 1;\n"})
        assert response.status_code == 201, response.text
        frame = ws.receive_json()
        assert (frame["kind"], frame["data"]) == ("model.created", {"slug": "widget"})


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "subscribe", "topics": ["job:not-an-id"]},
        {"type": "subscribe", "topics": ["model:../etc"]},
        {"type": "subscribe", "topics": ["everything"]},
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


def test_not_json_is_answered(client: TestClient) -> None:
    with client.websocket_connect(WS) as ws:
        ws.send_text("{nope")
        assert ws.receive_json() == {"type": "error", "message": "not JSON"}


def test_topics_are_capped(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realtime, "MAX_TOPICS", 2)
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "models", "fonts")
        ws.send_json({"type": "subscribe", "topics": ["settings"]})
        assert ws.receive_json() == {"type": "error", "message": "at most 2 topics"}


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
    ("event", "topics"),
    [
        (JobEvent(kind="job.running", job_id=JOB_ID, slug="demo"), [f"job:{JOB_ID}"]),
        (JobProgress(job_id=JOB_ID, slug="demo", stage="solids"), [f"job:{JOB_ID}"]),
        (
            PrintEvent(kind="print.progress", output_id=OUTPUT_ID, slug="demo"),
            [f"print:{OUTPUT_ID}"],
        ),
        (LibraryRemoved(name="BOSL2", commits=["c" * 40]), ["libraries"]),
        (SettingsChanged(section="connection"), ["settings"]),
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
        LibraryRemoved(name="BOSL2", commits=[]),
        FontInstalled(family="DejaVu Sans"),
        SettingsChanged(section="connection"),
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


def test_rate_limit_refills() -> None:
    now = [0.0]
    limit = realtime.RateLimit(2, 1.0, lambda: now[0])
    assert limit.take() and limit.take()
    assert not limit.take()
    now[0] = 1.0
    assert limit.take()
