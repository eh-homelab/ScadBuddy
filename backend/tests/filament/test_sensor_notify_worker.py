"""A sensor's queued notifications sent by the print worker (#6646, plan Decision 40): it
claims the rows through SB's side (`SensorNotificationsApi`), calls Bambuddy's
``POST /notifications/app-message`` and ends each row with Bambuddy's answer or a fixed
code. Here SB's side is the API's own store (`PoolSensorNotifications`), as the API's
in-process print worker runs it; the CRUD routes are `tests/crud/test_crud_sensor_notify.py`."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from scadbuddy.filament import sensor_notify
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import LocationCreate, LocationKind, SensorIn
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


async def test_a_client_that_cannot_be_built_claims_nothing(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 50: the client is built before the claim, so a failure there leaves the
    row queued for the next pass, not ``sending`` until a claim ends it ``interrupted``."""
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)

    def broken(settings: StoredSettings) -> Any:
        raise RuntimeError("no client")

    monkeypatch.setattr(sensor_notify, "client_for", broken)
    with pytest.raises(RuntimeError, match="no client"):
        await sensor_notify.deliver_pending(PoolSensorNotifications(pg_pool), _settings)
    assert _rows(pg_pool, sensor) == [{"state": "queued", "channels": None, "error": None}]
    monkeypatch.undo()
    with respx.mock() as router:
        router.post(SEND).respond(json={"channels": 1})
        report = await sensor_notify.deliver_pending(PoolSensorNotifications(pg_pool), _settings)
    assert (report.sent, report.failed) == (1, 0)


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
    from tests.filament.pull_support import scheduled_pull
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


def _sensor_at(pool: PgPool, place_name: str) -> UUID:
    kinds = CatalogStore(pool).list("location-kinds")
    kind = next(r.id for r in kinds if isinstance(r, LocationKind) and r.name == "Shelf")
    place = LocationStore(pool).create(LocationCreate(name=place_name, kind_id=kind)).id
    body = SensorIn.model_validate(
        {
            "location_id": place,
            "source": "home_assistant",
            "kind": "humidity",
            "entity_id": f"sensor.{place_name.replace(' ', '_')}",
            "name": place_name,
            "unit": "%",
            "alert_above": 50,
            "notify": True,
        }
    )
    return SensorStore(pool).create(body, actor="ann").id


async def test_a_slow_bambuddy_hands_the_unsent_rows_back(pg_pool: PgPool) -> None:
    """The pass starts no send past its budget: the rows it never tried are ``queued``
    again, not left ``sending`` for a later claim to end ``interrupted``."""
    api = PoolSensorNotifications(pg_pool)
    sensors = []
    for n in range(4):
        sensor = _sensor_at(pg_pool, f"Box {n}")
        _record(pg_pool, sensor, 60.0, T0 + timedelta(minutes=n))
        sensors.append(sensor)

    # Each send takes 0.3 s of the pass's own clock, so the database's speed on the
    # runner does not decide how many sends start (#8412).
    now = [0.0]

    async def slow(request: httpx.Request) -> httpx.Response:
        now[0] += 0.3
        return httpx.Response(200, json={"channels": 1})

    with respx.mock() as router:
        send = router.post(SEND).mock(side_effect=slow)
        report = await sensor_notify.deliver_pending(
            api, _settings, budget=0.5, clock=lambda: now[0]
        )
    assert (report.sent, report.failed) == (2, 0)
    assert send.call_count == 2
    states = [_rows(pg_pool, s)[0] for s in sensors]
    assert [r["state"] for r in states] == ["sent", "sent", "queued", "queued"]
    assert all(r["error"] is None for r in states)
    with respx.mock() as router:
        router.post(SEND).respond(json={"channels": 1})
        again = await sensor_notify.deliver_pending(api, _settings)
    assert (again.sent, again.failed) == (2, 0)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadTimeout("slow"), "interrupted"),
        (httpx.WriteTimeout("slow"), "interrupted"),
        (httpx.ConnectTimeout("slow"), "unavailable"),
    ],
)
async def test_a_timeout_after_the_request_went_is_interrupted(
    pg_pool: PgPool, error: httpx.TimeoutException, code: str
) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    with respx.mock() as router:
        send = router.post(SEND).mock(side_effect=error)
        report = await sensor_notify.deliver_pending(PoolSensorNotifications(pg_pool), _settings)
    assert (report.sent, report.failed) == (0, 1)
    assert send.call_count == 1
    (row,) = _rows(pg_pool, sensor)
    assert (row["state"], row["error"]) == ("failed", code)
