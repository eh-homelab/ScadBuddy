"""#7143 Task 3: linking a spool to a tray, or reconciling one into it, deletes the
tray's row in ``filament_unknown_trays``."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament.inventory import RECONCILE_ACTOR, FilamentInventory
from scadbuddy.filament.unknown_tray_rows import note_on, rows_on
from scadbuddy.spools.models import SeenTray
from tests.conftest import PgPool
from tests.filament.test_tray_reconcile import (
    GREY,
    HT_A,
    PINK,
    _assigned,
    _get,
    _link_bb,
    _moves,
    _printer,
    _report,
    _spool,
    _tray,
)

pytestmark = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _plain_printer(pool: PgPool) -> UUID:
    printer = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, %s, 'Garage')",
            (printer, f"SN{printer.hex[:8]}"),
        )
    return printer


def _red(tray_id: int = 1) -> SeenTray:
    return SeenTray(ams_id=0, tray_id=tray_id, material="PLA", colour="#FF0000FF")


def _cells(pool: PgPool, printer: UUID) -> list[tuple[int, int]]:
    with pool.connection() as conn:
        return [(r.ams_id, r.tray_id) for r in rows_on(conn, printer)]


def test_the_old_tray_link_clears_the_row(pg_pool: PgPool) -> None:
    printer = _plain_printer(pg_pool)
    spool = _spool(pg_pool, "PLA", "#FF0000FF")
    with pg_pool.connection() as conn:
        note_on(conn, printer, _red(), T0)
    assert _cells(pg_pool, printer) == [(0, 1)]

    FilamentInventory(pg_pool).link(printer, 0, 1, spool, _red(), actor="ann")

    assert _cells(pg_pool, printer) == []


def test_link_on_clears_only_its_tray(pg_pool: PgPool) -> None:
    printer = _plain_printer(pg_pool)
    spool = _spool(pg_pool, "PLA", "#FF0000FF")
    with pg_pool.connection() as conn:
        note_on(conn, printer, _red(1), T0)
        note_on(conn, printer, _red(2), T0)

    FilamentInventory(pg_pool).link(printer, 0, 1, spool, _red(1), actor="ann")

    assert _cells(pg_pool, printer) == [(0, 2)]


def test_reconcile_places_an_assigned_spool_into_an_unknown_tray(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    grey = _spool(pg_pool, "PETG", GREY)
    _link_bb(pg_pool, grey, 121)
    tray = _tray("PETG", GREY)
    _report(pg_pool, printer, tray)
    seen = SeenTray(ams_id=HT_A, tray_id=0, material="PETG", rgba=GREY)
    with pg_pool.connection() as conn:
        note_on(conn, printer, seen, T0)

    moved = FilamentInventory(pg_pool).reconcile_trays([_assigned(121)])

    assert [(m.printer_id, m.ams_id, m.tray_id, m.removed, m.placed) for m in moved] == [
        (printer, HT_A, 0, None, grey)
    ]
    assert _cells(pg_pool, printer) == []
    assert _get(pg_pool, grey).location_id is not None
    ((actor, data),) = _moves(pg_pool, grey)
    assert actor == RECONCILE_ACTOR
    assert data["reason"] == "assigned_to_empty_tray"


def test_reconcile_leaves_a_disagreeing_assigned_spool(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    pink = _spool(pg_pool, "PLA", PINK)
    _link_bb(pg_pool, pink, 121)
    _report(pg_pool, printer, _tray("PETG", GREY))
    seen = SeenTray(ams_id=HT_A, tray_id=0, material="PETG", rgba=GREY)
    with pg_pool.connection() as conn:
        note_on(conn, printer, seen, T0)

    moved = FilamentInventory(pg_pool).reconcile_trays([_assigned(121)])

    assert moved == []
    assert _cells(pg_pool, printer) == [(HT_A, 0)]
    assert _get(pg_pool, pink).location_id is None
