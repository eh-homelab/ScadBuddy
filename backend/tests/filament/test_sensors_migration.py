"""The sensors migration (#6268) keeps one unarchived hand-made place per AMS unit before
it makes the unit index."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from scadbuddy.render.pg_store import MIGRATIONS, migrate

pytestmark = [pytest.mark.requires_postgres, pytest.mark.pg_empty]

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


def test_places_with_no_ams_unit_are_kept_and_duplicates_hand_on_their_contents(
    pg_conninfo: str,
) -> None:
    """A printer's places with no ``ams_id`` (a shelf beside it) never clash in the unit
    index, so none is archived; an archived duplicate's spools and child places move to the
    kept place, the spool with a ``moved`` event."""
    index = next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith(SLUG))
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:index])
        printer = _one(
            conn,
            "INSERT INTO printers (id, serial, name)"
            " VALUES (gen_random_uuid(), 'SNSHELF', 'P') RETURNING id",
        )
        kind = _one(conn, "SELECT id FROM filament_location_kinds LIMIT 1")

        def place(name: str, created: str, ams: int | None, parent: Any = None) -> Any:
            return _one(
                conn,
                "INSERT INTO filament_locations"
                " (name, kind_id, printer_id, ams_id, parent_id, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                name,
                kind,
                printer,
                ams,
                parent,
                created,
            )

        shelf_a = place("Shelf A", "2026-11-01T00:00:00Z", None)
        shelf_b = place("Shelf B", "2026-11-02T00:00:00Z", None)
        kept = place("Unit", "2026-11-01T00:00:00Z", 0)
        dup = place("Unit again", "2026-11-02T00:00:00Z", 0)
        child = place("Box", "2026-11-03T00:00:00Z", None, parent=dup)
        spool = _one(
            conn,
            "INSERT INTO filament_spools (material_id, location_id)"
            " SELECT id, %s FROM filament_materials WHERE name = 'PLA' RETURNING id",
            dup,
        )

        migrate(conn, MIGRATIONS[: index + 1])

        live = {
            row[0]
            for row in conn.execute("SELECT id FROM filament_locations WHERE archived_at IS NULL")
        }
        spool_place = _one(conn, "SELECT location_id FROM filament_spools WHERE id = %s", spool)
        child_parent = _one(conn, "SELECT parent_id FROM filament_locations WHERE id = %s", child)
        moved = conn.execute(
            "SELECT data FROM filament_spool_events WHERE spool_id = %s AND kind = 'moved'",
            (spool,),
        ).fetchall()
    assert {shelf_a, shelf_b, kept, child} <= live
    assert dup not in live
    assert spool_place == kept
    assert child_parent == kept
    assert len(moved) == 1
    assert moved[0][0]["from"] == str(dup)
    assert moved[0][0]["to"] == str(kept)


def test_a_duplicates_moved_event_sorts_after_the_spools_earlier_events(
    pg_conninfo: str,
) -> None:
    """A spool's ledger may hold events stamped later than the migration's clock (a
    report's time); the ``moved`` event is booked after the last of them."""
    index = next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith(SLUG))
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:index])
        printer = _one(
            conn,
            "INSERT INTO printers (id, serial, name)"
            " VALUES (gen_random_uuid(), 'SNLATE', 'P') RETURNING id",
        )
        kind = _one(conn, "SELECT id FROM filament_location_kinds LIMIT 1")

        def place(name: str, created: str) -> Any:
            return _one(
                conn,
                "INSERT INTO filament_locations (name, kind_id, printer_id, ams_id, created_at)"
                " VALUES (%s, %s, %s, 0, %s) RETURNING id",
                name,
                kind,
                printer,
                created,
            )

        place("Unit", "2026-11-01T00:00:00Z")
        dup = place("Unit again", "2026-11-02T00:00:00Z")
        spool = _one(
            conn,
            "INSERT INTO filament_spools (material_id, location_id)"
            " SELECT id, %s FROM filament_materials WHERE name = 'PLA' RETURNING id",
            dup,
        )
        later = _one(
            conn,
            "INSERT INTO filament_spool_events (spool_id, at, kind, actor)"
            " VALUES (%s, now() + interval '1 day', 'weighed', 'ann') RETURNING at",
            spool,
        )

        migrate(conn, MIGRATIONS[: index + 1])

        moved_at = _one(
            conn,
            "SELECT at FROM filament_spool_events WHERE spool_id = %s AND kind = 'moved'",
            spool,
        )
        last = _one(
            conn,
            "SELECT kind FROM filament_spool_events WHERE spool_id = %s"
            " ORDER BY at DESC, seq DESC LIMIT 1",
            spool,
        )
    assert moved_at > later
    assert last == "moved"
