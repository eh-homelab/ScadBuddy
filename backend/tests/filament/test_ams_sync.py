"""Sync weights from the AMS (#6268 Task 2): `capture.sync_ams_weights` books the AMS's
remain of every loaded, unlocked spool as a reading that anchors."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from scadbuddy.filament import capture
from scadbuddy.filament import models as m
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.status import STALE_AFTER
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool
from tests.filament.test_capture import (
    T0,
    U1,
    U2,
    _events,
    _printer,
    _report,
    _row,
    _spool,
    _tray,
    _write_report,
)
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

LATER = T0 + timedelta(seconds=5)


def _loaded(pool: PgPool, pid: str, *, remain: int | None = 50) -> UUID:
    """A spool loaded in tray 1 by a first report, whose remain is read once."""
    spool = _spool(pool, tray_uuid=U1)
    with pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET label_weight_g = 1000 WHERE id = %s", (spool,))
        # The spool's own events are stamped by the clock; the test's times are T0's.
        conn.execute(
            "UPDATE filament_spool_events SET at = %s WHERE spool_id = %s",
            (T0 - timedelta(days=1), spool),
        )
    tray = _tray(1, U1) if remain is None else _tray(1, U1, remain=remain)
    _write_report(pool, pid, _report(tray), T0)
    capture.reconcile_all(pool, [pid], now=T0 + timedelta(seconds=1))
    return spool


def _sync(pool: PgPool, at: datetime = LATER) -> m.AmsSyncResult:
    return capture.sync_ams_weights(pool, actor="t", now=at)


def test_sync_books_every_loaded_unlocked_spool(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid)
    result = _sync(pg_pool)
    assert (result.booked, result.spools) == (1, [spool])
    assert result.stale_printers == []
    _, forced = _events(pg_pool, spool, "ams_reading")
    assert forced.at == LATER and forced.ref_id is None and forced.actor == "t"
    assert forced.data == {"remain_pct": 50, "printing": False, "forced": True}


def test_sync_skips_a_locked_spool(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET weight_locked = true WHERE id = %s", (spool,))
    result = _sync(pg_pool)
    assert (result.booked, result.locked, result.spools) == (0, 1, [])
    assert len(_events(pg_pool, spool, "ams_reading")) == 1


def test_sync_skips_a_printing_printer(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid)
    report = _report(_tray(1, U1, remain=40), gcode_state="RUNNING")
    _write_report(pg_pool, pid, report, T0)
    result = _sync(pg_pool, T0 + timedelta(seconds=2))
    assert (result.booked, result.printing) == (0, 1)
    assert len(_events(pg_pool, spool, "ams_reading")) == 1


def test_sync_books_the_idle_printer_and_skips_the_printing_one(pg_pool: PgPool) -> None:
    busy, _ = _printer(pg_pool)
    busy_spool = _loaded(pg_pool, busy)
    idle_spool = _spool(pg_pool, tray_uuid=U2)
    printers = PrinterStore(pg_pool, KEK)
    idle = printers.create(PrinterCreate(serial="01P00A000000002", name="H2D")).id
    printers.link_lan(idle, LanLink(host="10.0.0.8", access_code="12345678"), cert_sha256="cd" * 32)
    _write_report(pg_pool, idle, _report(_tray(2, U2, remain=70)), T0)
    capture.reconcile_all(pg_pool, [idle], now=T0 + timedelta(seconds=1))
    busy_report = _report(_tray(1, U1, remain=40), gcode_state="RUNNING")
    _write_report(pg_pool, busy, busy_report, T0)
    result = _sync(pg_pool, T0 + timedelta(seconds=2))
    assert (result.booked, result.printing, result.spools) == (1, 1, [idle_spool])
    assert len(_events(pg_pool, busy_spool, "ams_reading")) == 1


def test_sync_counts_a_tray_without_remain(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid, remain=None)
    result = _sync(pg_pool)
    assert (result.booked, result.no_remain) == (0, 1)
    assert _events(pg_pool, spool, "ams_reading") == []


def test_sync_skips_a_stale_report(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid)
    result = _sync(pg_pool, T0 + STALE_AFTER + timedelta(seconds=1))
    assert (result.booked, result.stale) == (0, 1)
    assert result.stale_printers == [m.StalePrinter(id=UUID(pid), name="H2C")]
    assert len(_events(pg_pool, spool, "ams_reading")) == 1


def test_sync_counts_a_printer_whose_report_cannot_be_read_as_stale(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid)

    def boom(*args: object) -> None:
        raise ValueError("malformed report")

    monkeypatch.setattr(capture, "_status", boom)
    result = _sync(pg_pool)
    assert (result.booked, result.stale) == (0, 1)
    assert result.stale_printers == [m.StalePrinter(id=UUID(pid), name="H2C")]
    assert len(_events(pg_pool, spool, "ams_reading")) == 1


def test_a_synced_reading_becomes_the_anchor(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid, remain=50)
    FilamentSpoolStore(pg_pool).add_event(
        spool,
        m.SpoolEventAdd(kind="weighed", grams=300.0, at=T0 + timedelta(minutes=1)),
        actor="t",
    )
    assert _row(pg_pool, spool)["grams_left"] == 300.0
    fresh = T0 + timedelta(minutes=2)
    _write_report(pg_pool, pid, _report(_tray(1, U1, remain=50)), fresh)
    _sync(pg_pool, fresh + timedelta(seconds=1))
    assert _row(pg_pool, spool)["grams_left"] == pytest.approx(500.0)


def test_a_reloaded_tray_syncs_only_its_holder(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    old = _loaded(pg_pool, pid)
    new = _spool(pg_pool, tray_uuid=U2)
    _write_report(pg_pool, pid, _report(_tray(1, U2, remain=90)), T0 + timedelta(seconds=2))
    capture.reconcile_all(pg_pool, [pid], now=T0 + timedelta(seconds=3))
    result = _sync(pg_pool, T0 + timedelta(seconds=4))
    assert result.spools == [new]
    assert len(_events(pg_pool, old, "ams_reading")) == 1


def test_a_lock_set_before_the_spool_locks_is_honoured(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid)
    real = capture.lock

    def lock_after_a_weight_lock(conn: capture.Conn, spool_ids: Sequence[UUID]) -> None:
        with pg_pool.connection() as other:
            other.execute("UPDATE filament_spools SET weight_locked = true WHERE id = %s", (spool,))
        real(conn, spool_ids)

    monkeypatch.setattr(capture, "lock", lock_after_a_weight_lock)
    result = _sync(pg_pool)
    assert (result.booked, result.locked) == (0, 1)
    assert len(_events(pg_pool, spool, "ams_reading")) == 1


def test_a_spool_on_a_printer_with_no_report_counts_as_stale(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid)
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM printer_reports WHERE printer_id = %s", (pid,))
    result = _sync(pg_pool)
    assert (result.booked, result.stale) == (0, 1)
    assert result.stale_printers == [m.StalePrinter(id=UUID(pid), name="H2C")]
    assert len(_events(pg_pool, spool, "ams_reading")) == 1


def test_sync_skips_a_spool_locked_between_the_holder_read_and_its_lock(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid)
    real_lock = capture.lock

    def lock_after_a_person_locks_the_weight(conn: capture.Conn, spool_ids: Sequence[UUID]) -> None:
        with pg_pool.connection() as other:
            other.execute("UPDATE filament_spools SET weight_locked = true WHERE id = %s", (spool,))
        real_lock(conn, spool_ids)

    monkeypatch.setattr(capture, "lock", lock_after_a_person_locks_the_weight)
    result = _sync(pg_pool)
    assert (result.booked, result.locked, result.spools) == (0, 1, [])
    assert len(_events(pg_pool, spool, "ams_reading")) == 1


def test_a_spool_on_a_printer_whose_row_is_gone_counts_as_stale(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid)
    monkeypatch.setattr(PrinterStore, "read_on", staticmethod(lambda *args: None))
    result = _sync(pg_pool)
    assert (result.booked, result.stale) == (0, 1)
    assert result.stale_printers == [m.StalePrinter(id=UUID(pid), name=None)]
    assert len(_events(pg_pool, spool, "ams_reading")) == 1


def test_a_spool_placed_between_the_holder_read_and_the_lock_is_left_alone(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, _ = _printer(pg_pool)
    spool = _loaded(pg_pool, pid)
    newcomer: list[UUID] = []
    real_lock = capture.lock

    def lock_after_a_spool_is_placed(conn: capture.Conn, spool_ids: Sequence[UUID]) -> None:
        with pg_pool.connection() as other:
            made = other.execute(
                "INSERT INTO filament_spools (material_id, location_id)"
                " SELECT m.id, s.location_id FROM filament_materials m, filament_spools s"
                " WHERE m.name = 'PLA' AND s.id = %s RETURNING id",
                (spool,),
            ).fetchone()
            assert made is not None
            newcomer.append(made["id"])
        real_lock(conn, spool_ids)

    monkeypatch.setattr(capture, "lock", lock_after_a_spool_is_placed)
    result = _sync(pg_pool)
    assert (result.booked, result.spools) == (1, [spool])
    assert result.stale_printers == []
    assert _events(pg_pool, newcomer[0], "ams_reading") == []


def test_a_printer_with_no_report_and_no_spool_is_not_named_stale(pg_pool: PgPool) -> None:
    """The sync never visits this printer (it has no report row), so the visit filter, not
    `_stale`, keeps it out of `stale_printers`."""
    _printer(pg_pool)
    result = _sync(pg_pool, T0 + STALE_AFTER + timedelta(seconds=1))
    assert (result.booked, result.stale) == (0, 0)
    assert result.stale_printers == []


def test_a_printer_with_a_stale_report_and_no_spool_is_not_named_stale(pg_pool: PgPool) -> None:
    """The sync visits this printer (it has a report row), finds the report stale and
    holds no spool, so `_stale`'s `if not holders` branch keeps it out of `stale_printers`."""
    pid, _ = _printer(pg_pool)
    _write_report(pg_pool, pid, _report(_tray(1, U1)), T0)
    result = _sync(pg_pool, T0 + STALE_AFTER + timedelta(seconds=1))
    assert (result.booked, result.stale) == (0, 0)
    assert result.stale_printers == []
