"""``WS /api/v1/ws`` carries ``activity`` and ``activity:<id>`` (spec 2026-10-09)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from scadbuddy.core.events import ActivityEvent, EventBus
from tests.api.test_realtime import WS, bus, subscribe

__all__ = ["bus"]


def test_activity_topics_are_followed(client: TestClient, bus: EventBus) -> None:
    with client.websocket_connect(WS) as ws:
        subscribe(ws, "activity", "activity:render:abc123")
        event = ActivityEvent(activity_id="render:abc123")
        bus.publish(event)
        frame = ws.receive_json()
        assert frame["kind"] == "activity.changed"
        assert frame["topics"] == ["activity", "activity:render:abc123"]
        assert frame["data"]["activity_id"] == "render:abc123"
