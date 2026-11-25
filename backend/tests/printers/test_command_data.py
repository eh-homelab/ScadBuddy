"""ScadBuddy's own K-profile notes and slot presets (#7276, Task 5)."""

from __future__ import annotations

import uuid
from uuid import UUID

import psycopg.errors
import pytest

from scadbuddy.bambuddy.slot_control import SlotPresetBody
from scadbuddy.printers.command_data import PrinterCommandData
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000007276"


def _printer(printers: PrinterStore, serial: str = SERIAL) -> UUID:
    return uuid.UUID(printers.create(PrinterCreate(serial=serial, name="H2C")).id)


def _body(preset_id: str = "GFA00", name: str = "Bambu PLA Basic") -> SlotPresetBody:
    return SlotPresetBody(preset_id=preset_id, preset_name=name, tray_info_idx="GFA00")


def test_a_blank_note_deletes_it(printers: PrinterStore, pg_pool: PgPool) -> None:
    pid = _printer(printers)
    data = PrinterCommandData(pg_pool)
    data.set_note(pid, "0:0.4:PLA", "good")
    assert data.notes(pid) == {"0:0.4:PLA": "good"}
    data.set_note(pid, "0:0.4:PLA", "better")
    assert data.notes(pid) == {"0:0.4:PLA": "better"}
    data.set_note(pid, "0:0.4:PLA", "   ")
    assert data.notes(pid) == {}
    data.set_note(pid, "k", "x")
    data.delete_note(pid, "k")
    assert data.notes(pid) == {}


def test_a_note_key_with_a_slash_is_refused(printers: PrinterStore, pg_pool: PgPool) -> None:
    pid = _printer(printers)
    with pytest.raises(psycopg.errors.CheckViolation):
        PrinterCommandData(pg_pool).set_note(pid, "a/b", "x")


def test_a_slot_preset_is_one_row_per_slot(printers: PrinterStore, pg_pool: PgPool) -> None:
    pid = _printer(printers)
    data = PrinterCommandData(pg_pool)
    first = data.set_slot_preset(pid, 0, 1, _body())
    assert (first.preset_id, first.preset_name, first.tray_info_idx) == (
        "GFA00",
        "Bambu PLA Basic",
        "GFA00",
    )
    data.set_slot_preset(pid, 0, 2, _body("GFB00", "PETG"))
    again = data.set_slot_preset(pid, 0, 1, _body("GFC00", "ABS"))
    assert again.preset_id == "GFC00"
    saved = data.slot_presets(pid)
    assert set(saved) == {(0, 1), (0, 2)}
    assert saved[(0, 1)].preset_name == "ABS"
    data.clear_slot_preset(pid, 0, 1)
    assert set(data.slot_presets(pid)) == {(0, 2)}


def test_a_printer_delete_takes_its_notes_and_presets(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    pid = _printer(printers)
    data = PrinterCommandData(pg_pool)
    data.set_note(pid, "k", "x")
    data.set_slot_preset(pid, 0, 0, _body())
    printers.delete(str(pid))
    with pg_pool.connection() as conn:
        for table in ("printer_kprofile_notes", "printer_slot_presets"):
            row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            assert row is not None and row["n"] == 0


def test_a_merge_keeps_the_kept_printers_note_on_a_clash(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    synthetic = printers.ensure_bambuddy(7, serial=None, name="H2C", model="H2C")
    gone = uuid.UUID(synthetic.id)
    kept = _printer(printers)
    data = PrinterCommandData(pg_pool)
    data.set_note(gone, "clash", "gone")
    data.set_note(kept, "clash", "kept")
    data.set_note(gone, "only", "moved")
    data.set_slot_preset(gone, 0, 1, _body("GONE", "gone"))
    data.set_slot_preset(kept, 0, 1, _body("KEPT", "kept"))
    data.set_slot_preset(gone, 0, 2, _body("MOVED", "moved"))

    printers.ensure_bambuddy(7, serial=SERIAL, name="H2C", model="H2C")

    assert data.notes(kept) == {"clash": "kept", "only": "moved"}
    presets = data.slot_presets(kept)
    assert presets[(0, 1)].preset_id == "KEPT" and presets[(0, 2)].preset_id == "MOVED"
