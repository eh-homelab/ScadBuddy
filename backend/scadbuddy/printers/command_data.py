"""ScadBuddy's own K-profile notes and slot presets (#7276, Task 5): what a person wrote
about a printer's K profile and the slicer preset a slot shows. One row per note key and
per slot, on the shared rows pool. A printer's delete takes them and a merge keeps the
kept printer's row on a clash (`printers/store.py` `PRINTER_REFERENCES`)."""

from __future__ import annotations

from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.slot_control import SavedSlotPreset, SlotPresetBody


class PrinterCommandData:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def notes(self, printer_id: UUID) -> dict[str, str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT note_key, note FROM printer_kprofile_notes WHERE printer_id = %s",
                (printer_id,),
            ).fetchall()
        return {row["note_key"]: row["note"] for row in rows}

    def set_note(self, printer_id: UUID, key: str, note: str) -> None:
        """Save ``note`` under ``key``; a blank note deletes it."""
        if not note.strip():
            self.delete_note(printer_id, key)
            return
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO printer_kprofile_notes (printer_id, note_key, note)"
                " VALUES (%s, %s, %s) ON CONFLICT (printer_id, note_key)"
                " DO UPDATE SET note = EXCLUDED.note, updated_at = now()",
                (printer_id, key, note),
            )

    def delete_note(self, printer_id: UUID, key: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM printer_kprofile_notes WHERE printer_id = %s AND note_key = %s",
                (printer_id, key),
            )

    def slot_presets(self, printer_id: UUID) -> dict[tuple[int, int], SavedSlotPreset]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT ams_id, tray_id, preset_id, preset_name, tray_info_idx"
                " FROM printer_slot_presets WHERE printer_id = %s",
                (printer_id,),
            ).fetchall()
        return {
            (row["ams_id"], row["tray_id"]): SavedSlotPreset(
                preset_id=row["preset_id"],
                preset_name=row["preset_name"],
                tray_info_idx=row["tray_info_idx"],
            )
            for row in rows
        }

    def set_slot_preset(
        self, printer_id: UUID, ams_id: int, tray_id: int, body: SlotPresetBody
    ) -> SavedSlotPreset:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO printer_slot_presets"
                " (printer_id, ams_id, tray_id, preset_id, preset_name, tray_info_idx)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (printer_id, ams_id, tray_id) DO UPDATE SET"
                " preset_id = EXCLUDED.preset_id, preset_name = EXCLUDED.preset_name,"
                " tray_info_idx = EXCLUDED.tray_info_idx, updated_at = now()",
                (
                    printer_id,
                    ams_id,
                    tray_id,
                    body.preset_id,
                    body.preset_name,
                    body.tray_info_idx,
                ),
            )
        return SavedSlotPreset(
            preset_id=body.preset_id,
            preset_name=body.preset_name,
            tray_info_idx=body.tray_info_idx,
        )

    def clear_slot_preset(self, printer_id: UUID, ams_id: int, tray_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM printer_slot_presets"
                " WHERE printer_id = %s AND ams_id = %s AND tray_id = %s",
                (printer_id, ams_id, tray_id),
            )
