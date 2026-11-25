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

``fila_switch`` is fitted when ``device.fila_switch`` is present, and each AMS unit's
``info`` (hex) names its extruder in bits 8-11, ``0xE`` meaning "not fixed": with the
switch fitted, bits 24-27 then name its inlet, 0 B and 1 A (Bambuddy
``bambu_mqtt.py`` ``_parse_fila_switch`` and its ``ams_extruder_map`` pass, after
BambuStudio ``DevFilaSystem.cpp``, read in the deployed image 2026-10-24). A rack
slot's ``fila_id`` and ``color_m`` are the filament it last ran.

`tests/printers/fixtures/lan-report-h2c-idle.json` is a recorded H2C report (firmware
01.02.00.00, 2026-10-24): four AMS 2 Pro on the switch's inlets and an AMS-HT, five rack
slots. Not mapped, so left at their defaults: ``ams_mapping``, ``current_plate_id``,
``current_archive_id`` (Bambuddy's own), ``awaiting_plate_clear`` and ``cover_url``.
A field that is not the type it should be reads as absent, never as a failed status.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from scadbuddy.bambuddy.models import (
    AmsTray,
    AmsUnit,
    FilaSwitch,
    HmsError,
    NozzleInfo,
    NozzleRackSlot,
    PrinterStatus,
)
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow

#: A printer whose last report is older than this is offline (Review Focus 5).
STALE_AFTER = timedelta(seconds=90)

#: The states that show the printer running a plate (OpenBambuAPI ``mqtt.md``).
RUNNING = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})


def is_fresh(seen_at: datetime, now: datetime) -> bool:
    """Whether a LAN report seen at ``seen_at`` still answers for its printer: the one
    rule for the card's ``connected``, `printers_now`'s path and a run's (#4442 I3)."""
    return now - seen_at < STALE_AFTER


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


def print_error_text(code: int) -> str:
    """A report's ``print_error`` as Bambu's apps show it: eight hex digits split in
    two, ``0500_C010``."""
    text = f"{code & 0xFFFFFFFF:08X}"
    return f"{text[:4]}_{text[4:]}"


def _text(value: Any) -> str:
    """``value`` when it is a string, else ``""``: a mistyped field reads as absent."""
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _trays(value: Any) -> list[AmsTray]:
    return [tray for entry in _list(value) if (tray := _valid(AmsTray, entry)) is not None]


#: The A2L AMS-Lite reports unit 16 and takes the bit base of unit 6 (Bambuddy
#: ``normalize_am_unit_id``, ``bambu_mqtt.py:115-127`` at ``0d427510``).
_A2L_PHYSICAL, _A2L_BITS = 16, 6


def _presence_bits(block: dict[str, Any]) -> int | None:
    """``ams.tray_exist_bits``, the firmware's "which slots hold a spool" mask (hex), or
    ``None`` when the report carries none, or carries the printer-off pattern: all zeros
    with ``power_on_flag`` false (Bambuddy ``apply_tray_exist_bits``, #765)."""
    raw = block.get("tray_exist_bits")
    try:
        bits = raw if isinstance(raw, int) else int(str(raw), 16) if raw else None
    except ValueError:
        return None
    if bits is None or (bits == 0 and block.get("power_on_flag", True) is False):
        return None
    return bits


def _presence_bit(ams_id: int, tray_id: int) -> int | None:
    """Where a tray's presence is in ``tray_exist_bits``: ``ams_id * 4 + tray`` for an
    AMS (0-15, the A2L folded to 6), ``16 + (ams_id - 128)`` for an AMS-HT (128-135);
    ``None`` for any other unit, whose layout is unknown (Bambuddy
    ``apply_tray_exist_bits``, ``bambu_mqtt.py:216-330`` at ``0d427510``, which checked the
    HT's against OrcaSlicer ``DevFilaSystem.cpp`` and a live H2D capture). Not yet
    measured on our H2C: the LAN fixture is synthetic (plan 2026-10-16 Task 7).

    The HT bits 16-23 are also AMS units 4 and 5's bits, so :func:`ams_units` vetoes by no
    bit two reported trays share: nothing says whose it is (#4377)."""
    unit = _A2L_BITS if ams_id == _A2L_PHYSICAL else ams_id
    if 128 <= unit <= 135:
        return 16 + (unit - 128)
    if 0 <= unit <= 15:
        return unit * 4 + tray_id
    return None


def ams_units(report: dict[str, Any]) -> list[AmsUnit]:
    block = report.get("ams")
    units = block.get("ams") if isinstance(block, dict) else None
    bits = _presence_bits(block) if isinstance(block, dict) else None
    found: list[AmsUnit] = []
    for entry in _list(units):
        if not isinstance(entry, dict):
            continue
        # The LAN report's `humidity` is Bambu's 1-5 level; the percent is `humidity_raw`
        # (recorded H2C report: "humidity": "3" beside "humidity_raw": "29"). `AmsUnit`
        # holds the percent, so a report without it has none. Bambuddy's status gives the
        # percent too (measured 2026-11-18, read-only GETs on the live Bambuddy 1.3.1, H2C
        # printer 1: `/api/v1/printers/1/status` `ams[].humidity` was 31, 17, 14, 15 and 19,
        # and `/api/v1/ams-history/1/{ams_id}`'s latest points had `humidity` equal to
        # `humidity_raw` for every unit; plan 2026-11-18-filament-weights-sensors Decision 28).
        unit = _valid(
            AmsUnit,
            {**entry, "humidity": entry.get("humidity_raw"), "tray": _trays(entry.get("tray"))},
        )
        if unit is not None:
            found.append(unit)
    if bits is None:
        return found
    claims = Counter(
        bit
        for unit in found
        for tray in unit.tray
        if (bit := _presence_bit(unit.id, tray.id)) is not None
    )
    for unit in found:
        # Bambuddy annotates the same ``exists`` (``annotate_exists=True``), which
        # `trays._filled` reads as a veto; a shared bit leaves both trays unknown.
        for index, tray in enumerate(unit.tray):
            bit = _presence_bit(unit.id, tray.id)
            if bit is not None and claims[bit] == 1:
                unit.tray[index] = tray.model_copy(update={"exists": bool(bits >> bit & 1)})
    return found


def _vt_tray(report: dict[str, Any]) -> list[AmsTray]:
    """The external holders. An H2-series printer lists both in ``vir_slot`` and sends
    ``vt_tray`` as the one active spool, so ``vir_slot`` wins (Bambuddy
    ``bambu_mqtt.py:2323-2349`` at ``0d427510``; ha-bambulab ``pybambu/models.py:3696`` at
    ``0e027ff1``). A single ``vir_slot`` holder that says 255 is a single-nozzle
    printer's one holder, 254, as Bambuddy reads it. Any other id is dropped: a holder
    is tray ``id - 254`` (``trays._trays``), so no other id names one (#4377)."""
    slots = report.get("vir_slot")
    if isinstance(slots, list) and slots:
        trays = _trays(slots)
        if len(trays) == 1 and trays[0].id == 255:
            trays = [trays[0].model_copy(update={"id": 254})]
    else:
        value = report.get("vt_tray")
        trays = _trays([value] if isinstance(value, dict) else value)
    return [tray for tray in trays if tray.id in (254, 255)]


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


def mounted_nozzles(report: dict[str, Any]) -> dict[int, NozzleInfo]:
    """The nozzles mounted on the extruders, by extruder id (``device.nozzle.info`` ids 0
    and 1; a printer that reports one nozzle at the top level is extruder 0). A missing id
    is missing here: `PrinterStatus.nozzles` is sorted by id and so shifts."""
    return _scan_nozzles(report)[0]


def _nozzles(report: dict[str, Any]) -> tuple[list[NozzleInfo], list[NozzleRackSlot]]:
    mounted, rack = _scan_nozzles(report)
    return [mounted[key] for key in sorted(mounted)], rack


def _scan_nozzles(report: dict[str, Any]) -> tuple[dict[int, NozzleInfo], list[NozzleRackSlot]]:
    mounted: dict[int, NozzleInfo] = {}
    rack: list[NozzleRackSlot] = []
    for entry in _list(_device(report, "nozzle").get("info")):
        if not isinstance(entry, dict) or (slot_id := _int(entry.get("id"))) is None:
            continue
        fields = {
            "nozzle_type": str(entry.get("type") or ""),
            "nozzle_diameter": str(entry.get("diameter") or ""),
        }
        slot = _valid(
            NozzleRackSlot,
            {
                **fields,
                "id": slot_id,
                "serial_number": entry.get("sn"),
                "wear": _int(entry.get("wear")),
                "stat": _int(entry.get("stat")),
                "filament_id": _text(entry.get("fila_id")),
                "filament_color": _text(entry.get("color_m")),
            },
        )
        if slot is not None:
            rack.append(slot)
        if slot_id < RACK_FIRST_ID:
            mounted[slot_id] = NozzleInfo.model_validate(fields)
    if not mounted and ("nozzle_type" in report or "nozzle_diameter" in report):
        mounted[0] = NozzleInfo(
            nozzle_type=str(report.get("nozzle_type") or ""),
            nozzle_diameter=str(report.get("nozzle_diameter") or ""),
        )
    # The mounted hotends are listed with the rack, as Bambuddy's status lists them: the
    # carriage's (id 0) is the position the report omits while it is mounted
    # (`rack.rank.rack_positions`; the H2C's idle report of 2026-10-24 omits 19 and has
    # ``src_id`` 19). A printer with no rack lists none.
    if not any(slot.id >= RACK_FIRST_ID for slot in rack):
        rack = []
    return mounted, rack


def _fila_switch(report: dict[str, Any]) -> FilaSwitch | None:
    device = report.get("device")
    fitted = isinstance(device, dict) and isinstance(device.get("fila_switch"), dict)
    return FilaSwitch(installed=True) if fitted else None


def _routing(report: dict[str, Any], switch: bool) -> tuple[dict[str, int], dict[str, str]]:
    """``ams_extruder_map`` and ``ams_switch_inlet`` from each unit's ``info``."""
    block = report.get("ams")
    extruders: dict[str, int] = {}
    inlets: dict[str, str] = {}
    for entry in _list(block.get("ams") if isinstance(block, dict) else None):
        if not isinstance(entry, dict) or (unit := _int(entry.get("id"))) is None:
            continue
        raw = entry.get("info")
        try:
            info = int(raw, 16) if isinstance(raw, str) else None
        except ValueError:
            info = None
        if info is None:
            continue
        extruder = info >> 8 & 0xF
        if extruder != 0xE:
            extruders[str(unit)] = extruder
        elif switch and (inlet := {0: "B", 1: "A"}.get(info >> 24 & 0xF)) is not None:
            inlets[str(unit)] = inlet
    return extruders, inlets


def _active_extruder(report: dict[str, Any]) -> int:
    state = _int(_device(report, "extruder").get("state"))
    return 0 if state is None else (state >> 4) & 0xF


#: ``snow`` when an extruder holds nothing (Bambuddy ``tray_ids.py`` ``WIRE_SLOT_NONE``).
SNOW_NONE = 0xFFFF


def decode_snow(value: Any) -> tuple[int, int] | None:
    """The ``(ams_id, slot)`` an extruder's ``snow`` names: ``ams_id << 8 | slot``, an AMS
    0-3 by its slot's low two bits and an AMS-HT 128-135 as slot 0, as Bambuddy's
    ``decode_wire_slot`` reads it:
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/utils/tray_ids.py#L349-L374
    ``None`` for ``SNOW_NONE`` and for what it cannot decode. An external holder (high
    byte 254 or 255) is ``None`` too: ``decode_wire_slot`` reads 0xFEFF as Ext-R, but the
    recorded idle H2C (``tests/printers/fixtures/lan-report-h2c-idle.json``, nothing
    loaded, ``tray_now`` 255) reports 0xFEFF on extruder 1, and Bambuddy's own unload
    leaves 254/255 out of its ``snow`` check (``bambu_mqtt.py`` L8491-L8493 at that
    commit). The external holders keep the ``tray_now`` rule (`ams_slots.is_loaded`)."""
    if isinstance(value, bool):
        return None
    snow = _int(value) if isinstance(value, int | str) else None
    if snow is None or not 0 <= snow < SNOW_NONE:
        return None
    ams_id, slot = snow >> 8, snow & 0xFF
    if 0 <= ams_id <= 3:
        return ams_id, slot & 0x03
    if 128 <= ams_id <= 135:
        return ams_id, 0
    return None


def extruder_loaded(report: dict[str, Any]) -> list[tuple[int, int] | None]:
    """Each extruder's loaded slot (`decode_snow`) from ``device.extruder.info``, by
    extruder id; ``[]`` unless two or more entries carry an ``id`` and a ``snow``. Bambuddy
    reads ``snow`` only from two extruders' entries (``bambu_mqtt.py`` L3551-L3575 and
    the unload's dual-nozzle gate at L8473-L8493, d3be761f): nobody has read a
    single-nozzle printer's ``snow``, so one with one entry keeps ``tray_now``."""
    info = _device(report, "extruder").get("info")
    if not isinstance(info, list):
        return []
    by_id: dict[int, tuple[int, int] | None] = {}
    for entry in info:
        if not isinstance(entry, dict) or "snow" not in entry:
            continue
        ext_id = entry.get("id")
        if isinstance(ext_id, bool) or not isinstance(ext_id, int | str):
            continue
        key = _int(ext_id)
        if key is None or not 0 <= key <= 1:
            continue
        by_id[key] = decode_snow(entry["snow"])
    if len(by_id) < 2:
        return []
    return [by_id[key] for key in sorted(by_id)]


def status_from_report(row: PrinterRow, report: LanReport, *, now: datetime) -> PrinterStatus:
    """``row``'s status from its latest merged report. ``id`` is the Bambuddy printer's
    when the row links one, else 0 (a LAN-only printer has no Bambuddy id); the card
    and the run name the printer by ``row.id``."""
    data = report.print
    nozzles, rack = _nozzles(data)
    ams = ams_units(data)
    switch = _fila_switch(data)
    extruders, inlets = _routing(data, switch is not None)
    block = data.get("ams")
    raw_ams_status = data.get("ams_status")
    ams_status = None if isinstance(raw_ams_status, bool) else _int(raw_ams_status)
    status = PrinterStatus.model_validate(
        {
            # Bambuddy ``bambu_mqtt.py`` L2819-L2831 (``tray_now``) and L3644-L3655 (the
            # AMS state is the second byte of ``ams_status``).
            "tray_now": block.get("tray_now") if isinstance(block, dict) else None,
            "ams_status_main": (
                (ams_status >> 8) & 0xFF if ams_status is not None and ams_status >= 0 else None
            ),
            "id": row.bambuddy_printer_id or 0,
            "name": row.name,
            "connected": is_fresh(report.received_at, now),
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
            "fila_switch": switch,
            "ams_extruder_map": extruders,
            "ams_switch_inlet": inlets,
            "extruder_loaded": extruder_loaded(data),
            "hms_errors": hms_errors(data.get("hms")),
        }
    )
