"""#7167: the migration that adds a spool's home dry box and the `non_print_waste` event."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from scadbuddy.render.pg_store import MIGRATIONS, MIGRATIONS_DIR, migrate

pytestmark = [pytest.mark.requires_postgres, pytest.mark.pg_empty]

MIGRATION = MIGRATIONS_DIR / "20261127T0754Z_filament_dry_boxes.sql"

Conn = psycopg.Connection[dict[str, Any]]


def _before(conn: Conn) -> None:
    before = next(i for i, m in enumerate(MIGRATIONS) if m.id == MIGRATION.stem)
    migrate(conn, MIGRATIONS[:before])


def _apply(conn: Conn) -> None:
    conn.execute(MIGRATION.read_text())


def _one(conn: Conn, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return next(iter(row.values()))


def _spool(conn: Conn) -> UUID:
    material: UUID = _one(conn, "SELECT id FROM filament_materials WHERE name = 'PLA'")
    spool: UUID = _one(
        conn, "INSERT INTO filament_spools (material_id) VALUES (%s) RETURNING id", (material,)
    )
    return spool


def _place(conn: Conn, name: str, kind: str, capacity: int | None, archived: bool = False) -> UUID:
    place: UUID = _one(
        conn,
        "INSERT INTO filament_locations (name, kind_id, capacity, archived_at)"
        " SELECT %s, id, %s, CASE WHEN %s THEN now() END FROM filament_location_kinds"
        " WHERE name = %s RETURNING id",
        (name, capacity, archived, kind),
    )
    return place


def _moved(conn: Conn, spool: UUID, to: UUID | None, at: str) -> None:
    conn.execute(
        "INSERT INTO filament_spool_events (spool_id, kind, at, data)"
        " VALUES (%s, 'moved', %s::timestamptz, %s::jsonb)",
        (spool, at, json.dumps({"to": None if to is None else str(to)})),
    )


def _home(conn: Conn, spool: UUID) -> UUID | None:
    home: UUID | None = _one(
        conn, "SELECT home_location_id FROM filament_spools WHERE id = %s", (spool,)
    )
    return home


def _event(conn: Conn, spool: UUID, kind: str) -> None:
    conn.execute(
        "INSERT INTO filament_spool_events (spool_id, kind) VALUES (%s, %s)", (spool, kind)
    )


def test_migration_adds_home_columns_and_waste_kind(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before(conn)
        spool = _spool(conn)
        _apply(conn)
        cols = {
            r["column_name"]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'filament_spools'"
            )
        }
        assert {"home_location_id", "home_asked_at"} <= cols
        _event(conn, spool, "non_print_waste")
        with pytest.raises(psycopg.errors.CheckViolation):
            _event(conn, spool, "bogus")


def test_deleting_the_home_box_clears_the_home(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before(conn)
        _apply(conn)
        spool = _spool(conn)
        box = _place(conn, "Box 1", "Dry box", 1)
        conn.execute("UPDATE filament_spools SET home_location_id = %s", (box,))
        conn.execute("DELETE FROM filament_locations WHERE id = %s", (box,))
        assert _home(conn, spool) is None


def test_home_backfilled_from_moved_events(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before(conn)
        box = _place(conn, "Box 1", "Dry box", 1)
        big = _place(conn, "Big box", "Dry box", 4)
        gone = _place(conn, "Old box", "Dry box", 1, archived=True)
        shelf = _place(conn, "Shelf 1", "Shelf", 1)
        in_box, to_shelf, in_big, in_gone, out_again = (_spool(conn) for _ in range(5))
        _moved(conn, in_box, shelf, "2026-11-01T00:00:00Z")
        _moved(conn, in_box, box, "2026-11-02T00:00:00Z")
        _moved(conn, to_shelf, box, "2026-11-01T00:00:00Z")
        _moved(conn, to_shelf, shelf, "2026-11-02T00:00:00Z")
        _moved(conn, in_big, big, "2026-11-01T00:00:00Z")
        _moved(conn, in_gone, gone, "2026-11-01T00:00:00Z")
        _moved(conn, out_again, box, "2026-11-01T00:00:00Z")
        _moved(conn, out_again, None, "2026-11-02T00:00:00Z")
        _apply(conn)
        assert _home(conn, in_box) == box
        assert _home(conn, to_shelf) is None
        assert _home(conn, in_big) is None
        assert _home(conn, in_gone) is None
        assert _home(conn, out_again) is None
