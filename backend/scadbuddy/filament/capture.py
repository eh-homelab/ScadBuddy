"""Tray changes in the ledger (#6263 Task 6, spec 2026-11-14-filament-management-design.md
§5.3): what a printer's report shows in each AMS tray, written as ``loaded``, ``unloaded``
and ``ams_reading`` events, with the spool's place following the tray.

`reconcile` compares each tray of the report with the spools in that tray's place (a
location with ``printer_id`` set). The spool the tray holds (`FilamentInventory.holder_on`:
its tag, else the placed spool whose fingerprint still matches) is the tray's. Any other
spool in the place is ``unloaded`` and moves back to the place it came from; the holder
is ``loaded`` and moves in. Every event is timed at the report (``seen_at``), never the
clock, so a resent report books nothing twice and a late one never reorders the ledger.

A report that does not show an AMS unit says nothing of its trays, so they are left alone
(a delta report, a printer mid-reconnect). A tray the report shows empty, in a unit it
shows, is unloaded. The AMS's ``remain`` is a reading only for a tray with RFID keys that
sent it (`bambuddy/trays.py` `seen_trays`), and one only when it moved since the spool's
last reading: a report arrives about once a second, and the ledger's ``refresh_derived``
fast path (`ledger._on_top`) keeps each one from re-walking the spool's history.

`trays_changed` tells the write in `printers/reports.py` whether any of this can have
changed: it compares what a tray shows, never the humidity and temperature the report
carries per unit. `touched` and `lock` let that write take every spool the plate end and
the reconcile will book on, in id order, before either books (no deadlock with a settle).
This module never imports `printers.reports`; that module imports it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.bambuddy.trays import EXTERNAL, seen_trays, tray_label
from scadbuddy.filament import ledger, prints
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.locations import LocationStore
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.status import RUNNING, status_from_report
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools.models import clean_tag_uid, clean_tray_uuid

Conn = Connection[DictRow]

#: Who a watch-booked event names, and a backstop's.
WATCH_ACTOR: Final = "system:printer-watch"
BACKSTOP_ACTOR: Final = "system:capture-backstop"


@dataclass(frozen=True)
class CaptureResult:
    """What one `reconcile` booked."""

    loaded: int = 0
    unloaded: int = 0
    readings: int = 0

    def __add__(self, other: CaptureResult) -> CaptureResult:
        return CaptureResult(
            self.loaded + other.loaded,
            self.unloaded + other.unloaded,
            self.readings + other.readings,
        )


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except ValueError:
        return None


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _tray_row(ams_id: int, tray: Mapping[str, Any]) -> tuple[Any, ...]:
    material = tray.get("tray_type")
    return (
        ams_id,
        _int(tray.get("id")),
        clean_tray_uuid(str(tray.get("tray_uuid") or "")),
        clean_tag_uid(str(tray.get("tag_uid") or "")),
        material.strip() if isinstance(material, str) else None,
        tray.get("tray_color"),
        tray.get("remain"),
    )


def tray_projection(report: Mapping[str, Any]) -> tuple[tuple[Any, ...], ...]:
    """What the report shows in its trays: first the AMS unit ids, then per tray
    ``(ams_id, tray_id, tray_uuid, tag_uid, material, colour, remain)``, from
    ``ams.ams[].tray[]``, ``vt_tray`` and ``vir_slot`` (the external holders are unit
    255). The unit's humidity and temperature are left out: they change between reports
    that change no tray (#6263 preflight C19)."""
    block = report.get("ams")
    units = _dicts(block.get("ams")) if isinstance(block, dict) else []
    ids: list[int] = []
    rows: list[tuple[Any, ...]] = []
    for unit in units:
        unit_id = _int(unit.get("id"))
        if unit_id is None:
            continue
        ids.append(unit_id)
        rows += [_tray_row(unit_id, tray) for tray in _dicts(unit.get("tray"))]
    slots = report.get("vir_slot")
    holders = (
        _dicts(slots) if isinstance(slots, list) and slots else _dicts([report.get("vt_tray")])
    )
    rows += [_tray_row(EXTERNAL, tray) for tray in holders]
    return (tuple(sorted(ids)), *sorted(rows, key=repr))


def trays_changed(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> bool:
    """The two reports show different trays (`tray_projection`); a report with no
    ``before`` is a change."""
    return before is None or tray_projection(before) != tray_projection(after)


def _status(row: PrinterRow, report: Mapping[str, Any], at: datetime) -> PrinterStatus:
    return status_from_report(
        row, LanReport(serial=row.serial, received_at=at, print=dict(report)), now=at
    )


def touched(
    conn: Conn, printer_id: str, report: Mapping[str, Any], ended_job: str | None
) -> list[UUID]:
    """Every spool the plate end and the reconcile of one report can book on, by id: those
    with an open use of ``ended_job``, those in this printer's tray places, and those each
    reported tray holds."""
    found: set[UUID] = set()
    if ended_job is not None:
        found |= {use.spool_id for use in prints.open_for_job(conn, ended_job)}
    found |= {
        row["id"]
        for row in conn.execute(
            "SELECT s.id FROM filament_spools s JOIN filament_locations l ON l.id = s.location_id"
            " WHERE l.printer_id = %s AND l.archived_at IS NULL AND s.archived_at IS NULL",
            (printer_id,),
        ).fetchall()
    }
    row = PrinterStore.read_on(conn, printer_id)
    if row is not None:
        for seen in seen_trays(_status(row, report, datetime.now(UTC))):
            holder, _ = FilamentInventory.holder_on(conn, UUID(printer_id), seen)
            if holder is not None:
                found.add(holder)
    return sorted(found)


def lock(conn: Conn, spool_ids: Sequence[UUID]) -> None:
    """Lock the spools, in id order, in one statement."""
    conn.execute(
        "SELECT 1 FROM filament_spools WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
        (list(spool_ids),),
    ).fetchall()


def _label(row: PrinterRow, ams_id: int, tray_id: int) -> str:
    return f"{row.name} {tray_label(ams_id, tray_id)}"


def _walk(
    status: PrinterStatus, report: Mapping[str, Any], places: Mapping[tuple[int, int], Any]
) -> list[tuple[int, int]]:
    """The trays to compare: each tray of each AMS unit the report shows (its ``tray[]``
    ids joined with the printer's existing places under that unit, so an emptied tray is
    still visited), and the external holders the report names. Trays of a unit the report
    does not show are left alone."""
    keys: set[tuple[int, int]] = set()
    for unit in status.ams:
        keys |= {(unit.id, tray.id) for tray in unit.tray}
        keys |= {key for key in places if key[0] == unit.id}
    if report.get("vt_tray") is not None or report.get("vir_slot") is not None:
        keys |= {(EXTERNAL, tray.id - 254) for tray in status.vt_tray}
    return sorted(keys)


def _moved_from(conn: Conn, spool_id: UUID, place: UUID) -> UUID | None:
    """Where ``spool_id`` came from when it moved into ``place``: the ``from`` of its
    newest ``moved`` event to it, when that is an unarchived place that is not a tray;
    else none."""
    row = conn.execute(
        "SELECT l.id FROM filament_spool_events e JOIN filament_locations l"
        " ON l.id::text = e.data ->> 'from' AND l.archived_at IS NULL AND l.printer_id IS NULL"
        " WHERE e.spool_id = %s AND e.kind = 'moved' AND e.data ->> 'to' = %s"
        " ORDER BY e.at DESC, e.seq DESC LIMIT 1",
        (spool_id, str(place)),
    ).fetchone()
    return None if row is None else row["id"]


def _reading_due(conn: Conn, spool_id: UUID, key: tuple[UUID, int, int], remain: int) -> bool:
    """The spool's newest ``ams_reading`` has another percent or another tray."""
    last = conn.execute(
        "SELECT printer_id, ams_id, tray_id, data -> 'remain_pct' AS pct"
        " FROM filament_spool_events WHERE spool_id = %s AND kind = 'ams_reading'"
        " ORDER BY at DESC, seq DESC LIMIT 1",
        (spool_id,),
    ).fetchone()
    return (
        last is None
        or (last["printer_id"], last["ams_id"], last["tray_id"]) != key
        or (last["pct"] != remain)
    )


def reconcile(
    conn: Conn, printer_id: str, report: Mapping[str, Any], seen_at: datetime, *, actor: str
) -> CaptureResult:
    """Book the loads, unloads and AMS readings the report shows, at ``seen_at``, on
    ``conn``'s transaction. A printer with no row books nothing."""
    row = PrinterStore.read_on(conn, printer_id)
    if row is None:
        return CaptureResult()
    printer = UUID(printer_id)
    status = _status(row, report, seen_at)
    filled = {(t.ams_id, t.tray_id): t for t in seen_trays(status)}
    places: dict[tuple[int, int], UUID] = {}
    current: dict[UUID, list[UUID]] = {}
    for found in conn.execute(
        "SELECT l.id, l.ams_id, l.tray_id, s.id AS spool_id FROM filament_locations l"
        " LEFT JOIN filament_spools s ON s.location_id = l.id AND s.archived_at IS NULL"
        " WHERE l.printer_id = %s AND l.archived_at IS NULL ORDER BY s.id",
        (printer_id,),
    ).fetchall():
        places[(found["ams_id"], found["tray_id"])] = found["id"]
        if found["spool_id"] is not None:
            current.setdefault(found["id"], []).append(found["spool_id"])
    state = report.get("gcode_state")
    printing = isinstance(state, str) and state in RUNNING
    job = report.get("subtask_name") if printing else None
    result = CaptureResult()
    for key in _walk(status, report, places):
        ams_id, tray_id = key
        seen = filled.get(key)
        holder, found_by = (
            FilamentInventory.holder_on(conn, printer, seen) if seen is not None else (None, None)
        )
        place = places.get(key)
        here = current.get(place, []) if place is not None else []
        loaded = False
        if holder not in here:
            for spool in here:
                assert place is not None
                ledger.record(
                    conn,
                    spool,
                    "unloaded",
                    printer=printer,
                    ams_id=ams_id,
                    tray_id=tray_id,
                    actor=actor,
                    at=seen_at,
                    data={"by": found_by},
                )
                LocationStore.move_on(
                    conn, spool, _moved_from(conn, spool, place), actor=actor, at=seen_at
                )
                result += CaptureResult(unloaded=1)
            if holder is not None and seen is not None:
                label = (
                    f"{row.name} external" if ams_id == EXTERNAL else _label(row, ams_id, tray_id)
                )
                target = LocationStore.ensure_ams_on(conn, printer, ams_id, tray_id, label=label)
                ledger.record(
                    conn,
                    holder,
                    "loaded",
                    printer=printer,
                    ams_id=ams_id,
                    tray_id=tray_id,
                    actor=actor,
                    at=seen_at,
                    data={"by": found_by},
                )
                FilamentInventory.place_on(conn, holder, target.id, seen, actor=actor, at=seen_at)
                loaded = True
                result += CaptureResult(loaded=1)
        if (
            holder is not None
            and seen is not None
            and seen.remain is not None
            and (loaded or _reading_due(conn, holder, (printer, ams_id, tray_id), seen.remain))
        ):
            ledger.record(
                conn,
                holder,
                "ams_reading",
                source="ams_remain",
                printer=printer,
                ams_id=ams_id,
                tray_id=tray_id,
                actor=actor,
                at=seen_at,
                data={"remain_pct": seen.remain, "printing": printing, "job": job},
            )
            result += CaptureResult(readings=1)
    return result
