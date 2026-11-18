"""The sensors migration (#6268) keeps one unarchived hand-made place per AMS unit before
it makes the unit index."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from scadbuddy.render.pg_store import MIGRATIONS, migrate

pytestmark = pytest.mark.requires_postgres

Conn = psycopg.Connection[tuple[Any, ...]]

SLUG = "_filament_sensors"


def _one(conn: Conn, sql: str, *args: object) -> Any:
    row = conn.execute(sql, args).fetchone()
    assert row is not None
    return row[0]


def test_duplicate_unit_places_are_archived_but_the_oldest(pg_conninfo: str) -> None:
    index = next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith(SLUG))
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:index])
        printer = _one(
            conn,
            "INSERT INTO printers (id, serial, name)"
            " VALUES (gen_random_uuid(), 'SNDUP', 'P') RETURNING id",
        )
        kind = _one(conn, "SELECT id FROM filament_location_kinds LIMIT 1")

        def place(name: str, created: str, ams: int = 0) -> Any:
            return _one(
                conn,
                "INSERT INTO filament_locations (name, kind_id, printer_id, ams_id, created_at)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                name,
                kind,
                printer,
                ams,
                created,
            )

        newer = place("B", "2026-11-02T00:00:00Z")
        oldest = place("A", "2026-11-01T00:00:00Z")
        other = place("C", "2026-11-03T00:00:00Z", ams=1)

        migrate(conn, MIGRATIONS[: index + 1])

        live = {
            row[0]
            for row in conn.execute("SELECT id FROM filament_locations WHERE archived_at IS NULL")
        }
    assert oldest in live
    assert other in live
    assert newer not in live
