"""Bambuddy's ``PrinterStatus`` from a LAN report (#3823, plan 2026-10-16, Task 5), so
``/printers/now``, the print dialog and the native follow read one shape whichever link
answered.

Field names are OpenBambuAPI `mqtt.md`'s ``pushall`` report
(https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md, read 2026-10-18): the AMS
units under ``ams.ams`` (ids, humidity and temperatures as strings), ``vt_tray`` the
external spool (an object there; a list is taken too), ``hms`` entries of ``attr`` and
``code`` integers. The H2-series blocks are ha-bambulab's (``pybambu/models.py``, read
the same day): ``device.nozzle.info`` lists each mounted nozzle (ids 0 and 1, by
extruder) and each rack slot (ids from 16) with ``type``, ``diameter``, ``sn``,
``wear`` and ``stat``; ``device.extruder.state`` carries the active extruder in bits
4-7.

Not in a recorded report: the test fixture is synthetic (no printer was reachable when
this was written, plan Task 7). Not mapped, so left at their defaults: ``ams_mapping``,
``ams_extruder_map``, ``ams_switch_inlet``, ``fila_switch``, ``current_plate_id``,
``current_archive_id`` (Bambuddy's own), ``awaiting_plate_clear`` and ``cover_url``.
A field that is not the type it should be reads as absent, never as a failed status.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from scadbuddy.bambuddy.models import (
    AmsTray,
    AmsUnit,
    HmsError,
    NozzleInfo,
    NozzleRackSlot,
    PrinterStatus,
)
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow

#: A printer whose last report is older than this is offline (Review Focus 5).
STALE_AFTER = timedelta(seconds=90)
#: The first rack slot's id in ``device.nozzle.info`` (ha-bambulab ``RACK_SLOT_IDS``).
RACK_FIRST_ID = 16


def _valid[M: BaseModel](model: type[M], data: Any) -> M | None:
    if not isinstance(data, dict):
        return None
    try:
        return model.model_validate(data)
    except ValidationError:
        return None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _trays(value: Any) -> list[AmsTray]:
    return [tray for entry in _list(value) if (tray := _valid(AmsTray, entry)) is not None]


def _ams(report: dict[str, Any]) -> list[AmsUnit]:
    block = report.get("ams")
    units = block.get("ams") if isinstance(block, dict) else None
    found: list[AmsUnit] = []
    for entry in _list(units):
        if not isinstance(entry, dict):
            continue
        unit = _valid(AmsUnit, {**entry, "tray": _trays(entry.get("tray"))})
        if unit is not None:
            found.append(unit)
    return found


def _vt_tray(report: dict[str, Any]) -> list[AmsTray]:
    value = report.get("vt_tray")
    return _trays([value] if isinstance(value, dict) else value)


def hms_errors(value: Any) -> list[HmsError]:
    """``hms`` entries as Bambuddy's ``HmsError``: the code ``<attr><code>`` in 16 hex
    digits (``0700220000020025``, Bambuddy's ``full_code``), the severity the code's
    third hex digit from the top, no description (Bambuddy's text is its own)."""
    errors: list[HmsError] = []
    for entry in _list(value):
        if not isinstance(entry, dict):
            continue
        attr, code = _int(entry.get("attr")), _int(entry.get("code"))
        if attr is None or code is None:
            continue
        text = f"{attr & 0xFFFFFFFF:08X}{code & 0xFFFFFFFF:08X}"
        errors.append(
            HmsError(code=text, full_code=text, severity=(code >> 16) & 0xF, description=None)
        )
    return errors


def _device(report: dict[str, Any], name: str) -> dict[str, Any]:
    device = report.get("device")
    block = device.get(name) if isinstance(device, dict) else None
    return block if isinstance(block, dict) else {}


def _nozzles(report: dict[str, Any]) -> tuple[list[NozzleInfo], list[NozzleRackSlot]]:
    mounted: dict[int, NozzleInfo] = {}
    rack: list[NozzleRackSlot] = []
    for entry in _list(_device(report, "nozzle").get("info")):
        if not isinstance(entry, dict) or (slot_id := _int(entry.get("id"))) is None:
            continue
        fields = {
            "nozzle_type": str(entry.get("type") or ""),
            "nozzle_diameter": str(entry.get("diameter") or ""),
        }
        if slot_id >= RACK_FIRST_ID:
            slot = _valid(
                NozzleRackSlot,
                {
                    **fields,
                    "id": slot_id,
                    "serial_number": entry.get("sn"),
                    "wear": _int(entry.get("wear")),
                    "stat": _int(entry.get("stat")),
                },
            )
            if slot is not None:
                rack.append(slot)
        else:
            mounted[slot_id] = NozzleInfo.model_validate(fields)
    if not mounted and ("nozzle_type" in report or "nozzle_diameter" in report):
        mounted[0] = NozzleInfo(
            nozzle_type=str(report.get("nozzle_type") or ""),
            nozzle_diameter=str(report.get("nozzle_diameter") or ""),
        )
    return [mounted[key] for key in sorted(mounted)], rack


def _active_extruder(report: dict[str, Any]) -> int:
    state = _int(_device(report, "extruder").get("state"))
    return 0 if state is None else (state >> 4) & 0xF


def status_from_report(row: PrinterRow, report: LanReport, *, now: datetime) -> PrinterStatus:
    """``row``'s status from its latest merged report. ``id`` is the Bambuddy printer's
    when the row links one, else 0 (a LAN-only printer has no Bambuddy id); the card
    and the run name the printer by ``row.id``."""
    data = report.print
    nozzles, rack = _nozzles(data)
    ams = _ams(data)
    status = PrinterStatus.model_validate(
        {
            "id": row.bambuddy_printer_id or 0,
            "name": row.name,
            "connected": now - report.received_at < STALE_AFTER,
            "state": data.get("gcode_state") if isinstance(data.get("gcode_state"), str) else None,
            "progress": data.get("mc_percent"),
            "remaining_time": data.get("mc_remaining_time"),
            "layer_num": data.get("layer_num"),
            "total_layers": data.get("total_layer_num"),
            "subtask_name": data.get("subtask_name"),
            "hms_errors": [],
        }
    )
    return status.model_copy(
        update={
            "ams": ams,
            "ams_exists": bool(ams),
            "vt_tray": _vt_tray(data),
            "nozzles": nozzles,
            "nozzle_rack": rack,
            "active_extruder": _active_extruder(data),
            "hms_errors": hms_errors(data.get("hms")),
        }
    )
