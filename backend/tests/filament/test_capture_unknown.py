"""#7143 Task 2: capture records the trays no spool holds, and makes a spool for an RFID tray."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from scadbuddy.filament import bambuddy_pull, capture, prints
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.models import Material, SpoolTypeCreate, TypePreset
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.filament.unknown_tray_rows import UnknownTrayRow, rows_on
from scadbuddy.printers.reports import PrinterReportStore
from tests.conftest import PgPool
from tests.filament.test_capture import (
    T0,
    U1,
    _events,
    _link,
    _printer,
    _report,
    _spool,
    _tray,
    _tray_place,
    _write_report,
)

pytestmark = pytest.mark.requires_postgres


def _advance(pool: PgPool, pid: str, report: dict[str, Any], at: Any = T0) -> None:
    with pool.connection() as conn, conn.transaction():
        PrinterReportStore(pool).advance_in(conn, pid, report, at)


def _rows(pool: PgPool, pid: str) -> list[UnknownTrayRow]:
    with pool.connection() as conn:
        return rows_on(conn, UUID(pid))


def _material(pool: PgPool) -> UUID:
    return next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )


def _type(pool: PgPool, code: str, rgba: str = "#FF0000FF", preset: str | None = None) -> UUID:
    store = SpoolTypeStore(pool)
    made = store.create(
        SpoolTypeCreate(code=code, material_id=_material(pool), rgba=rgba, label_weight_g=1000)
    )
    if preset is not None:
        row = TypePreset(
            type_id=made.id,
            printer_model="H2C",
            nozzle_diameter="0.4",
            slicer_filament=preset,
            slicer_filament_name=None,
        )
        store.set_presets(made.id, [row], actor="t")
    return made.id


def _spools(pool: PgPool) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, type_id, material_id, rgba, location_id FROM filament_spools"
                " ORDER BY number"
            ).fetchall()
        ]


def test_an_untagged_tray_with_no_holder_gets_a_row(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    _advance(pg_pool, pid, _report(_tray(2)))
    (row,) = _rows(pg_pool, pid)
    assert (row.ams_id, row.tray_id) == (0, 2)
    assert row.material == "PLA" and row.colour_rgba == "#FF0000FF" and row.remain_pct is None


def test_a_linked_tray_gets_no_row(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    _link(pg_pool, pid, _spool(pg_pool))
    _advance(pg_pool, pid, _report(_tray(1)))
    assert _rows(pg_pool, pid) == []


def test_emptying_the_tray_clears_its_row(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    _advance(pg_pool, pid, _report(_tray(2)))
    assert len(_rows(pg_pool, pid)) == 1
    _advance(
        pg_pool,
        pid,
        _report(_tray(2, tray_type="", state=0)),
        T0 + timedelta(minutes=1),
    )
    assert _rows(pg_pool, pid) == []


def test_a_loading_tray_keeps_its_row(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    _advance(pg_pool, pid, _report(_tray(2)))
    (before,) = _rows(pg_pool, pid)
    _advance(pg_pool, pid, _report(_tray(2, state=5)), T0 + timedelta(minutes=1))
    (after,) = _rows(pg_pool, pid)
    assert after.id == before.id


def test_a_unit_the_report_omits_keeps_its_rows(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    _advance(pg_pool, pid, _report(_tray(2)))
    _advance(pg_pool, pid, _report(_tray(1), unit="1"), T0 + timedelta(minutes=1))
    rows = [(r.ams_id, r.tray_id) for r in _rows(pg_pool, pid)]
    assert (0, 2) in rows


def test_a_tray_that_flips_known_and_back_gets_a_new_row(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    _advance(pg_pool, pid, _report(_tray(1)))
    (first,) = _rows(pg_pool, pid)
    _link(pg_pool, pid, _spool(pg_pool))
    # `link` deletes the row itself.
    assert _rows(pg_pool, pid) == []
    FilamentInventory(pg_pool).unlink(UUID(pid), 0, 1, actor="t")
    t2 = T0 + timedelta(minutes=2)
    _advance(pg_pool, pid, _report(_tray(1, tray_color="00FF00FF")), t2)
    (second,) = _rows(pg_pool, pid)
    assert second.id != first.id and second.first_seen_at == t2


def test_an_external_holder_gets_no_row(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    report = {"vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "FF0000FF", "state": 11}}
    _advance(pg_pool, pid, report)
    assert _rows(pg_pool, pid) == []


def test_an_rfid_tray_gets_a_spool_of_the_matching_type(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    kind = _type(pg_pool, "RED", preset="GFA00")
    _advance(pg_pool, pid, _report(_tray(1, U1, tray_info_idx="GFA00", remain=80)))
    (spool,) = _spools(pg_pool)
    assert spool["type_id"] == kind
    assert spool["location_id"] == _tray_place(pg_pool, pid)
    with pg_pool.connection() as conn:
        binding = conn.execute(
            "SELECT 1 FROM filament_tag_bindings b JOIN filament_tags t ON t.id = b.tag_id"
            " WHERE b.spool_id = %s AND b.unbound_at IS NULL AND t.tray_uuid = %s",
            (spool["id"], U1),
        ).fetchone()
    assert binding is not None
    kinds = [e.kind for e in _events(pg_pool, spool["id"])]
    for wanted in ("created", "tag_bound", "loaded", "moved", "ams_reading"):
        assert wanted in kinds
    (created,) = _events(pg_pool, spool["id"], "created")
    assert created.data["by"] == capture.RFID_MADE
    (reading,) = _events(pg_pool, spool["id"], "ams_reading")
    assert reading.data["remain_pct"] == 80
    assert _rows(pg_pool, pid) == []


def test_an_rfid_tray_with_no_type_gets_a_typeless_spool(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    _advance(pg_pool, pid, _report(_tray(1, U1)))
    (spool,) = _spools(pg_pool)
    assert spool["type_id"] is None
    assert spool["material_id"] == _material(pg_pool)
    assert spool["rgba"] == "#FF0000FF"


def test_an_archived_spools_tag_goes_to_the_new_spool(pg_pool: PgPool) -> None:
    old = _spool(pg_pool, tray_uuid=U1)
    from scadbuddy.filament.spools import FilamentSpoolStore

    FilamentSpoolStore(pg_pool).archive(old, True, actor="t")
    pid, _ = _printer(pg_pool)
    _advance(pg_pool, pid, _report(_tray(1, U1)))
    spools = _spools(pg_pool)
    assert len(spools) == 2
    new = spools[1]["id"]
    with pg_pool.connection() as conn:
        open_ = conn.execute(
            "SELECT spool_id FROM filament_tag_bindings WHERE unbound_at IS NULL"
        ).fetchall()
        closed = conn.execute(
            "SELECT reason FROM filament_tag_bindings WHERE spool_id = %s"
            " AND unbound_at IS NOT NULL",
            (old,),
        ).fetchall()
    assert [r["spool_id"] for r in open_] == [new]
    # `archive` closed the old binding, so the new spool took a free tag.
    assert [r["reason"] for r in closed] == ["archived"]


def test_the_pull_links_the_bb_spool_to_the_rfid_spool(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    _advance(pg_pool, pid, _report(_tray(1, U1)))
    (spool,) = _spools(pg_pool)
    with pg_pool.connection() as conn:
        found = bambuddy_pull._holder(conn, {"tag_uid": None, "tray_uuid": U1}, 7)
    assert found == (spool["id"], False)


def test_no_ams_kind_still_writes_the_rows(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_location_kinds SET archived_at = now() WHERE ams")
    _advance(pg_pool, pid, _report(_tray(1, U1), _tray(2)))
    assert [(r.ams_id, r.tray_id) for r in _rows(pg_pool, pid)] == [(0, 1), (0, 2)]
    assert _spools(pg_pool) == []


def test_a_busy_tray_lock_writes_no_row(pg_pool: PgPool) -> None:
    pid, reports = _printer(pg_pool)
    key = f"filament_tray:{pid}:0:2"
    with pg_pool.connection() as other:
        other.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
        try:
            _advance(pg_pool, pid, _report(_tray(2)))
        finally:
            other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
    assert _rows(pg_pool, pid) == []
    assert reports.get(pid) is not None
    # With the lock free, the next changed report writes the row.
    _advance(pg_pool, pid, _report(_tray(2, tray_color="00FF00FF")), T0 + timedelta(minutes=1))
    assert len(_rows(pg_pool, pid)) == 1


def test_a_busy_tray_lock_leaves_the_row_on_clear(pg_pool: PgPool) -> None:
    """D11: a link or a resolve that holds the tray's lock is settling the row, so capture
    does not delete it; the next report does."""
    pid, _ = _printer(pg_pool)
    _advance(pg_pool, pid, _report(_tray(2)))
    (row,) = _rows(pg_pool, pid)
    key = f"filament_tray:{pid}:0:2"
    with pg_pool.connection() as other:
        other.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
        try:
            empty = _report(_tray(2, tray_type="", state=0))
            _advance(pg_pool, pid, empty, T0 + timedelta(minutes=1))
        finally:
            other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
    assert [r.id for r in _rows(pg_pool, pid)] == [row.id]
    again = _report(_tray(2, tray_type="", state=0, tray_color="00000000"))
    _advance(pg_pool, pid, again, T0 + timedelta(minutes=2))
    assert _rows(pg_pool, pid) == []


def test_a_locked_tag_makes_no_spool_and_waits_for_the_next_report(pg_pool: PgPool) -> None:
    """A bind locks the tag before its spools; capture holds spools, so it skips a locked
    tag row instead of waiting on it (no deadlock)."""
    from scadbuddy.filament.spools import FilamentSpoolStore

    FilamentSpoolStore(pg_pool).archive(_spool(pg_pool, tray_uuid=U1), True, actor="t")
    pid, _ = _printer(pg_pool)
    with pg_pool.connection() as holder, holder.transaction():
        holder.execute("SELECT id FROM filament_tags WHERE tray_uuid = %s FOR UPDATE", (U1,))
        _advance(pg_pool, pid, _report(_tray(1, U1)))
    assert len(_spools(pg_pool)) == 1 and _rows(pg_pool, pid) == []
    _advance(pg_pool, pid, _report(_tray(1, U1, remain=50)), T0 + timedelta(minutes=1))
    assert len(_spools(pg_pool)) == 2


def test_a_failing_unknown_step_still_writes_the_report(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pid, reports = _printer(pg_pool)

    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("secret")

    monkeypatch.setattr(capture, "note_unknown", boom)
    with caplog.at_level(logging.WARNING):
        _advance(pg_pool, pid, _report(_tray(2)))
    assert reports.get(pid) is not None
    (record,) = [r for r in caplog.records if r.message == prints.FILAMENT_CAPTURE_FAILED]
    assert record.__dict__["what"] == "unknown_trays"
    assert "secret" not in caplog.text


def test_the_backstop_writes_a_row_the_watch_missed(pg_pool: PgPool) -> None:
    pid, _ = _printer(pg_pool)
    _write_report(pg_pool, pid, _report(_tray(2)), T0)
    capture.reconcile_all(pg_pool, [pid], now=T0 + timedelta(seconds=1))
    assert len(_rows(pg_pool, pid)) == 1


def test_an_out_of_range_remain_is_dropped_from_the_row(pg_pool: PgPool) -> None:
    """A ``remain`` over 100 is no percent (`note_on` takes 0 to 100): the row keeps none.
    With no AMS kind the RFID tray falls back to a row, which carries the reading."""
    pid, _ = _printer(pg_pool)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_location_kinds SET archived_at = now() WHERE ams")
    _advance(pg_pool, pid, _report(_tray(1, U1, remain=150)))
    (row,) = _rows(pg_pool, pid)
    assert row.remain_pct is None


def test_match_tray_prefers_the_preset_of_the_trays_idx(pg_pool: PgPool) -> None:
    older = _type(pg_pool, "OLD")
    newer = _type(pg_pool, "NEW", preset="GFA00")
    with pg_pool.connection() as conn:
        found = SpoolTypeStore.match_tray(conn, "PLA", "#FF0000FF", "GFA00")
        plain = SpoolTypeStore.match_tray(conn, "PLA", "#FF0000FF", None)
        other = SpoolTypeStore.match_tray(conn, "PLA", "#00FF00FF", "GFA00")
    assert found is not None and found.id == newer
    assert plain is not None and plain.id == older
    assert other is None
