"""Location sensors and their readings (#6268)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament.catalog import CatalogRefError, CatalogStore
from scadbuddy.filament.locations import LocationStore, repoint_printer_locations
from scadbuddy.filament.models import (
    LocationCreate,
    LocationKind,
    MaterialCreate,
    SensorIn,
    SensorPatch,
)
from scadbuddy.filament.sensors import SensorStore, repoint_printer_sensors
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _place(pool: PgPool) -> UUID:
    kinds = CatalogStore(pool).list("location-kinds")
    kind = next(r.id for r in kinds if isinstance(r, LocationKind) and r.name == "Shelf")
    return LocationStore(pool).create(LocationCreate(name="Dry box", kind_id=kind)).id


def _printer(pool: PgPool) -> UUID:
    printer = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, %s, 'P')",
            (printer, f"SN{printer.hex[:8]}"),
        )
    return printer


def _sensor(pool: PgPool, **extra: object) -> UUID:
    body = SensorIn.model_validate(
        {
            "location_id": _place(pool),
            "source": "home_assistant",
            "kind": "humidity",
            "entity_id": "sensor.box",
            "name": "Box",
            "unit": "%",
            **extra,
        }
    )
    return SensorStore(pool).create(body, actor="ann").id


def _record(pool: PgPool, sensor: UUID, value: float, at: datetime) -> bool:
    with pool.connection() as conn, conn.transaction():
        return SensorStore.record_on(conn, sensor, value, at)


def test_a_sensor_stores_a_changed_value(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool)
    assert _record(pg_pool, sensor, 40.0, T0)
    assert not _record(pg_pool, sensor, 40.0, T0 + timedelta(minutes=1))
    assert _record(pg_pool, sensor, 41.5, T0 + timedelta(minutes=2))
    readings = SensorStore(pg_pool).readings(sensor, T0 - timedelta(hours=1), 100)
    assert [r.value for r in readings] == [40.0, 41.5]
    got = SensorStore(pg_pool).list(None)[0]
    assert (got.last_value, got.last_at, got.reachable) == (41.5, T0 + timedelta(minutes=2), True)


def test_a_same_value_is_stored_again_after_15_minutes(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool)
    assert _record(pg_pool, sensor, 40.0, T0)
    assert not _record(pg_pool, sensor, 40.0, T0 + timedelta(minutes=14))
    assert _record(pg_pool, sensor, 40.0, T0 + timedelta(minutes=15))
    assert len(SensorStore(pg_pool).readings(sensor, T0 - timedelta(hours=1), 100)) == 2


def test_alerting_follows_the_last_value(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=50, alert_below=10)
    store = SensorStore(pg_pool)
    assert not store.list(None)[0].alerting
    _record(pg_pool, sensor, 60.0, T0)
    assert store.list(None)[0].alerting
    _record(pg_pool, sensor, 30.0, T0 + timedelta(minutes=1))
    assert not store.list(None)[0].alerting
    _record(pg_pool, sensor, 5.0, T0 + timedelta(minutes=2))
    assert store.list(None)[0].alerting


def test_an_ams_sensor_is_one_per_unit_and_kind(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    with pg_pool.connection() as conn:
        place = LocationStore.ensure_ams_unit_on(conn, printer, 0)
    body = SensorIn.model_validate(
        {
            "location_id": place,
            "source": "ams",
            "kind": "humidity",
            "printer_id": printer,
            "ams_id": 0,
            "name": "AMS 0",
        }
    )
    store = SensorStore(pg_pool)
    store.create(body, actor="x")
    with pytest.raises(ValueError, match="already"):
        store.create(body, actor="x")
    with pytest.raises(ValueError, match="entity_id"):
        SensorIn.model_validate(
            {"location_id": place, "source": "home_assistant", "kind": "humidity", "name": "n"}
        )


def test_archive_hides_a_sensor(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool)
    store = SensorStore(pg_pool)
    store.archive(sensor, actor="ann")
    assert store.list(None) == []


def test_one_ams_unit_location_per_unit(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    with pg_pool.connection() as conn:
        first = LocationStore.ensure_ams_unit_on(conn, printer, 0)
        again = LocationStore.ensure_ams_unit_on(conn, printer, 0)
        other = LocationStore.ensure_ams_unit_on(conn, printer, 1)
    assert first == again != other


def test_tray_locations_hang_under_their_unit(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    store = LocationStore(pg_pool)
    tray = store.ensure_ams(printer, 0, 2, label="T")
    assert tray.parent_id is not None
    with pg_pool.connection() as conn:
        unit = LocationStore.ensure_ams_unit_on(conn, printer, 0)
    assert tray.parent_id == unit
    assert store.ensure_ams(printer, 0, 3, label="T3").parent_id == unit


def test_a_merge_keeps_the_kept_printers_unit_sensor(pg_pool: PgPool) -> None:
    gone, kept, lone = _printer(pg_pool), _printer(pg_pool), _printer(pg_pool)
    store = SensorStore(pg_pool)
    made: dict[UUID, list[UUID]] = {}
    for printer in (gone, kept, lone):
        with pg_pool.connection() as conn:
            place = LocationStore.ensure_ams_unit_on(conn, printer, 0)
        kinds = ("humidity", "temperature") if printer == gone else ("humidity",)
        made[printer] = [
            store.create(
                SensorIn.model_validate(
                    {
                        "location_id": place,
                        "source": "ams",
                        "kind": kind,
                        "printer_id": printer,
                        "ams_id": 0,
                        "name": kind,
                    }
                ),
                actor="x",
            ).id
            for kind in kinds
        ]
    with pg_pool.connection() as conn:
        kept_unit = LocationStore.ensure_ams_unit_on(conn, kept, 0)
        with conn.transaction():
            repoint_printer_locations(conn, gone, kept)
            repoint_printer_sensors(conn, gone, kept)
    live = {s.id: s for s in store.list(None)}
    assert made[gone][0] not in live
    assert live[made[gone][1]].printer_id == kept
    assert live[made[gone][1]].location_id == kept_unit
    assert {s.id for s in store.list(kept_unit)} == {made[kept][0], made[gone][1]}
    assert made[kept][0] in live and made[lone][0] in live


def test_an_external_holder_has_no_unit_place(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    tray = LocationStore(pg_pool).ensure_ams(printer, 255, 0, label="Ext")
    assert tray.parent_id is None
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM filament_locations WHERE printer_id = %s"
            " AND tray_id IS NULL",
            (printer,),
        ).fetchone()
    assert row == {"n": 0}


def test_a_non_finite_or_older_reading_stores_nothing(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool)
    assert _record(pg_pool, sensor, 40.0, T0 + timedelta(minutes=5))
    assert not _record(pg_pool, sensor, float("nan"), T0 + timedelta(minutes=6))
    assert not _record(pg_pool, sensor, float("inf"), T0 + timedelta(minutes=7))
    assert not _record(pg_pool, sensor, 55.0, T0)
    assert not _record(pg_pool, sensor, 55.0, T0 + timedelta(minutes=5))
    got = SensorStore(pg_pool).list(None)[0]
    assert (got.last_value, got.last_at) == (40.0, T0 + timedelta(minutes=5))
    assert len(SensorStore(pg_pool).readings(sensor, T0 - timedelta(hours=1), 100)) == 1


def test_sensor_input_is_checked() -> None:
    base: dict[str, object] = {
        "location_id": uuid4(),
        "kind": "humidity",
        "name": "n",
    }
    ha = base | {"source": "home_assistant", "entity_id": "sensor.a"}
    with pytest.raises(ValueError, match="printer_id"):
        SensorIn.model_validate(ha | {"printer_id": uuid4()})
    ams = base | {"source": "ams", "printer_id": uuid4(), "ams_id": 0}
    with pytest.raises(ValueError, match="entity_id"):
        SensorIn.model_validate(ams | {"entity_id": "sensor.a"})
    with pytest.raises(ValueError, match="alert_below"):
        SensorIn.model_validate(ha | {"alert_above": 10, "alert_below": 20})
    with pytest.raises(ValueError):
        SensorIn.model_validate(ha | {"alert_above": float("nan")})


def test_a_sensor_needs_a_live_location(pg_pool: PgPool) -> None:
    place = _place(pg_pool)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_locations SET archived_at = now() WHERE id = %s", (place,))
    body = SensorIn.model_validate(
        {
            "location_id": place,
            "source": "home_assistant",
            "kind": "humidity",
            "entity_id": "sensor.a",
            "name": "n",
        }
    )
    with pytest.raises(CatalogRefError):
        SensorStore(pg_pool).create(body, actor="x")
    live = _sensor(pg_pool)
    with pytest.raises(CatalogRefError):
        SensorStore(pg_pool).update(live, SensorPatch(location_id=place), actor="x")


def test_a_max_hours_value_fits_its_column() -> None:
    assert MaterialCreate(name="x", max_hours_outside_dry=Decimal("99999.9"))
    with pytest.raises(ValueError):
        MaterialCreate(name="x", max_hours_outside_dry=Decimal("99999.95"))
