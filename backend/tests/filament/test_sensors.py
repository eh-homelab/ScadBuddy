"""Location sensors and their readings (#6268)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.errors import CheckViolation

from scadbuddy.filament import prints, sensors
from scadbuddy.filament.catalog import CatalogRefError, CatalogStore
from scadbuddy.filament.locations import LocationStore, repoint_printer_locations
from scadbuddy.filament.models import (
    LocationCreate,
    LocationKind,
    LocationPatch,
    MaterialCreate,
    SensorIn,
    SensorPatch,
)
from scadbuddy.filament.sensors import SensorStore, repoint_printer_sensors
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
PRINTER_FIXTURES = Path(__file__).parents[1] / "printers" / "fixtures"


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


def _watched(pool: PgPool) -> tuple[str, PrinterReportStore]:
    printers = PrinterStore(pool, KEK)
    row = printers.create(PrinterCreate(serial="01P00A000000009", name="H2C"))
    printers.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    return row.id, PrinterReportStore(pool)


def _advance(reports: PrinterReportStore, pid: str, report: dict[str, Any], at: datetime) -> None:
    with reports.pool.connection() as conn, conn.transaction():
        reports.advance_in(conn, pid, report, at)


def _ams_readings(pool: PgPool, pid: str) -> dict[tuple[int, str], list[float]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT s.ams_id, s.kind, r.value FROM filament_sensor_readings r"
            " JOIN filament_location_sensors s ON s.id = r.sensor_id"
            " WHERE s.printer_id = %s AND s.source = 'ams' ORDER BY r.at",
            (pid,),
        ).fetchall()
    found: dict[tuple[int, str], list[float]] = {}
    for row in rows:
        found.setdefault((row["ams_id"], row["kind"]), []).append(row["value"])
    return found


def test_a_report_records_ams_humidity_and_temperature(pg_pool: PgPool) -> None:
    pid, reports = _watched(pg_pool)
    units = [
        {"id": "0", "humidity": "3", "humidity_raw": "29", "temp": "24.5"},
        {"id": "1", "humidity": "5", "humidity_raw": "16"},
    ]
    _advance(reports, pid, {"ams": {"ams": units}}, T0)
    assert _ams_readings(pg_pool, pid) == {
        (0, "humidity"): [29.0],
        (0, "temperature"): [24.5],
        (1, "humidity"): [16.0],
    }
    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT s.location_id, l.id AS unit FROM filament_location_sensors s"
            " JOIN filament_locations l ON l.printer_id = s.printer_id AND l.ams_id = s.ams_id"
            " AND l.tray_id IS NULL WHERE s.printer_id = %s",
            (pid,),
        ).fetchall()
    assert len(rows) == 3
    assert all(r["location_id"] == r["unit"] for r in rows)
    with pg_pool.connection() as conn:
        names = conn.execute(
            "SELECT l.name AS place, s.name FROM filament_location_sensors s"
            " JOIN filament_locations l ON l.id = s.location_id WHERE s.printer_id = %s"
            " AND s.ams_id = 0 AND s.kind = 'humidity'",
            (pid,),
        ).fetchone()
    assert names == {"place": "H2C AMS-A", "name": "H2C AMS-A humidity"}
    _advance(reports, pid, {"ams": {"ams": units}}, T0 + timedelta(minutes=1))
    assert _ams_readings(pg_pool, pid)[(0, "humidity")] == [29.0]


def test_a_recorded_h2c_report_stores_the_percent_not_the_level(pg_pool: PgPool) -> None:
    recorded = json.loads((PRINTER_FIXTURES / "lan-report-h2c-idle.json").read_text("utf-8"))
    pid, reports = _watched(pg_pool)
    _advance(reports, pid, recorded["print"], T0)
    humidity = {
        unit: values[0]
        for (unit, kind), values in _ams_readings(pg_pool, pid).items()
        if kind == "humidity"
    }
    assert humidity == {0: 29.0, 1: 16.0, 2: 12.0, 3: 14.0, 128: 15.0}


def test_a_level_without_a_percent_records_no_humidity(pg_pool: PgPool) -> None:
    pid, reports = _watched(pg_pool)
    units = [{"id": "0", "humidity": "3", "temp": "25"}]
    _advance(reports, pid, {"ams": {"ams": units}}, T0)
    assert _ams_readings(pg_pool, pid) == {(0, "temperature"): [25.0]}


def test_a_report_without_ams_writes_nothing(pg_pool: PgPool) -> None:
    pid, reports = _watched(pg_pool)
    _advance(reports, pid, {"gcode_state": "IDLE"}, T0)
    with pg_pool.connection() as conn:
        count = conn.execute("SELECT count(*) AS n FROM filament_location_sensors").fetchone()
    assert count is not None
    assert count["n"] == 0
    assert reports.get(pid) is not None


def test_an_empty_humidity_is_skipped(pg_pool: PgPool) -> None:
    pid, reports = _watched(pg_pool)
    _advance(reports, pid, {"ams": {"ams": [{"id": "0", "humidity_raw": "", "temp": "25"}]}}, T0)
    assert _ams_readings(pg_pool, pid) == {(0, "temperature"): [25.0]}


def test_a_failing_sensor_capture_keeps_the_report(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pid, reports = _watched(pg_pool)

    def boom(*args: object) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(sensors, "capture_ams_on", boom)
    with caplog.at_level(logging.WARNING):
        _advance(reports, pid, {"ams": {"ams": [{"id": "0", "humidity_raw": "29"}]}}, T0)
    assert reports.get(pid) is not None
    record = next(r for r in caplog.records if r.getMessage() == prints.FILAMENT_CAPTURE_FAILED)
    assert record.__dict__["error"] == "RuntimeError"
    assert "secret detail" not in caplog.text


class _Counting:
    """A connection that counts the statements run through ``execute``."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.statements = 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.statements += 1
        return self.conn.execute(*args, **kwargs)


def _unit(**more: str) -> dict[str, Any]:
    return {
        "ams": {"ams": [{"id": "0", "humidity": "3", "humidity_raw": "29", "temp": "24"} | more]}
    }


def test_an_unchanged_report_costs_one_query(pg_pool: PgPool) -> None:
    pid, reports = _watched(pg_pool)
    _advance(reports, pid, _unit(), T0)
    with pg_pool.connection() as conn, conn.transaction():
        counted = _Counting(conn)
        sensors.capture_ams_on(counted, pid, _unit(), T0 + timedelta(minutes=1))  # type: ignore[arg-type]
    assert counted.statements == 1
    assert _ams_readings(pg_pool, pid) == {(0, "humidity"): [29.0], (0, "temperature"): [24.0]}


def test_a_changed_value_inside_fifteen_minutes_is_stored(pg_pool: PgPool) -> None:
    pid, reports = _watched(pg_pool)
    _advance(reports, pid, _unit(), T0)
    _advance(reports, pid, _unit(humidity_raw="31"), T0 + timedelta(minutes=1))
    assert _ams_readings(pg_pool, pid)[(0, "humidity")] == [29.0, 31.0]


def test_the_same_value_is_stored_again_after_fifteen_minutes(pg_pool: PgPool) -> None:
    pid, reports = _watched(pg_pool)
    _advance(reports, pid, _unit(), T0)
    _advance(reports, pid, _unit(), T0 + sensors.SAME_VALUE_EVERY)
    assert _ams_readings(pg_pool, pid)[(0, "humidity")] == [29.0, 29.0]


def test_a_report_marks_an_unreachable_sensor_reachable(pg_pool: PgPool) -> None:
    pid, reports = _watched(pg_pool)
    _advance(reports, pid, _unit(), T0)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_location_sensors SET reachable = false")
    _advance(reports, pid, _unit(), T0 + timedelta(minutes=1))
    with pg_pool.connection() as conn:
        rows = conn.execute("SELECT reachable FROM filament_location_sensors").fetchall()
    assert [r["reachable"] for r in rows] == [True, True]
    assert _ams_readings(pg_pool, pid)[(0, "humidity")] == [29.0]


def test_an_archived_unit_place_hands_its_sensors_to_a_new_one(pg_pool: PgPool) -> None:
    pid, reports = _watched(pg_pool)
    _advance(reports, pid, _unit(), T0)
    with pg_pool.connection() as conn:
        old = LocationStore.ensure_ams_unit_on(conn, UUID(pid), 0)
    LocationStore(pg_pool).update(old, LocationPatch(archived=True))
    _advance(reports, pid, _unit(humidity_raw="31"), T0 + timedelta(minutes=1))
    with pg_pool.connection() as conn:
        new = LocationStore.ensure_ams_unit_on(conn, UUID(pid), 0)
        places = {
            r["location_id"]
            for r in conn.execute(
                "SELECT location_id FROM filament_location_sensors WHERE printer_id = %s",
                (pid,),
            ).fetchall()
        }
    assert new != old
    assert places == {new}
    assert _ams_readings(pg_pool, pid)[(0, "humidity")] == [29.0, 31.0]


def test_an_archived_unit_place_with_no_ams_kind_stores_no_reading(pg_pool: PgPool) -> None:
    """With no AMS kind there is no live place to move to, so the reading is skipped:
    none lands on the hidden place (Decision 18)."""
    pid, reports = _watched(pg_pool)
    _advance(reports, pid, _unit(), T0)
    with pg_pool.connection() as conn:
        old = LocationStore.ensure_ams_unit_on(conn, UUID(pid), 0)
    LocationStore(pg_pool).update(old, LocationPatch(archived=True))
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_location_kinds SET archived_at = now() WHERE ams")
    _advance(reports, pid, _unit(humidity_raw="31", temp="30"), T0 + timedelta(minutes=1))
    assert _ams_readings(pg_pool, pid) == {(0, "humidity"): [29.0], (0, "temperature"): [24.0]}


def test_the_external_holder_makes_no_sensor(pg_pool: PgPool) -> None:
    pid, reports = _watched(pg_pool)
    _advance(reports, pid, _unit(id="255"), T0)
    with pg_pool.connection() as conn:
        count = conn.execute("SELECT count(*) AS n FROM filament_location_sensors").fetchone()
    assert count is not None
    assert count["n"] == 0


def test_a_missing_ams_kind_is_logged_once(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sensors, "_no_ams_kind_logged", False)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_location_kinds SET archived_at = now() WHERE ams")
    pid, reports = _watched(pg_pool)
    with caplog.at_level(logging.WARNING):
        _advance(reports, pid, _unit(), T0)
        _advance(reports, pid, _unit(), T0 + timedelta(minutes=1))
    assert [r.getMessage() for r in caplog.records].count(prints.FILAMENT_CAPTURE_FAILED) == 0
    assert sum("no location kind" in r.getMessage() for r in caplog.records) == 1
    assert _ams_readings(pg_pool, pid) == {}


def test_a_max_hours_value_fits_its_column() -> None:
    assert MaterialCreate(name="x", max_hours_outside_dry=Decimal("99999.9"))
    with pytest.raises(ValueError):
        MaterialCreate(name="x", max_hours_outside_dry=Decimal("99999.95"))


def test_a_patch_cannot_invert_the_stored_band(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool, alert_above=60)
    store = SensorStore(pg_pool)
    with pytest.raises(sensors.SensorBandError, match="alert_below must be under alert_above"):
        store.update(sensor, SensorPatch.model_validate({"alert_below": 80}), actor="ann")
    kept = store.list(None)[0]
    assert (kept.alert_below, kept.alert_above) == (None, 60)
    fixed = store.update(sensor, SensorPatch.model_validate({"alert_below": 40}), actor="ann")
    assert (fixed.alert_below, fixed.alert_above) == (40, 60)
    cleared = store.update(
        sensor, SensorPatch.model_validate({"alert_below": 80, "alert_above": None}), actor="ann"
    )
    assert (cleared.alert_below, cleared.alert_above) == (80, None)


def test_the_table_refuses_an_inverted_band(pg_pool: PgPool) -> None:
    sensor = _sensor(pg_pool)
    with pytest.raises(CheckViolation), pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_location_sensors SET alert_below = 80, alert_above = 60 WHERE id = %s",
            (sensor,),
        )


def test_an_ams_tray_place_is_found_when_every_ams_kind_is_archived(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    store = LocationStore(pg_pool)
    tray = store.ensure_ams(printer, 0, 2, label="T")
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET archived_at = now() WHERE id = %s", (tray.parent_id,)
        )
        conn.execute("UPDATE filament_locations SET parent_id = NULL WHERE id = %s", (tray.id,))
        conn.execute("UPDATE filament_location_kinds SET archived_at = now() WHERE ams")
        again = LocationStore.ensure_ams_on(conn, printer, 0, 2, label="T")
    assert again.id == tray.id


def _location_of(pool: PgPool, sensor: UUID) -> UUID:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT location_id FROM filament_location_sensors WHERE id = %s", (sensor,)
        ).fetchone()
    assert row is not None
    return UUID(str(row["location_id"]))


def test_an_ha_sensor_on_an_archived_place_is_not_read(pg_pool: PgPool) -> None:
    """#6679 item 2: no reading lands on a hidden place, and it is not marked unreachable."""
    sensor = _sensor(pg_pool)
    LocationStore(pg_pool).update(_location_of(pg_pool, sensor), LocationPatch(archived=True))
    with pg_pool.connection() as conn, conn.transaction():
        assert sensors.apply_ha_states_on(conn, {"sensor.box": "40"}, T0) == (0, 0)
        assert sensors.apply_ha_states_on(conn, {}, T0) == (0, 0)
        row = conn.execute(
            "SELECT reachable FROM filament_location_sensors WHERE id = %s", (sensor,)
        ).fetchone()
    assert row is not None and row["reachable"]
    assert SensorStore(pg_pool).readings(sensor, T0 - timedelta(hours=1), 100) == []


def test_a_sensor_on_an_archived_place_is_not_listed(pg_pool: PgPool) -> None:
    """#6679 item 2: the list leaves out a sensor whose place is hidden."""
    hidden = _sensor(pg_pool)
    kinds = CatalogStore(pg_pool).list("location-kinds")
    kind = next(r.id for r in kinds if isinstance(r, LocationKind) and r.name == "Shelf")
    other = LocationStore(pg_pool).create(LocationCreate(name="Cabinet", kind_id=kind)).id
    body = {"location_id": other, "source": "home_assistant", "kind": "humidity"}
    shown = (
        SensorStore(pg_pool)
        .create(SensorIn.model_validate({**body, "entity_id": "sensor.o", "name": "O"}), "ann")
        .id
    )
    place = _location_of(pg_pool, hidden)
    LocationStore(pg_pool).update(place, LocationPatch(archived=True))
    store = SensorStore(pg_pool)
    assert [s.id for s in store.list(None)] == [shown]
    assert store.list(place) == []
