"""#7143 Task 1: the rows of the unknown trays and the block's text."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from scadbuddy.filament.unknown_tray_rows import (
    PoolUnknownTrays,
    UnknownSpoolRef,
    blocked_detail,
    clear_on,
    note_on,
    rows_on,
)
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools.models import SeenTray
from tests.conftest import PgPool
from tests.printers.conftest import KEK
from tests.support.printers import MERGED_BY

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"
T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
A = UUID("00000000-0000-0000-0000-00000000000a")
B = UUID("00000000-0000-0000-0000-00000000000b")
C = UUID("00000000-0000-0000-0000-00000000000c")


def _printer(pool: PgPool, serial: str = SERIAL, name: str = "H2C") -> UUID:
    printers = PrinterStore(pool, KEK)
    row = printers.create(PrinterCreate(serial=serial, name=name))
    printers.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    return UUID(row.id)


def _seen(tray_id: int = 1, ams_id: int = 0, rgba: str = "#FF0000FF") -> SeenTray:
    return SeenTray(ams_id=ams_id, tray_id=tray_id, material="PLA", colour="#FF0000", rgba=rgba)


def test_note_inserts_then_keeps_seen_at_while_nothing_changes(pg_pool: PgPool) -> None:
    pid = _printer(pg_pool)
    with pg_pool.connection() as conn:
        first = note_on(conn, pid, _seen(), T0)
        again = note_on(conn, pid, _seen(), T0 + timedelta(seconds=5))
        rows = rows_on(conn, pid)
    assert [r.id for r in rows] == [first.id]
    assert again.id == first.id
    assert first.first_seen_at == first.seen_at == T0
    assert again.first_seen_at == again.seen_at == T0


def test_a_changed_colour_moves_seen_at_only(pg_pool: PgPool) -> None:
    pid = _printer(pg_pool)
    t1 = T0 + timedelta(seconds=30)
    with pg_pool.connection() as conn:
        red = note_on(conn, pid, _seen(rgba="#FF0000FF"), T0)
        blue = note_on(conn, pid, _seen(rgba="#0000FFFF"), t1)
    assert blue.id == red.id
    assert blue.first_seen_at == T0
    assert blue.seen_at == t1
    assert blue.colour_rgba == "#0000FFFF"


def test_clear_deletes_one_tray_only(pg_pool: PgPool) -> None:
    pid = _printer(pg_pool)
    with pg_pool.connection() as conn:
        note_on(conn, pid, _seen(1), T0)
        note_on(conn, pid, _seen(2), T0)
        assert clear_on(conn, pid, 0, 1) is True
        assert clear_on(conn, pid, 0, 1) is False
        assert [(r.ams_id, r.tray_id) for r in rows_on(conn, pid)] == [(0, 2)]


def test_deleting_the_printer_deletes_its_rows(pg_pool: PgPool) -> None:
    pid = _printer(pg_pool)
    with pg_pool.connection() as conn:
        note_on(conn, pid, _seen(), T0)
        conn.execute("DELETE FROM printers WHERE id = %s", (pid,))
        assert rows_on(conn, pid) == []


def test_merging_a_printer_drops_its_rows(pg_pool: PgPool) -> None:
    printers = PrinterStore(pg_pool, KEK)
    synthetic = UUID(
        printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C", by=MERGED_BY).id
    )
    mine = _printer(pg_pool, name="Mine")
    with pg_pool.connection() as conn:
        note_on(conn, synthetic, _seen(1), T0)
        note_on(conn, mine, _seen(2), T0)
    printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C", by=MERGED_BY)
    with pg_pool.connection() as conn:
        assert [(r.printer_id, r.tray_id) for r in rows_on(conn)] == [(mine, 2)]


def test_blocked_detail_names_every_slot() -> None:
    assert blocked_detail("H2C", [UnknownSpoolRef(id=A, slot="AMS-A slot 2")]) == (
        "H2C has an unknown spool in AMS-A slot 2: link it on the Filament page first."
    )
    refs = [
        UnknownSpoolRef(id=A, slot="AMS-A slot 1"),
        UnknownSpoolRef(id=B, slot="AMS-A slot 2"),
        UnknownSpoolRef(id=C, slot="HT-A"),
    ]
    assert blocked_detail("H2C", refs) == (
        "H2C has an unknown spool in AMS-A slot 1, AMS-A slot 2 and HT-A:"
        " link it on the Filament page first."
    )


async def test_pool_blocking_lists_slots(pg_pool: PgPool) -> None:
    pid = _printer(pg_pool)
    with pg_pool.connection() as conn:
        row = note_on(conn, pid, _seen(0, ams_id=128), T0)
    assert await PoolUnknownTrays(pg_pool).blocking(str(pid)) == [
        UnknownSpoolRef(id=row.id, slot="HT-A")
    ]
