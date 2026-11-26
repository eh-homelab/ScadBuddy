"""The camera and camera-relay CRUD routes (camera recording, phase 1)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.events import CameraFeedEvent
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.models import LanLink, PrinterCreate
from tests.api.test_crud_profile_fetch import bearer_of
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

REPORT: dict[str, Any] = json.loads(
    (Path(__file__).parents[1] / "camera" / "fixtures" / "h2c-report.json").read_text()
)
CODE = "12345678"
CAMERA = "/api/v1/internal/crud/camera"
RELAY = "/api/v1/internal/crud/camera-relay"


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


@pytest.fixture
def printer(client: TestClient) -> str:
    printers = _state(client).components.get(PRINTERS)
    printers.store.kek = KEK
    row = printers.store.create(PrinterCreate(serial="01P00A000000001", name="H2C", model="H2C"))
    printers.store.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256="ab" * 32
    )
    printers.reports.put(row.id, REPORT, datetime.now(UTC))
    return row.id


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[CameraFeedEvent]:
    seen: list[CameraFeedEvent] = []
    monkeypatch.setattr("scadbuddy.api.crud_camera.emit", lambda _bus, event: seen.append(event))
    return seen


def _segment(start: datetime, key: str) -> dict[str, Any]:
    return {
        "started_at": start.isoformat(),
        "duration_ms": 4000,
        "object_key": key,
        "bytes": 1000,
        "init_bytes": 100,
        "init_sha256": "0" * 64,
    }


def test_camera_cannot_read_codes(client: TestClient, printer: str) -> None:
    response = client.get(f"{RELAY}/printers", headers=bearer_of(client, "camera"))
    assert response.status_code == 403
    assert response.json()["reason"] == "wrong_caller"


def test_camera_routes_return_no_code(client: TestClient, printer: str) -> None:
    headers = bearer_of(client, "camera")
    [listed] = client.get(f"{CAMERA}/printers", headers=headers).json()
    assert "access_code" not in listed
    one = client.get(f"{CAMERA}/printers/{printer}", headers=headers)
    assert one.json() == listed
    assert CODE not in one.text
    missing = client.get(f"{CAMERA}/printers/{uuid.uuid4()}", headers=headers)
    assert (missing.status_code, missing.json()["reason"]) == (404, "not_recorded")


def test_relay_reads_codes(client: TestClient, printer: str) -> None:
    headers = bearer_of(client, "camera-relay")
    [listed] = client.get(f"{RELAY}/printers", headers=headers).json()
    assert (listed["printer_id"], listed["access_code"]) == (printer, CODE)
    one = client.get(f"{RELAY}/printers/{printer}", headers=headers)
    assert one.json()["access_code"] == CODE
    assert client.get(f"{RELAY}/printers/nope", headers=headers).status_code == 404


def test_segments_409_when_not_leader(client: TestClient, printer: str) -> None:
    headers = bearer_of(client, "camera")
    body = {"recorder": "r1", "segments": [_segment(datetime.now(UTC), f"camera/{printer}/a.mp4")]}
    response = client.post(f"{CAMERA}/printers/{printer}/segments", json=body, headers=headers)
    assert (response.status_code, response.json()["reason"]) == (409, "not_leader")


def test_lease_answers_indexed_until(client: TestClient, printer: str) -> None:
    headers = bearer_of(client, "camera")
    lease = f"{CAMERA}/printers/{printer}/lease"
    first = client.post(lease, json={"recorder": "r1", "action": "acquire"}, headers=headers)
    assert first.json()["held"] is True
    assert first.json()["indexed_until"] is None
    other = client.post(lease, json={"recorder": "r2", "action": "acquire"}, headers=headers)
    assert (other.json()["held"], other.json()["holder"]) == (False, "r1")
    start = datetime.now(UTC) - timedelta(seconds=10)
    body = {"recorder": "r1", "segments": [_segment(start, f"camera/{printer}/a.mp4")]}
    posted = client.post(f"{CAMERA}/printers/{printer}/segments", json=body, headers=headers)
    assert posted.json() == {"indexed": 1}
    renewed = client.post(lease, json={"recorder": "r1", "action": "renew"}, headers=headers)
    until = datetime.fromisoformat(renewed.json()["indexed_until"])
    assert until == start + timedelta(seconds=4)
    released = client.post(lease, json={"recorder": "r1", "action": "release"}, headers=headers)
    assert released.json()["held"] is False


def test_segments_422_for_another_printers_key(client: TestClient, printer: str) -> None:
    headers = bearer_of(client, "camera")
    lease = f"{CAMERA}/printers/{printer}/lease"
    client.post(lease, json={"recorder": "r1", "action": "acquire"}, headers=headers)
    key = f"rec/camera/{uuid.uuid4()}/a.mp4"
    body = {"recorder": "r1", "segments": [_segment(datetime.now(UTC), key)]}
    response = client.post(f"{CAMERA}/printers/{printer}/segments", json=body, headers=headers)
    assert (response.status_code, response.json()["reason"]) == (422, "foreign_object_key")


def test_segments_are_capped_at_500(client: TestClient, printer: str) -> None:
    headers = bearer_of(client, "camera")
    now = datetime.now(UTC)
    segments = [_segment(now, f"camera/{i}.mp4") for i in range(501)]
    body = {"recorder": "r1", "segments": segments}
    response = client.post(f"{CAMERA}/printers/{printer}/segments", json=body, headers=headers)
    assert response.status_code == 422


def test_feed_change_emits_once(
    client: TestClient, printer: str, events: list[CameraFeedEvent]
) -> None:
    headers = bearer_of(client, "camera")
    url = f"{CAMERA}/printers/{printer}/feed"
    for _ in range(2):
        response = client.post(url, json={"part": "recorder", "state": "pulling"}, headers=headers)
        assert response.status_code == 204
    assert [(e.kind, e.printer_id) for e in events] == [("camera.feed", printer)]
    client.post(url, json={"part": "recorder", "state": "stalled"}, headers=headers)
    assert len(events) == 2


def test_relay_feed_is_the_relay_part(
    client: TestClient, printer: str, events: list[CameraFeedEvent]
) -> None:
    headers = bearer_of(client, "camera-relay")
    url = f"{RELAY}/printers/{printer}/feed"
    assert (
        client.post(url, json={"part": "recorder", "state": "pulling"}, headers=headers).status_code
        == 204
    )
    pool = _state(client).settings_store.pool
    with pool.connection() as conn:
        rows = conn.execute("SELECT part FROM camera_feeds").fetchall()
    assert [r["part"] for r in rows] == ["relay"]
    assert len(events) == 1


def test_feed_and_lease_404_for_a_deleted_printer(
    client: TestClient, printer: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A printer deleted while its feed runs: 404, not a 500 the callers retry forever."""
    _state(client).components.get(PRINTERS).store.delete(printer)
    camera = bearer_of(client, "camera")
    relay = bearer_of(client, "camera-relay")
    feed = {"part": "recorder", "state": "stopped"}
    caplog.clear()
    answers = [
        client.post(f"{CAMERA}/printers/{printer}/feed", json=feed, headers=camera),
        client.post(f"{RELAY}/printers/{printer}/feed", json=feed, headers=relay),
        client.post(
            f"{CAMERA}/printers/{printer}/lease",
            json={"recorder": "r1", "action": "acquire"},
            headers=camera,
        ),
        client.post(
            f"{CAMERA}/printers/{printer}/lease",
            json={"recorder": "r1", "action": "renew"},
            headers=camera,
        ),
    ]
    assert [(a.status_code, a.json()["reason"]) for a in answers] == [
        (404, "printer_not_found")
    ] * 4
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]


def test_areas_list_printers_and_changes(client: TestClient) -> None:
    for caller in ("camera", "camera-relay"):
        response = client.get(
            f"/api/v1/internal/crud/{caller}/areas", headers=bearer_of(client, caller)
        )
        assert response.json() == {"areas": ["printers", "changes"]}
