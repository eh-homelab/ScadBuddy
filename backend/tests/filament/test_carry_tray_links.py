"""The tray-link carry (#6262 Task 12, F8, F30): each person's link of an untagged tray to
a spool (`tray_spool_links`, the retired overlay's) becomes the SB spool's place in that
tray, with the old ``MATERIAL|RRGGBB`` fingerprint as ``MATERIAL|#RRGGBBFF`` and one
``moved`` event. A spool already in a tray, or a tray another spool already holds, is
left as it is. After it no code reads the overlay's tables; they stay for the follow-up
PR that drops them (plan Decision 6)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

import scadbuddy
from scadbuddy.render.pg_store import MIGRATIONS, migrate

pytestmark = pytest.mark.requires_postgres

Conn = psycopg.Connection[tuple[Any, ...]]

SLUG = "_filament_carry_tray_links"
#: The retired overlay's and native spools' tables, kept unread until the drop PR.
OLD_TABLES = (
    "native_spools",
    "native_spool_uses",
    "physical_spools",
    "spool_provider_records",
    "spool_record_links",
    "spool_provider_refreshes",
    "tray_spool_links",
)
PRINTER = "00000000-0000-0000-0000-0000000000a1"


def _before(conn: Conn) -> None:
    index = next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith(SLUG))
    migrate(conn, MIGRATIONS[:index])
    conn.execute("INSERT INTO printers (id, serial, name) VALUES (%s, 'S1', 'Garage')", (PRINTER,))


def _one(conn: Conn, sql: str, *args: object) -> Any:
    row = conn.execute(sql, args).fetchone()
    assert row is not None
    return row[0]


def _spool(conn: Conn, number: int) -> Any:
    return _one(
        conn,
        "INSERT INTO filament_spools (number, material_id) OVERRIDING SYSTEM VALUE"
        " SELECT %s, id FROM filament_materials WHERE name = 'PLA' RETURNING id",
        number,
    )


def _tray_link(conn: Conn, tray: int, physical: Any, fingerprint: str, ams: int = 0) -> None:
    conn.execute("INSERT INTO physical_spools (id) VALUES (%s) ON CONFLICT DO NOTHING", (physical,))
    conn.execute(
        "INSERT INTO tray_spool_links (printer_id, ams_id, tray_id, physical_spool_id,"
        " fingerprint) VALUES (%s, %s, %s, %s, %s)",
        (PRINTER, ams, tray, physical, fingerprint),
    )


def _place(conn: Conn, spool: Any) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT l.printer_id::text, l.ams_id, l.tray_id, s.tray_fingerprint"
        " FROM filament_spools s JOIN filament_locations l ON l.id = s.location_id"
        " WHERE s.id = %s",
        (spool,),
    ).fetchone()


def _moves(conn: Conn, spool: Any) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT actor, data->>'from', (data->>'to')::uuid = s.location_id"
        " FROM filament_spool_events e JOIN filament_spools s ON s.id = e.spool_id"
        " WHERE e.spool_id = %s AND e.kind = 'moved'",
        (spool,),
    ).fetchall()


def test_a_tray_link_becomes_a_tray_place(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        spool = _spool(conn, 1)
        _tray_link(conn, 1, spool, "PLA|FF0000")

        migrate(conn)

        assert _place(conn, spool) == (PRINTER, 0, 1, "PLA|#FF0000FF")
        assert _moves(conn, spool) == [("system:migration", None, True)]
        kind = _one(
            conn,
            "SELECT k.ams FROM filament_spools s JOIN filament_locations l ON l.id = s.location_id"
            " JOIN filament_location_kinds k ON k.id = l.kind_id WHERE s.id = %s",
            spool,
        )
        assert kind is True


def test_an_empty_colour_link_carries(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        spool = _spool(conn, 1)
        _tray_link(conn, 2, spool, "PLA|")

        migrate(conn)

        assert _place(conn, spool) == (PRINTER, 0, 2, "PLA|")


def test_a_bb_only_tray_link_finds_its_pulled_spool(pg_conninfo: str) -> None:
    """A physical spool known only to Bambuddy: the pull made its SB spool with a new id,
    linked to the BB record the physical spool's record link names."""
    physical = "00000000-0000-0000-0000-0000000000b7"
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        pulled = _spool(conn, 7)
        _tray_link(conn, 0, physical, "PETG|00FF00")
        conn.execute(
            "INSERT INTO spool_record_links (provider, record_id, physical_spool_id, linked_by)"
            " VALUES ('bambuddy', '42', %s, 'manual')",
            (physical,),
        )
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id) VALUES (%s, 42)", (pulled,)
        )

        migrate(conn)

        assert _place(conn, pulled) == (PRINTER, 0, 0, "PETG|#00FF00FF")
        assert len(_moves(conn, pulled)) == 1


def test_the_carry_skips_a_taken_tray(pg_conninfo: str) -> None:
    """A tray another spool already holds keeps it; a spool already in a tray stays
    there; a link that names no SB spool, or a fingerprint of no known shape, is
    skipped, never an error."""
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        holder, linked, placed, odd = (_spool(conn, n) for n in (1, 2, 3, 4))
        kind = _one(conn, "SELECT id FROM filament_location_kinds WHERE ams LIMIT 1")
        taken = _one(
            conn,
            "INSERT INTO filament_locations (name, kind_id, printer_id, ams_id, tray_id)"
            " VALUES ('Garage AMS-A slot 1', %s, %s, 0, 0) RETURNING id",
            kind,
            PRINTER,
        )
        other = _one(
            conn,
            "INSERT INTO filament_locations (name, kind_id, printer_id, ams_id, tray_id)"
            " VALUES ('Garage AMS-A slot 4', %s, %s, 0, 3) RETURNING id",
            kind,
            PRINTER,
        )
        conn.execute("UPDATE filament_spools SET location_id = %s WHERE id = %s", (taken, holder))
        conn.execute("UPDATE filament_spools SET location_id = %s WHERE id = %s", (other, placed))
        _tray_link(conn, 0, linked, "PLA|FF0000")  # tray 0 is the holder's
        _tray_link(conn, 1, placed, "PLA|00FF00")  # already in tray 3
        _tray_link(conn, 2, "00000000-0000-0000-0000-0000000000ee", "PLA|0000FF")  # no SB spool
        _tray_link(conn, 0, odd, "PLA|12", ams=1)  # not RRGGBB

        migrate(conn)

        assert _place(conn, holder) == (PRINTER, 0, 0, None)
        assert _place(conn, linked) is None
        assert _place(conn, placed) == (PRINTER, 0, 3, None)
        assert _place(conn, odd) is None
        assert _one(conn, "SELECT count(*) FROM filament_spool_events WHERE kind = 'moved'") == 0
        # Nothing is dropped (Decision 6).
        assert _one(conn, "SELECT count(*) FROM tray_spool_links") == 4


def test_two_links_of_one_spool_place_it_once(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        spool = _spool(conn, 1)
        _tray_link(conn, 0, spool, "PLA|FF0000")
        _tray_link(conn, 1, spool, "PLA|FF0000")

        migrate(conn)

        assert _place(conn, spool) is not None
        assert len(_moves(conn, spool)) == 1


def test_no_code_names_the_old_tables() -> None:
    """Nothing under ``backend/scadbuddy`` but the migrations reads or writes them."""
    root = Path(scadbuddy.__file__).parent
    pattern = re.compile(r"\b(" + "|".join(OLD_TABLES) + r")\b")
    found = [
        f"{path.relative_to(root)}: {match.group(1)}"
        for path in sorted(root.rglob("*.py"))
        for match in pattern.finditer(path.read_text())
    ]
    assert found == []
