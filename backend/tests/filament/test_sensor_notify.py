"""A location sensor's alert notifications (#6646): one when it starts alerting and one
when it stops, queued under the sensor's row lock and sent through Bambuddy's
app-message route by the spool refresh's pull."""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from scadbuddy.filament import sensor_notify
from scadbuddy.filament.models import SensorPatch
from scadbuddy.filament.sensors import SensorStore
from scadbuddy.library.settings_store import StoredSettings
from tests.conftest import PgPool
from tests.filament.test_bambuddy_pull import _filament
from tests.filament.test_sensors import T0, _record, _sensor

pytestmark = pytest.mark.requires_postgres

BB = "https://bambuddy.test"
SEND = f"{BB}/api/v1/notifications/app-message"
SETTINGS = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")


def _rows(pool: PgPool, sensor: UUID) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT kind, value, read_at, state, channels, error"
            " FROM filament_sensor_notifications WHERE sensor_id = %s ORDER BY queued_at, read_at",
            (sensor,),
        ).fetchall()
        return [dict(row) for row in rows]


def _kinds(pool: PgPool, sensor: UUID) -> list[str]:
    return [row["kind"] for row in _rows(pool, sensor)]


def test_a_sensor_that_starts_alerting_queues_one_notification(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 40.0, T0)
    assert _kinds(pg_pool, sensor) == []
    _record(pg_pool, sensor, 60.0, T0 + timedelta(minutes=1))
    (row,) = _rows(pg_pool, sensor)
    assert (row["kind"], row["value"], row["read_at"], row["state"]) == (
        "started",
        60.0,
        T0 + timedelta(minutes=1),
        "queued",
    )


def test_a_sensor_that_stays_alerting_queues_nothing_more(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    _record(pg_pool, sensor, 61.0, T0 + timedelta(minutes=1))
    _record(pg_pool, sensor, 61.0, T0 + timedelta(minutes=30))
    _record(pg_pool, sensor, 70.0, T0 + timedelta(minutes=31))
    assert _kinds(pg_pool, sensor) == ["started"]


def test_a_sensor_that_stops_alerting_queues_one_notification(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, alert_below=10, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    _record(pg_pool, sensor, 30.0, T0 + timedelta(minutes=1))
    _record(pg_pool, sensor, 31.0, T0 + timedelta(minutes=2))
    _record(pg_pool, sensor, 5.0, T0 + timedelta(minutes=3))
    assert _kinds(pg_pool, sensor) == ["started", "stopped", "started"]


def test_a_reading_that_arrives_twice_queues_once(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    _record(pg_pool, sensor, 60.0, T0)
    assert _kinds(pg_pool, sensor) == ["started"]


def test_concurrent_readings_queue_once(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    start = threading.Barrier(4)

    def read(minute: int) -> None:
        start.wait()
        _record(pg_pool, sensor, 60.0 + minute, T0 + timedelta(minutes=minute))

    threads = [threading.Thread(target=read, args=(m,)) for m in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _kinds(pg_pool, sensor) == ["started"]


def test_notify_off_queues_nothing(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50)
    _record(pg_pool, sensor, 60.0, T0)
    _record(pg_pool, sensor, 30.0, T0 + timedelta(minutes=1))
    assert _kinds(pg_pool, sensor) == []


def test_notify_turned_on_during_an_alert_sends_only_its_end(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50)
    store = SensorStore(pg_pool)
    _record(pg_pool, sensor, 60.0, T0)
    store.update(sensor, SensorPatch(notify=True), actor="ann")
    _record(pg_pool, sensor, 65.0, T0 + timedelta(minutes=1))
    assert _kinds(pg_pool, sensor) == []
    _record(pg_pool, sensor, 30.0, T0 + timedelta(minutes=2))
    assert _kinds(pg_pool, sensor) == ["stopped"]


def test_a_changed_band_is_judged_at_the_next_stored_reading(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    store = SensorStore(pg_pool)
    _record(pg_pool, sensor, 45.0, T0)
    store.update(sensor, SensorPatch(alert_above=40), actor="ann")
    assert store.list(None)[0].alerting
    assert _kinds(pg_pool, sensor) == []
    _record(pg_pool, sensor, 46.0, T0 + timedelta(minutes=1))
    assert _kinds(pg_pool, sensor) == ["started"]


def test_the_sensor_shows_its_last_notification(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    store = SensorStore(pg_pool)
    assert store.list(None)[0].last_notification is None
    _record(pg_pool, sensor, 60.0, T0)
    _record(pg_pool, sensor, 30.0, T0 + timedelta(minutes=1))
    last = store.list(None)[0].last_notification
    assert last is not None
    assert (last.kind, last.value, last.state, last.channel) == (
        "stopped",
        30.0,
        "queued",
        "bambuddy",
    )
    patched = store.update(sensor, SensorPatch(name="Box 2"), actor="ann")
    assert patched.last_notification == last


async def test_the_pull_sends_a_queued_notification_once(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 62.5, T0)
    with respx.mock() as router:
        send = router.post(SEND).respond(json={"channels": 2})
        report = await sensor_notify.deliver_pending(f, SETTINGS)
        assert (report.sent, report.failed) == (1, 0)
        again = await sensor_notify.deliver_pending(f, SETTINGS)
        assert (again.sent, again.failed) == (0, 0)
    assert send.call_count == 1
    request = send.calls[0].request
    assert request.headers["X-API-Key"] == "k"
    body = json.loads(request.content)
    assert body["title"] == "Box: above the limit"
    assert "62.5 %" in body["message"] and "Dry box" in body["message"]
    (row,) = _rows(pg_pool, sensor)
    assert (row["state"], row["channels"], row["error"]) == ("sent", 2, None)
    last = SensorStore(pg_pool).list(None)[0].last_notification
    assert last is not None and last.sent_at is not None


async def test_a_stop_says_the_sensor_is_back(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    sensor = _sensor(pg_pool, alert_below=20, notify=True)
    _record(pg_pool, sensor, 10.0, T0)
    _record(pg_pool, sensor, 25.0, T0 + timedelta(minutes=1))
    with respx.mock() as router:
        send = router.post(SEND).respond(json={"channels": 1})
        await sensor_notify.deliver_pending(f, SETTINGS)
    titles = [json.loads(c.request.content)["title"] for c in send.calls]
    assert titles == ["Box: below the limit", "Box: back within its limits"]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(200, json={"channels": 0}), "no_channel"),
        (httpx.Response(403, json={"detail": "nope"}), "scope"),
        (httpx.Response(429, json={"detail": "slow down"}), "rate_limited"),
        (httpx.Response(503), "unavailable"),
    ],
)
async def test_a_failed_send_is_recorded_and_not_tried_again(
    pg_pool: PgPool, answer: httpx.Response, error: str
) -> None:
    f = _filament(pg_pool)
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    with respx.mock() as router:
        send = router.post(SEND).mock(return_value=answer)
        report = await sensor_notify.deliver_pending(f, SETTINGS)
        await sensor_notify.deliver_pending(f, SETTINGS)
    assert (report.sent, report.failed) == (0, 1)
    assert send.call_count == 1
    (row,) = _rows(pg_pool, sensor)
    assert (row["state"], row["error"]) == ("failed", error)
    last = SensorStore(pg_pool).list(None)[0].last_notification
    assert last is not None and last.error == error


async def test_with_no_bambuddy_a_notification_waits(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    with respx.mock():
        report = await sensor_notify.deliver_pending(f, StoredSettings())
    assert (report.sent, report.failed) == (0, 0)
    assert _rows(pg_pool, sensor)[0]["state"] == "queued"


async def test_the_scheduled_pull_sends_the_queued_notifications(pg_pool: PgPool) -> None:
    from scadbuddy.core.settings import FilamentSync
    from scadbuddy.filament.bambuddy_pull import scheduled_pull

    f = _filament(pg_pool)
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    nothing = FilamentSync(spools="off", weights="off", tags="off", presets="off")
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/location-ha-sensors/entities").respond(json=[])
        send = router.post(SEND).respond(json={"channels": 1})
        assert await scheduled_pull(f, lambda: SETTINGS, lambda: nothing) == "skipped"
    assert send.call_count == 1
