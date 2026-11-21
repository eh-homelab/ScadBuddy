"""A location sensor's alert notifications, queued (#6646): one when it starts alerting and
one when it stops, queued under the sensor's row lock with the message made then, and
claimed and ended for the print worker (plan Decisions 29-46). The send itself is the print
worker's: `tests/filament/test_sensor_notify_worker.py`."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest

from scadbuddy.filament import sensors
from scadbuddy.filament.models import NotificationEnd, SensorIn, SensorPatch
from scadbuddy.filament.sensors import ALERT_CLEAR_AFTER, SensorStore, claim_on, end_on
from tests.conftest import PgPool
from tests.filament.test_sensors import T0, _record, _sensor

pytestmark = pytest.mark.requires_postgres

#: Past the hold an open alert needs before it closes.
CLEAR = ALERT_CLEAR_AFTER + timedelta(minutes=1)


def _rows(pool: PgPool, sensor: UUID) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, kind, value, read_at, title, message, state, channels, error"
            " FROM filament_sensor_notifications WHERE sensor_id = %s ORDER BY read_at",
            (sensor,),
        ).fetchall()
        return [dict(row) for row in rows]


def _kinds(pool: PgPool, sensor: UUID) -> list[str]:
    return [row["kind"] for row in _rows(pool, sensor)]


def _claim(pool: PgPool, limit: int = 10) -> list[Any]:
    with pool.connection() as conn, conn.transaction():
        return claim_on(conn, limit)


def _end(pool: PgPool, row: UUID, end: NotificationEnd) -> None:
    with pool.connection() as conn, conn.transaction():
        end_on(conn, row, end)


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


def test_an_alert_stops_once_the_readings_stay_inside_for_the_hold(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, alert_below=10, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    _record(pg_pool, sensor, 30.0, T0 + timedelta(minutes=1))
    assert _kinds(pg_pool, sensor) == ["started"]
    _record(pg_pool, sensor, 31.0, T0 + timedelta(minutes=1) + CLEAR)
    assert _kinds(pg_pool, sensor) == ["started", "stopped"]
    _record(pg_pool, sensor, 5.0, T0 + timedelta(minutes=2) + CLEAR)
    assert _kinds(pg_pool, sensor) == ["started", "stopped", "started"]


def test_a_flapping_reading_sends_at_most_one_start(pg_pool: PgPool) -> None:
    """Decision 39: AMS humidity stores every change, so 20, 21, 20, 21 around a limit of
    20 must not send a notification each time."""
    sensor = _sensor(pg_pool, alert_above=20, notify=True)
    for minute, value in enumerate([20.0, 21.0, 20.0, 21.0, 20.0, 21.0, 20.0]):
        _record(pg_pool, sensor, value, T0 + timedelta(minutes=minute))
    assert _kinds(pg_pool, sensor) == ["started"]


def test_a_repeated_inside_value_closes_the_alert_after_the_hold(pg_pool: PgPool) -> None:
    """The same value inside the band is stored again once the hold is over, so a sensor
    whose value settles still says it stopped, without waiting for `SAME_VALUE_EVERY`."""
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    _record(pg_pool, sensor, 40.0, T0 + timedelta(minutes=1))
    assert not _record(pg_pool, sensor, 40.0, T0 + timedelta(minutes=2))
    assert ALERT_CLEAR_AFTER < sensors.SAME_VALUE_EVERY
    assert _record(pg_pool, sensor, 40.0, T0 + timedelta(minutes=1) + CLEAR)
    assert _kinds(pg_pool, sensor) == ["started", "stopped"]


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
    _record(pg_pool, sensor, 30.0, T0 + CLEAR)
    assert _kinds(pg_pool, sensor) == []


def test_notify_turned_on_during_an_alert_sends_only_its_end(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50)
    store = SensorStore(pg_pool)
    _record(pg_pool, sensor, 60.0, T0)
    store.update(sensor, SensorPatch(notify=True), actor="ann")
    _record(pg_pool, sensor, 65.0, T0 + timedelta(minutes=1))
    assert _kinds(pg_pool, sensor) == []
    _record(pg_pool, sensor, 30.0, T0 + timedelta(minutes=2))
    _record(pg_pool, sensor, 31.0, T0 + timedelta(minutes=2) + CLEAR)
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
    _record(pg_pool, sensor, 31.0, T0 + timedelta(minutes=1) + CLEAR)
    last = store.list(None)[0].last_notification
    assert last is not None
    assert (last.kind, last.value, last.state, last.channel) == (
        "stopped",
        31.0,
        "queued",
        "bambuddy",
    )
    patched = store.update(sensor, SensorPatch(name="Box 2"), actor="ann")
    assert patched.last_notification == last


def test_the_last_notification_is_the_newest_reading_not_the_newest_insert(
    pg_pool: PgPool,
) -> None:
    """Finding 5: `queued_at` is when a writer's statement ran, so a writer that waited
    on the row lock can insert the newer reading's row first. The newest is by `read_at`."""
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    with pg_pool.connection() as conn:
        for kind, read_at, queued_at in (
            ("started", T0, T0 + timedelta(minutes=5)),
            ("stopped", T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)),
        ):
            conn.execute(
                "INSERT INTO filament_sensor_notifications"
                " (sensor_id, kind, value, read_at, title, message, queued_at)"
                " VALUES (%s, %s, 1, %s, 't', 'm', %s)",
                (sensor, kind, read_at, queued_at),
            )
    last = SensorStore(pg_pool).list(None)[0].last_notification
    assert last is not None and last.kind == "stopped"


def test_the_message_is_made_when_the_reading_is_queued(pg_pool: PgPool) -> None:
    """Finding 6: a band edited after the reading does not change what is sent, and the
    reading's time is written in UTC whatever offset it came with."""
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    eastern = timezone(timedelta(hours=-5))
    _record(pg_pool, sensor, 62.5, datetime(2026, 11, 18, 7, 30, tzinfo=eastern))
    SensorStore(pg_pool).update(sensor, SensorPatch(alert_above=70), actor="ann")
    (claimed,) = _claim(pg_pool)
    assert claimed.title == "Box: above the limit"
    assert "62.5 %" in claimed.message and "Dry box" in claimed.message
    assert "above 50 %" in claimed.message
    assert "Read 2026-11-18 12:30 UTC" in claimed.message


def test_a_stop_says_the_sensor_is_back(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_below=20, notify=True)
    _record(pg_pool, sensor, 10.0, T0)
    (started,) = _claim(pg_pool)
    assert started.title == "Box: below the limit"
    _record(pg_pool, sensor, 25.0, T0 + timedelta(minutes=1))
    _record(pg_pool, sensor, 26.0, T0 + timedelta(minutes=1) + CLEAR)
    (stopped,) = _claim(pg_pool)
    assert stopped.title == "Box: back within its limits"


def test_a_claim_marks_the_rows_sending_and_an_end_records_the_answer(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    (claimed,) = _claim(pg_pool)
    assert _rows(pg_pool, sensor)[0]["state"] == "sending"
    assert _claim(pg_pool) == []
    _end(pg_pool, claimed.id, NotificationEnd(channels=2))
    _end(pg_pool, claimed.id, NotificationEnd(error="unavailable"))
    (row,) = _rows(pg_pool, sensor)
    assert (row["state"], row["channels"], row["error"]) == ("sent", 2, None)
    last = SensorStore(pg_pool).list(None)[0].last_notification
    assert last is not None and last.sent_at is not None


def test_a_claim_skips_a_sensor_whose_notify_was_turned_off(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    SensorStore(pg_pool).update(sensor, SensorPatch(notify=False), actor="ann")
    assert _claim(pg_pool) == []
    (row,) = _rows(pg_pool, sensor)
    assert (row["state"], row["error"]) == ("failed", "cancelled")


def test_a_claim_skips_an_archived_sensor(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    SensorStore(pg_pool).archive(sensor, actor="ann")
    assert _claim(pg_pool) == []
    assert _rows(pg_pool, sensor)[0]["error"] == "cancelled"


def test_a_claim_sends_only_the_newest_row_of_a_sensor(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    _record(pg_pool, sensor, 30.0, T0 + timedelta(minutes=1))
    _record(pg_pool, sensor, 31.0, T0 + timedelta(minutes=1) + CLEAR)
    _record(pg_pool, sensor, 70.0, T0 + timedelta(minutes=2) + CLEAR)
    (claimed,) = _claim(pg_pool)
    rows = _rows(pg_pool, sensor)
    assert [(r["kind"], r["state"], r["error"]) for r in rows] == [
        ("started", "failed", "superseded"),
        ("stopped", "failed", "superseded"),
        ("started", "sending", None),
    ]
    assert claimed.id == rows[-1]["id"]


def test_a_claim_honours_its_limit_oldest_first(pg_pool: PgPool) -> None:
    first = _sensor(pg_pool, alert_above=50, notify=True)
    (made,) = SensorStore(pg_pool).list(None)
    second = (
        SensorStore(pg_pool)
        .create(
            SensorIn.model_validate(
                {
                    "location_id": made.location_id,
                    "source": "home_assistant",
                    "kind": "temperature",
                    "entity_id": "sensor.box_t",
                    "name": "Box T",
                    "alert_above": 50,
                    "notify": True,
                }
            ),
            actor="ann",
        )
        .id
    )
    _record(pg_pool, second, 60.0, T0 + timedelta(minutes=1))
    _record(pg_pool, first, 60.0, T0)
    (claimed,) = _claim(pg_pool, 1)
    assert claimed.id == _rows(pg_pool, first)[0]["id"]
    assert _rows(pg_pool, second)[0]["state"] == "queued"


def test_a_claim_ends_a_row_left_sending_as_interrupted(pg_pool: PgPool) -> None:
    """Finding 4: a crash between the claim and the end leaves a row `sending`; a later
    claim ends it `interrupted` and never sends it again."""
    sensor = _sensor(pg_pool, alert_above=50, notify=True)
    _record(pg_pool, sensor, 60.0, T0)
    _claim(pg_pool)
    assert _claim(pg_pool) == []
    assert _rows(pg_pool, sensor)[0]["state"] == "sending"
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_sensor_notifications SET claimed_at = claimed_at - %s",
            (sensors.INTERRUPTED_AFTER + timedelta(seconds=1),),
        )
    assert _claim(pg_pool) == []
    (row,) = _rows(pg_pool, sensor)
    assert (row["state"], row["error"]) == ("failed", "interrupted")
