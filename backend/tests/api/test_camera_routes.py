"""The browser's camera routes (camera live view, phase 3): the feed state of a printer and
the reconnect request (`api/camera.py`)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from scadbuddy.agent_changes.hub import Change
from scadbuddy.api.crud_camera import keep_camera
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.camera.models import FeedReport
from scadbuddy.camera.store import CameraStore
from scadbuddy.core.events import PG_CHANNEL, CameraReconnectEvent, Event
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.models import LanLink, PrinterCreate
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

REPORT: dict[str, Any] = json.loads(
    (Path(__file__).parents[1] / "camera" / "fixtures" / "h2c-report.json").read_text()
)


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _make_printer(client: TestClient, *, recorded: bool) -> str:
    printers = _state(client).components.get(PRINTERS)
    printers.store.kek = KEK
    row = printers.store.create(PrinterCreate(serial="01P00A000000001", name="H2C", model="H2C"))
    if recorded:
        printers.store.link_lan(
            row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
        )
        printers.reports.put(row.id, REPORT, datetime.now(UTC))
    return row.id


@pytest.fixture
def printer(client: TestClient) -> str:
    return _make_printer(client, recorded=True)


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[Event]:
    seen: list[Event] = []
    monkeypatch.setattr("scadbuddy.api.camera.emit", lambda _bus, event: seen.append(event))
    return seen


def _store(client: TestClient) -> CameraStore:
    printers = _state(client).components.get(PRINTERS)
    return CameraStore(printers.store.pool, printers.store)


def test_camera_state_lists_both_feeds(client: TestClient, printer: str) -> None:
    store = _store(client)
    store.report_feed(printer, FeedReport(part="relay", state="pulling"))
    store.report_feed(printer, FeedReport(part="recorder", state="retrying", error="no route"))
    body = client.get(f"/api/v1/printers/{printer}/camera").json()
    assert body["recorded"] is True
    feeds = {feed["part"]: feed for feed in body["feeds"]}
    assert set(feeds) == {"relay", "recorder"}
    assert feeds["relay"]["state"] == "pulling"
    assert feeds["recorder"]["state"] == "retrying"
    assert feeds["recorder"]["detail"] == "no route"
    assert feeds["relay"]["detail"] is None
    assert feeds["relay"]["updated_at"]


def test_camera_state_for_a_printer_without_a_stream(client: TestClient) -> None:
    printer = _make_printer(client, recorded=False)
    response = client.get(f"/api/v1/printers/{printer}/camera")
    assert response.status_code == 200
    assert response.json() == {"recorded": False, "live": False, "feeds": []}


def test_camera_state_of_an_unknown_printer_is_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/printers/{uuid.uuid4()}/camera")
    assert response.status_code == 404
    assert response.json()["type"].endswith("/printer-not-found")


def test_live_is_false_without_an_edge_url(client: TestClient, printer: str) -> None:
    body = client.get(f"/api/v1/printers/{printer}/camera").json()
    assert body["recorded"] is True
    assert body["live"] is False


def test_live_is_true_with_an_edge_set_up(client: TestClient, printer: str) -> None:
    state = _state(client)
    state.settings = state.settings.model_copy(
        update={
            "camera_edge_whep_url": "http://edge:8889",
            "camera_edge_hls_url": "http://edge:8888",
            "camera_edge_read_user": "reader",
            "camera_edge_read_pass": SecretStr("read-pass"),
        }
    )
    assert client.get(f"/api/v1/printers/{printer}/camera").json()["live"] is True


def test_reconnect_emits_the_event(client: TestClient, printer: str, events: list[Event]) -> None:
    response = client.post(f"/api/v1/printers/{printer}/camera/reconnect")
    assert response.status_code == 202
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, CameraReconnectEvent)
    assert event.printer_id == printer


def test_reconnect_refuses_a_printer_not_recorded(client: TestClient, events: list[Event]) -> None:
    printer = _make_printer(client, recorded=False)
    response = client.post(f"/api/v1/printers/{printer}/camera/reconnect")
    assert response.status_code == 409
    assert response.json()["type"].endswith("/camera-not-recorded")
    assert events == []


def test_reconnect_of_an_unknown_printer_is_404(client: TestClient, events: list[Event]) -> None:
    response = client.post(f"/api/v1/printers/{uuid.uuid4()}/camera/reconnect")
    assert response.status_code == 404
    assert events == []


def test_camera_reconnect_passes_the_change_stream() -> None:
    event = CameraReconnectEvent(printer_id="p1")
    assert event.kind == "camera.reconnect"
    assert keep_camera(Change(PG_CHANNEL, event.model_dump_json(), event.id))
