"""Location sensors (#6268): a humidity or temperature sensor on a place, from Home
Assistant (through Bambuddy) or an AMS unit's own report. A reading is stored when the
value changed, or when `SAME_VALUE_EVERY` has passed since the last stored one; the last
stored reading is kept on the sensor row for reads."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.trays import EXTERNAL
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogRefError

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]

_Readings = list[m.SensorReading]

SAME_VALUE_EVERY = timedelta(minutes=15)

_COLUMNS = ", ".join(n for n in m.Sensor.model_fields if n != "alerting")


class SensorExistsError(ValueError):
    """An unarchived sensor of that kind is already on that AMS unit."""

    def __init__(self) -> None:
        super().__init__("a sensor of that kind already exists on that AMS unit")


class SensorStore:
    """The sensor tables over a pool whose connections autocommit and return dict rows
    (the settings store's). Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    def list(self, location_id: UUID | None) -> list[m.Sensor]:
        """The unarchived sensors, of one place or of all, by name."""
        where, args = ("", []) if location_id is None else (" AND location_id = %s", [location_id])
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM filament_location_sensors WHERE archived_at IS NULL"
                f"{where} ORDER BY upper(name), id",
                args,
            ).fetchall()
        return [m.Sensor.model_validate(row) for row in rows]

    def create(self, body: m.SensorIn, actor: str) -> m.Sensor:
        """``actor`` is who asked; sensors keep no author, so it is not stored."""
        del actor
        data = body.model_dump()
        names = list(data)
        try:
            with self._pool.connection() as conn:
                self._live_place(conn, body.location_id)
                row = conn.execute(
                    f"INSERT INTO filament_location_sensors ({', '.join(names)})"
                    f" VALUES ({', '.join('%s' for _ in names)}) RETURNING {_COLUMNS}",
                    [data[n] for n in names],
                ).fetchone()
        except errors.UniqueViolation:
            raise SensorExistsError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        assert row is not None
        return m.Sensor.model_validate(row)

    def update(self, sensor_id: UUID, patch: m.SensorPatch, actor: str) -> m.Sensor:
        """The fields ``patch`` sends. `LookupError` for no such (unarchived) sensor."""
        del actor
        sent = [n for n in type(patch).model_fields if n in patch.model_fields_set]
        sets = ", ".join([*(f"{n} = %s" for n in sent), "updated_at = now()"])
        values: list[Any] = [getattr(patch, n) for n in sent]
        try:
            with self._pool.connection() as conn:
                if patch.location_id is not None:
                    self._live_place(conn, patch.location_id)
                row = conn.execute(
                    f"UPDATE filament_location_sensors SET {sets}"
                    f" WHERE id = %s AND archived_at IS NULL RETURNING {_COLUMNS}",
                    [*values, sensor_id],
                ).fetchone()
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        if row is None:
            raise LookupError(f"no sensor {sensor_id}")
        return m.Sensor.model_validate(row)

    def archive(self, sensor_id: UUID, actor: str) -> None:
        """`LookupError` for no such (unarchived) sensor."""
        del actor
        with self._pool.connection() as conn:
            done = conn.execute(
                "UPDATE filament_location_sensors SET archived_at = now(), updated_at = now()"
                " WHERE id = %s AND archived_at IS NULL RETURNING id",
                (sensor_id,),
            ).fetchone()
        if done is None:
            raise LookupError(f"no sensor {sensor_id}")

    def readings(self, sensor_id: UUID, since: datetime, limit: int) -> _Readings:
        """The stored readings from ``since`` on, oldest first, at most ``limit``."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT at, value FROM filament_sensor_readings"
                " WHERE sensor_id = %s AND at >= %s ORDER BY at LIMIT %s",
                (sensor_id, since, limit),
            ).fetchall()
        return [m.SensorReading.model_validate(row) for row in rows]

    @staticmethod
    def _live_place(conn: Conn, location_id: UUID) -> None:
        if (
            conn.execute(
                "SELECT 1 FROM filament_locations WHERE id = %s AND archived_at IS NULL",
                (location_id,),
            ).fetchone()
            is None
        ):
            raise CatalogRefError()

    @staticmethod
    def record_on(conn: Conn, sensor_id: UUID, value: float, at: datetime) -> bool:
        """Store ``value`` read at ``at`` if it differs from the last stored value or
        `SAME_VALUE_EVERY` has passed since it. Call it inside ``conn.transaction()``: the
        sensor row's lock (``FOR UPDATE``) holds only to the end of a transaction, and the
        reading and the row's last value must change together. Nothing is stored for a
        value that is not finite, or an ``at`` not newer than the last stored reading (a
        poll that arrived out of order). A stored or repeated value marks the sensor
        reachable. ``False`` when nothing was stored (or the sensor is gone)."""
        if not math.isfinite(value):
            return False
        row = conn.execute(
            "SELECT last_value, last_at FROM filament_location_sensors WHERE id = %s FOR UPDATE",
            (sensor_id,),
        ).fetchone()
        if row is None:
            return False
        last_at: datetime | None = row["last_at"]
        if last_at is not None and at <= last_at:
            return False
        if row["last_value"] == value and last_at is not None and at - last_at < SAME_VALUE_EVERY:
            conn.execute(
                "UPDATE filament_location_sensors SET reachable = true WHERE id = %s",
                (sensor_id,),
            )
            return False
        conn.execute(
            "INSERT INTO filament_sensor_readings (sensor_id, at, value) VALUES (%s, %s, %s)",
            (sensor_id, at, value),
        )
        conn.execute(
            "UPDATE filament_location_sensors SET reachable = true, last_value = %s,"
            " last_at = %s WHERE id = %s",
            (value, at, sensor_id),
        )
        return True


def capture_ams_on(conn: Conn, printer_id: str, report: dict[str, Any], seen_at: datetime) -> None:
    """Record each AMS unit's humidity and temperature from ``report`` as readings, on
    ``conn`` and in the caller's transaction. A unit's place and its sensors are made
    when a number first shows; a reading the report lacks (``""``) writes nothing."""
    from scadbuddy.filament.locations import LocationStore, NoAmsKindError  # imports this
    from scadbuddy.printers.status import ams_units

    units = [u for u in ams_units(report) if u.id != EXTERNAL]
    if not any(u.humidity is not None or u.temp is not None for u in units):
        return
    printer = UUID(printer_id)
    # One read of the printer's sensors: a report that changes nothing costs only this.
    known = {
        (row["ams_id"], row["kind"]): row
        for row in conn.execute(
            "SELECT id, ams_id, kind, last_value, last_at, reachable"
            " FROM filament_location_sensors"
            " WHERE source = 'ams' AND printer_id = %s AND archived_at IS NULL",
            (printer,),
        ).fetchall()
    }
    for unit in units:
        for kind, value, symbol in (
            ("humidity", unit.humidity, "%"),
            ("temperature", unit.temp, "°C"),
        ):
            if value is None:
                continue
            row = known.get((unit.id, kind))
            if row is None:
                try:
                    place = LocationStore.ensure_ams_unit_on(conn, printer, unit.id)
                except NoAmsKindError:
                    _log_no_ams_kind()
                    break
                made = conn.execute(
                    "INSERT INTO filament_location_sensors"
                    " (location_id, source, kind, printer_id, ams_id, name, unit)"
                    " VALUES (%s, 'ams', %s, %s, %s, %s, %s)"
                    " ON CONFLICT (printer_id, ams_id, kind)"
                    " WHERE source = 'ams' AND archived_at IS NULL DO NOTHING RETURNING id",
                    (place, kind, printer, unit.id, f"AMS {unit.id} {kind}", symbol),
                ).fetchone()
                if made is not None:
                    SensorStore.record_on(conn, made["id"], float(value), seen_at)
            elif _due(row, float(value), seen_at):
                SensorStore.record_on(conn, row["id"], float(value), seen_at)


_no_ams_kind_logged = False


def _log_no_ams_kind() -> None:
    """Say once per process that AMS sensors wait for an ``ams`` location kind."""
    global _no_ams_kind_logged
    if not _no_ams_kind_logged:
        _no_ams_kind_logged = True
        logger.warning("no location kind is marked as an AMS; AMS sensors are not recorded")


def _due(row: dict[str, Any], value: float, at: datetime) -> bool:
    """Whether `SensorStore.record_on` has something to do for ``value`` read at ``at``,
    from the sensor row already read: a first reading, a changed value, `SAME_VALUE_EVERY`
    since the last, or an unreachable sensor to mark reachable."""
    last_at: datetime | None = row["last_at"]
    return (
        last_at is None
        or not row["reachable"]
        or row["last_value"] != value
        or at - last_at >= SAME_VALUE_EVERY
    )


def repoint_printer_sensors(conn: Conn, merged: UUID, into: UUID) -> None:
    """Make printer ``merged``'s AMS sensors printer ``into``'s, on ``conn`` and in the
    printer merge's transaction (`printers/store.py` `_merge`). Where ``into`` has an
    unarchived sensor for the same unit and kind, ``into``'s stands and the gone one is
    archived, with its readings."""
    conn.execute(
        "UPDATE filament_location_sensors g SET archived_at = now(), updated_at = now()"
        " WHERE g.printer_id = %s AND g.archived_at IS NULL AND EXISTS ("
        " SELECT 1 FROM filament_location_sensors k WHERE k.printer_id = %s"
        " AND k.ams_id = g.ams_id AND k.kind = g.kind AND k.source = g.source"
        " AND k.archived_at IS NULL)",
        (merged, into),
    )
    conn.execute(
        "UPDATE filament_location_sensors SET printer_id = %s, updated_at = now()"
        " WHERE printer_id = %s",
        (into, merged),
    )
