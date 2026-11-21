"""A sensor's queued notifications sent by the print worker (#6646, plan Decision 40): it
claims the rows through SB's side (`SensorNotificationsApi`), calls Bambuddy's
``POST /notifications/app-message`` and ends each row with Bambuddy's answer or a fixed
code. Here SB's side is the API's own store (`PoolSensorNotifications`), as the API's
in-process print worker runs it; the CRUD routes are `tests/crud/test_crud_sensor_notify.py`."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from scadbuddy.filament import sensor_notify
from scadbuddy.filament.sensors import PoolSensorNotifications, SensorStore
from scadbuddy.library.settings_store import StoredSettings
from tests.conftest import PgPool
from tests.filament.test_sensors import T0, _record, _sensor

pytestmark = pytest.mark.requires_postgres

BB = "https://bambuddy.test"
SEND = f"{BB}/api/v1/notifications/app-message"
SETTINGS = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")


async def _settings() -> StoredSettings:
    return SETTINGS


async def _none() -> StoredSettings:
    return StoredSettings()


def _rows(pool: PgPool, sensor: UUID) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT state, channels, error FROM filament_sensor_notifications"
            " WHERE sensor_id = %s ORDER BY read_at",
            (sensor,),
        ).fetchall()
        return [dict(row) for row in rows]


async def test_a_queued_notification_is_sent_once(pg_pool: PgPool) -> None:
    api = PoolSensorNotifications(pg_pool)
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 62.5, T0)
    with respx.mock() as router:
        send = router.post(SEND).respond(json={"channels": 2})
        report = await sensor_notify.deliver_pending(api, _settings)
        assert (report.sent, report.failed) == (1, 0)
        again = await sensor_notify.deliver_pending(api, _settings)
        assert (again.sent, again.failed) == (0, 0)
    assert send.call_count == 1
    request = send.calls[0].request
    assert request.headers["X-API-Key"] == "k"
    body = json.loads(request.content)
    assert body["title"] == "Box: above the limit"
    assert "62.5 %" in body["message"] and "Dry box" in body["message"]
    assert _rows(pg_pool, sensor) == [{"state": "sent", "channels": 2, "error": None}]
    last = SensorStore(pg_pool).list(None)[0].last_notification
    assert last is not None and last.sent_at is not None


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(200, json={"channels": 0}), "no_channel"),
        (httpx.Response(401, json={"detail": "bad key"}), "unauthorized"),
        (httpx.Response(403, json={"detail": "nope"}), "scope"),
        (httpx.Response(404, json={"detail": "Not Found"}), "not_found"),
        (httpx.Response(422, json={"detail": "too long"}), "rejected"),
        (httpx.Response(429, json={"detail": "slow down"}), "rate_limited"),
        (httpx.Response(503), "unavailable"),
    ],
)
async def test_a_failed_send_is_recorded_and_not_tried_again(
    pg_pool: PgPool, answer: httpx.Response, error: str
) -> None:
    api = PoolSensorNotifications(pg_pool)
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    with respx.mock() as router:
        send = router.post(SEND).mock(return_value=answer)
        report = await sensor_notify.deliver_pending(api, _settings)
        await sensor_notify.deliver_pending(api, _settings)
    assert (report.sent, report.failed) == (0, 1)
    assert send.call_count == 1
    (row,) = _rows(pg_pool, sensor)
    assert (row["state"], row["error"]) == ("failed", error)
    last = SensorStore(pg_pool).list(None)[0].last_notification
    assert last is not None and last.error == error


async def test_with_no_bambuddy_a_notification_waits(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    with respx.mock():
        report = await sensor_notify.deliver_pending(PoolSensorNotifications(pg_pool), _none)
    assert (report.sent, report.failed) == (0, 0)
    assert _rows(pg_pool, sensor)[0]["state"] == "queued"


async def test_the_activity_sends_through_the_api_it_was_given(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    activities = sensor_notify.SensorNotifyActivities(PoolSensorNotifications(pg_pool), _settings)
    with respx.mock() as router:
        router.post(SEND).respond(json={"channels": 1})
        report = await activities.deliver()
    assert (report.sent, report.failed) == (1, 0)


async def test_the_pull_no_longer_sends(pg_pool: PgPool) -> None:
    """Decision 40: the API's pull makes no Bambuddy call for a notification."""
    from scadbuddy.core.settings import FilamentSync
    from scadbuddy.filament.bambuddy_pull import scheduled_pull
    from tests.filament.test_bambuddy_pull import _filament

    f = _filament(pg_pool)
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    nothing = FilamentSync(spools="off", weights="off", tags="off", presets="off")
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BB}/api/v1/location-ha-sensors/entities").respond(json=[])
        send = router.post(SEND).respond(json={"channels": 1})
        assert await scheduled_pull(f, lambda: SETTINGS, lambda: nothing) == "skipped"
    assert send.call_count == 0
    assert _rows(pg_pool, sensor)[0]["state"] == "queued"
