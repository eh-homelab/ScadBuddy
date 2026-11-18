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
(a delta report, a printer mid-reconnect). A tray the report shows loading or
scanning (a material and the SPOOL bit, not STEADY) is no news either. A tray the report
shows empty, in a unit it shows, is unloaded. The AMS's ``remain`` is a reading only for
a tray with RFID keys that sent it (`bambuddy/trays.py` `seen_trays`), and one only when it
moved since the spool's last reading: a report arrives about once a second, and the
ledger's ``refresh_derived`` fast path (`ledger._on_top`) keeps each one from re-walking
the spool's history.

`trays_changed` tells the write in `printers/reports.py` whether any of this can have
changed: it compares what a tray shows, never the humidity and temperature the report
carries per unit. `touched` and `lock` let that write take every spool the plate end and
the reconcile will book on, in id order, before either books (no deadlock with a settle).
This module never imports `printers.reports`; that module imports it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.bambuddy.trays import EXTERNAL, loading_trays, place_label, seen_trays
from scadbuddy.filament import ledger, prints
from scadbuddy.filament import models as m
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.locations import LocationStore
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.status import RUNNING, is_fresh, status_from_report
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools.models import clean_tag_uid, clean_tray_uuid

Conn = Connection[DictRow]
logger = logging.getLogger(__name__)

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
        tray.get("state"),
    )


def tray_projection(report: Mapping[str, Any]) -> tuple[tuple[Any, ...], ...]:
    """What the report shows in its trays: first the AMS unit ids, then per tray
    ``(ams_id, tray_id, tray_uuid, tag_uid, material, colour, remain, state)``, from
    ``ams.ams[].tray[]``, ``vt_tray`` and ``vir_slot`` (the external holders are unit
    255), and the unit ids come with ``ams.tray_exist_bits``. A tray's ``state`` and the
    presence bits are in because `bambuddy/trays.py` `_filled` reads both: a pulled AMS-HT
    spool keeps its ``tray_type`` and changes only those. The unit's humidity and
    temperature are left out: they change between reports that change no tray (#6263
    preflight C19)."""
    block = report.get("ams")
    units = _dicts(block.get("ams")) if isinstance(block, dict) else []
    bits = block.get("tray_exist_bits") if isinstance(block, dict) else None
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
    return ((tuple(sorted(ids)), bits), *sorted(rows, key=repr))


def trays_changed(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> bool:
    """The two reports show different trays (`tray_projection`); a report with no
    ``before`` is a change."""
    return before is None or tray_projection(before) != tray_projection(after)


def _status(row: PrinterRow, report: Mapping[str, Any], at: datetime) -> PrinterStatus:
    return status_from_report(
        row, LanReport(serial=row.serial, received_at=at, print=dict(report)), now=at
    )


def touched(
    conn: Conn,
    printer_id: str,
    report: Mapping[str, Any],
    ended_job: str | None,
    seen_at: datetime,
) -> list[UUID]:
    """Every spool the plate end and the reconcile of one report can book on, by id: those
    with an open use of ``ended_job``, those in this printer's tray places, and those each
    reported tray holds."""
    found: set[UUID] = set()
    if ended_job is not None:
        found |= {use.spool_id for use in prints.open_for_job(conn, printer_id, ended_job)}
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
        for seen in seen_trays(_status(row, report, seen_at)):
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


def _holders(
    conn: Conn, printer_id: str
) -> tuple[dict[tuple[int, int], UUID], dict[UUID, list[UUID]], set[UUID]]:
    """The printer's tray places by ``(ams_id, tray_id)``, the unarchived spools now in each
    place, and those of them whose weight is locked. An AMS-unit place is no tray."""
    places: dict[tuple[int, int], UUID] = {}
    current: dict[UUID, list[UUID]] = {}
    locked: set[UUID] = set()
    for found in conn.execute(
        "SELECT l.id, l.ams_id, l.tray_id, s.id AS spool_id, s.weight_locked"
        " FROM filament_locations l"
        " LEFT JOIN filament_spools s ON s.location_id = l.id AND s.archived_at IS NULL"
        " WHERE l.printer_id = %s AND l.tray_id IS NOT NULL AND l.archived_at IS NULL"
        " ORDER BY s.id",
        (printer_id,),
    ).fetchall():
        places[(found["ams_id"], found["tray_id"])] = found["id"]
        if found["spool_id"] is not None:
            current.setdefault(found["id"], []).append(found["spool_id"])
            if found["weight_locked"]:
                locked.add(found["spool_id"])
    return places, current, locked


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
    loading = loading_trays(status)
    places, current, _ = _holders(conn, printer_id)
    state = report.get("gcode_state")
    printing = isinstance(state, str) and state in RUNNING
    job = report.get("subtask_name") if printing else None
    result = CaptureResult()
    loaded_now: set[UUID] = set()
    for key in _walk(status, report, places):
        if key in loading:
            continue
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
                if spool in loaded_now:
                    continue
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
                label = place_label(row.name, ams_id, tray_id)
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
                loaded_now.add(holder)
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


def reconcile_all(
    pool: ConnectionPool[Conn], printer_ids: Sequence[str], *, now: datetime
) -> CaptureResult:
    """The backstop for what the printer watch missed: each printer's latest fresh report,
    reconciled in a transaction of its own. The report row is locked first, as the watch's
    write locks it, then the spools it can touch; the reconcile reads the row it locked. A
    stale report is the watch's to answer for. A printer that raises is logged by its
    error's type and skipped."""
    total = CaptureResult()
    for printer_id in printer_ids:
        try:
            with pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    "SELECT report, seen_at FROM printer_reports WHERE printer_id = %s FOR UPDATE",
                    (printer_id,),
                ).fetchone()
                if row is None or not is_fresh(row["seen_at"], now):
                    continue
                report = dict(row["report"])
                lock(conn, touched(conn, printer_id, report, None, row["seen_at"]))
                total += reconcile(conn, printer_id, report, row["seen_at"], actor=BACKSTOP_ACTOR)
        except Exception as error:
            logger.warning(
                prints.FILAMENT_CAPTURE_FAILED,
                extra={"printer_id": printer_id, "what": "backstop", "error": type(error).__name__},
            )
    return total


def _stale(
    conn: Conn,
    result: m.AmsSyncResult,
    printer_id: str,
    holders: list[UUID],
    *,
    name: str | None = None,
    gone: bool = False,
) -> None:
    """Count ``holders`` as ``stale`` and name their printer in ``stale_printers`` (#6679).
    A printer that holds no spool is not named. With no ``name`` and not ``gone``, the name
    is read from the printer's row."""
    if not holders:
        return
    result.stale += len(holders)
    if name is None and not gone:
        row = conn.execute("SELECT name FROM printers WHERE id = %s", (printer_id,)).fetchone()
        name = None if row is None else row["name"]
    result.stale_printers.append(m.StalePrinter(id=UUID(printer_id), name=name))


def sync_ams_weights(pool: ConnectionPool[Conn], *, actor: str, now: datetime) -> m.AmsSyncResult:
    """Book the AMS's ``remain`` of every spool in a printer's tray place now as an
    ``ams_reading`` timed ``now`` (``data.forced``), so it anchors the grams left (#6268
    Decision 2). Each printer is one transaction: its report row is locked first, then its
    spools, as `reconcile_all` does, and the holders and weight locks are read after the
    spool locks, so a lock set meanwhile is honoured. The tray's holder is the spool placed
    there now, never an older one (an AMS-unit place holds no spool). Each such spool is
    counted once: ``booked``, ``locked`` (its weight is locked), ``printing`` (its printer
    is printing), ``no_remain`` (its tray shows no ``remain``, or none at all) or ``stale``
    (its printer has no report or no row, or a report older than `is_fresh` or one that
    cannot be read: that is logged, and the other printers go on); each such printer is
    named once in ``stale_printers``. A spool in no tray
    place is not counted, nor is one placed in a tray between the first holder read and
    the spool locks: it was not locked, so it is left to the next sync."""
    with pool.connection() as conn:
        ids = [
            str(r["printer_id"])
            for r in conn.execute(
                "SELECT printer_id FROM printer_reports"
                " UNION SELECT printer_id FROM filament_locations"
                " WHERE printer_id IS NOT NULL AND tray_id IS NOT NULL AND archived_at IS NULL"
                " ORDER BY printer_id"
            ).fetchall()
        ]
    result = m.AmsSyncResult()
    for printer_id in ids:
        with pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT report, seen_at FROM printer_reports WHERE printer_id = %s FOR UPDATE",
                (printer_id,),
            ).fetchone()
            places, current, _ = _holders(conn, printer_id)
            taken = {s for spools in current.values() for s in spools}
            lock(conn, sorted(taken))
            places, current, locked = _holders(conn, printer_id)
            current = {p: [s for s in spools if s in taken] for p, spools in current.items()}
            holders = sorted({s for spools in current.values() for s in spools})
            if row is None or not is_fresh(row["seen_at"], now):
                _stale(conn, result, printer_id, holders)
                continue
            printer = PrinterStore.read_on(conn, printer_id)
            if printer is None:
                _stale(conn, result, printer_id, holders, gone=True)
                continue
            report = dict(row["report"])
            state = report.get("gcode_state")
            printing = isinstance(state, str) and state in RUNNING
            if printing:
                result.printing += len(holders)
                continue
            try:
                filled = {
                    (t.ams_id, t.tray_id): t for t in seen_trays(_status(printer, report, now))
                }
            except Exception as error:
                # A malformed stored report is one printer's problem, as in `reconcile_all`.
                logger.warning(
                    prints.FILAMENT_CAPTURE_FAILED,
                    extra={
                        "printer_id": printer_id,
                        "what": "ams sync",
                        "error": type(error).__name__,
                    },
                )
                _stale(conn, result, printer_id, holders, name=printer.name)
                continue
            for (ams_id, tray_id), place in places.items():
                seen = filled.get((ams_id, tray_id))
                for spool in current.get(place, []):
                    if spool in locked:
                        result.locked += 1
                    elif seen is None or seen.remain is None:
                        result.no_remain += 1
                    else:
                        ledger.record(
                            conn,
                            spool,
                            "ams_reading",
                            source="ams_remain",
                            printer=UUID(printer_id),
                            ams_id=ams_id,
                            tray_id=tray_id,
                            actor=actor,
                            at=now,
                            data={"remain_pct": seen.remain, "printing": False, "forced": True},
                        )
                        result.booked += 1
                        result.spools.append(spool)
    return result
