"""A printer's AMS slots over ScadBuddy's own LAN link (#7276, plan
``docs/superpowers/plans/2026-11-25-filament-hardware-off-bambuddy.md`` Task 8; the bodies,
views and checks are #7153's, moved from ``bambuddy/slot_control.py``).

Every write that reaches the printer checks the printer's stored LAN report first and
refuses what is unsafe (`guard`, R-7276-12); its commands are built here
(`configure_commands`, and the builders of `printers/lan_commands.py`) and run by the
``LanCommand`` workflow. A spool's preset becomes the slot's ``tray_info_idx`` and
``setting_id`` through the native catalogue (`slot_ids`, R-7276-15), never Bambuddy's id
of it. The configure's conversions port Bambuddy's configure route at the pinned commit
``d3be761f3bd0b6bac78045dc53599331d64c3672`` (P3):
https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/api/routes/printers.py#L3112-L3291
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import status as http
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice
from scadbuddy.bambuddy.extruders import extruder_of, two_nozzles
from scadbuddy.bambuddy.models import AmsTray, PresetRef, PrinterStatus
from scadbuddy.bambuddy.resolver import PRINTER_MODEL
from scadbuddy.bambuddy.trays import EXTERNAL, seen_trays, tray_at, tray_label
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.presets import preset_for
from scadbuddy.printers.lan_command_models import (
    LanCommandInput,
    LanCommandOutcome,
    LanCommandRun,
)
from scadbuddy.printers.lan_commands import (
    SlotParams,
    ams_filament_setting,
    extrusion_cali_sel,
    extrusion_cali_set_slot,
    wire_slot,
)
from scadbuddy.printers.lan_models import BUSY_STATES
from scadbuddy.slicing.own_presets import sb_id
from scadbuddy.workflows.print_models import LAN_REPLY_WAIT_AMS

#: A command that reaches the printer, each with its own checks.
Action = Literal["configure", "reset", "refresh", "load", "unload"]
NozzleDiameter = Literal["0.2", "0.4", "0.6", "0.8"]
#: ``tray_now`` when nothing is loaded (Bambuddy ``bambu_mqtt.py`` line 8590, v1.3.1).
NOTHING_LOADED = 255
#: The global tray of the external holders: Ext-L (or the only one) and Ext-R.
EXTERNAL_TRAY = 254
# Limit: an external holder's loaded state is `tray_now`'s alone (`is_loaded`): no cited
# source decodes its `snow` (`printers/status.py` `decode_snow`). Ext-R's global tray is
# 255, which is also NOTHING_LOADED, so `tray_now` cannot say Ext-R is loaded: it never
# shows `loaded`, `already_loaded` never fires for it, and its unload is refused
# `not_loaded`.
PRESET_ID = r"^[A-Za-z0-9_.+-]{0,64}$"
COLOUR = r"^#?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$"
#: As Bambuddy's ``AmsLabelBody.label`` (``schemas/printer.py`` line 364, v1.3.1).
LABEL_MAX = 100
#: The placeholder ``sequence_id`` of a builder's payload: ``lan_command`` replaces it.
SEQ = "0"
SENT = "sent"
UNCONFIRMED = "sent; the printer did not acknowledge it in time"


def _printable(value: str) -> str:
    if any(not ch.isprintable() for ch in value):
        raise ValueError("must hold printable characters only")
    return value


Printable64 = Annotated[str, Field(max_length=64), AfterValidator(_printable)]
Material = Annotated[str, Field(min_length=1, max_length=32), AfterValidator(_printable)]
Label = Annotated[str, Field(min_length=1, max_length=LABEL_MAX), AfterValidator(_printable)]
Temp = Annotated[int, Field(ge=0, le=500)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlotConfigureBody(_Strict):
    """A slot configured by hand: a ``spool_id`` (its values), the values themselves, or
    both, the values sent replacing the spool's."""

    spool_id: UUID | None = None
    material: Material | None = None
    colour: str | None = Field(default=None, pattern=COLOUR)
    #: The filament id the slot reports (``tray_info_idx``); empty takes the spool's
    #: preset's, else the material's generic.
    preset_id: str | None = Field(default=None, pattern=PRESET_ID)
    preset_name: Printable64 | None = None
    nozzle_temp_min: Temp | None = None
    nozzle_temp_max: Temp | None = None
    nozzle_diameter: NozzleDiameter | None = None
    cali_idx: int | None = Field(default=None, ge=-1, le=10_000)
    #: The K profile's setting id, sent with a new K value.
    setting_id: str | None = Field(default=None, pattern=PRESET_ID)
    k_value: float | None = Field(default=None, ge=0, lt=10)


PresetSource = Literal["cloud", "local", "builtin", "orca_cloud"]


class SlotConfigured(BaseModel):
    """What was sent to the slot."""

    ams_id: int
    tray_id: int
    spool_id: UUID | None
    material: str
    colour: str
    #: The slot's ``tray_info_idx``: the body's, the spool's preset's filament id, or the
    #: material's generic.
    preset_id: str
    preset_name: str
    #: Always ``None``: kept for the API's shape. It said where Bambuddy kept a preset.
    preset_source: PresetSource | None = None
    nozzle_temp_min: int
    nozzle_temp_max: int
    nozzle_diameter: str
    cali_idx: int | None
    k_value: float | None
    #: ``sent``, or that the printer did not acknowledge it in time.
    message: str = SENT


class SlotCommandSent(BaseModel):
    """A reset, refresh, load or unload sent; it says the command went out, not that the
    printer finished it."""

    ams_id: int
    tray_id: int
    action: Action
    message: str


class SlotPresetBody(_Strict):
    """The slicer preset a slot shows, kept by ScadBuddy."""

    preset_id: str = Field(min_length=1, max_length=64, pattern=PRESET_ID)
    preset_name: Annotated[str, Field(min_length=1, max_length=128), AfterValidator(_printable)]
    #: Accepted and ignored: nothing reads it (P17).
    preset_source: PresetSource = "cloud"
    #: The filament id the slot was configured with.
    tray_info_idx: str | None = Field(default=None, pattern=PRESET_ID)


class SavedSlotPreset(BaseModel):
    preset_id: str
    preset_name: str
    tray_info_idx: str | None = None


class AmsLabelBody(_Strict):
    label: Label


PlaceProblem = Literal["no_ams_kind", "no_printer"]


class AmsLabelSaved(BaseModel):
    """The unit's place, named by the label (R-7276-13). ``place_problem`` says why the
    place was not named: ``no_ams_kind`` (no location kind is marked AMS, so the unit has
    no place) or ``no_printer`` (the printer was deleted meanwhile). Two units may share a
    name: a printer's places are outside the name index."""

    ams_id: int
    label: str | None
    place: m.Location | None = None
    place_problem: PlaceProblem | None = None


class SlotView(BaseModel):
    tray_id: int
    #: ``AMS-A slot 1``, ``HT-A``, ``External slot 1``.
    label: str
    #: A spool is in it (the AMS's own flags).
    filled: bool
    #: An extruder feeds from it now (`is_loaded`).
    loaded: bool
    material: str | None
    colour: str | None
    preset_id: str | None
    preset_name: str | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    #: ScadBuddy's saved slot preset.
    saved_preset: SavedSlotPreset | None = None
    #: Load and unload address it.
    loadable: bool
    #: It has an RFID reader to refresh.
    refreshable: bool


class AmsUnitView(BaseModel):
    ams_id: int
    #: ``AMS-A``, ``HT-A``, ``External``.
    unit: str
    #: The unit's place name, when it is not the default ``<printer> <unit>``.
    label: str | None = None
    #: ScadBuddy's place of the unit, if made.
    place_id: UUID | None = None
    place_name: str | None = None
    slots: list[SlotView]


class AmsView(BaseModel):
    """A printer's AMS units and external holders, from its stored LAN report."""

    printer_id: str
    name: str
    #: The stored report is fresh.
    connected: bool
    state: str | None
    #: A print is active (``PREPARE``, ``SLICING``, ``RUNNING``, ``PAUSE``).
    printing: bool
    #: The AMS changes filament or reads a tag.
    ams_busy: bool
    tray_now: int | None
    units: list[AmsUnitView]


def refused(code: str, detail: str) -> ApiError:
    return ApiError(http.HTTP_409_CONFLICT, detail, code=code)


def invalid(detail: str) -> ApiError:
    return ApiError(http.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def known_slot(ams_id: int, tray_id: int) -> None:
    """422 for a unit or slot no printer has: an AMS 0-15 with slots 0-3, an AMS-HT
    128-135 with slot 0, the external holders 255 with slots 0-1."""
    if 0 <= ams_id <= 15:
        known = 0 <= tray_id <= 3
    elif 128 <= ams_id <= 135:
        known = tray_id == 0
    else:
        known = ams_id == EXTERNAL and tray_id in (0, 1)
    if not known:
        raise invalid(
            f"no printer has unit {ams_id} slot {tray_id}: an AMS is 0-15 with slots 0-3, an"
            " AMS-HT 128-135 with slot 0, and the external holders 255 with slots 0-1"
        )


def global_tray(ams_id: int, tray_id: int) -> int | None:
    """The global tray id of load, unload and ``tray_now`` (Bambuddy ``routes/printers.py``
    line 4767, v1.3.1), or ``None`` for a unit with none known (an AMS past 3)."""
    if ams_id == EXTERNAL:
        return EXTERNAL_TRAY + tray_id
    if 128 <= ams_id <= 135:
        return ams_id
    if 0 <= ams_id <= 3:
        return ams_id * 4 + tray_id
    return None


def filled(status: PrinterStatus, ams_id: int, tray_id: int) -> bool:
    return any((t.ams_id, t.tray_id) == (ams_id, tray_id) for t in seen_trays(status))


def ams_busy(status: PrinterStatus) -> bool:
    """Busy unless the AMS is idle (0) or in assist (3), which a running printer shows. 1
    (changing filament), 2 (reading a tag) and any value not seen yet count as busy, so an
    unknown state refuses the write. A report without the field (None) says nothing of the
    AMS."""
    return status.ams_status_main not in (None, 0, 3)


def is_loaded(status: PrinterStatus, ams_id: int, tray_id: int) -> bool:
    """An extruder feeds from the slot (P40). ``tray_now`` names only the active
    extruder's slot, so an AMS or AMS-HT slot is loaded when any extruder's decoded
    ``snow`` names it (`PrinterStatus.extruder_loaded`); with no such report, and for an
    external holder, when ``tray_now`` names it."""
    if ams_id != EXTERNAL and status.extruder_loaded:
        return (ams_id, tray_id) in status.extruder_loaded
    now = status.tray_now
    return now not in (None, NOTHING_LOADED) and now == global_tray(ams_id, tray_id)


def any_loaded(status: PrinterStatus) -> bool:
    """Some extruder holds filament: ``tray_now`` names a slot, or a decoded ``snow`` does."""
    return status.tray_now not in (None, NOTHING_LOADED) or any(
        slot is not None for slot in status.extruder_loaded
    )


def guard(status: PrinterStatus, action: Action, ams_id: int, tray_id: int) -> AmsTray:
    """The slot, once the stored report says ``action`` is safe now (R-7276-12); else a
    409 with a ``code``."""
    name = status.name or "the printer"
    slot = tray_label(ams_id, tray_id)
    if not status.connected:
        raise refused("printer_offline", f"ScadBuddy has no fresh report from {name}")
    if status.state in BUSY_STATES:
        raise refused(
            "printer_busy",
            f"{name} is printing ({status.state}); {action} the slot once the print has ended",
        )
    if ams_busy(status):
        raise refused(
            "ams_busy",
            f"{name}'s AMS is busy (it changes filament or reads a tag); try again once it is idle",
        )
    tray = tray_at(status, ams_id, tray_id)
    if tray is None:
        raise refused("no_slot", f"{name} reports no {slot}")
    here = is_loaded(status, ams_id, tray_id)
    if action == "load":
        if not filled(status, ams_id, tray_id):
            raise refused("tray_empty", f"{name}'s {slot} holds no spool to load")
        if here:
            raise refused("already_loaded", f"{name} is already loaded from {slot}")
    if action == "unload" and not here:
        raise refused("not_loaded", f"{name} is not loaded from {slot}")
    if action == "refresh" and any_loaded(status):
        raise refused(
            "filament_loaded",
            f"{name} has filament loaded; unload it before the AMS can read a tag again",
        )
    return tray


def _rgba(colour: str) -> str:
    """``#RRGGBBAA`` from ``RRGGBB`` or ``RRGGBBAA``, with or without ``#``."""
    hex_ = colour.lstrip("#").upper()
    return f"#{hex_}FF" if len(hex_) == 6 else f"#{hex_}"


def slot_extruder(status: PrinterStatus, ams_id: int, tray_id: int) -> int | None:
    """The extruder the slot feeds: `extruder_of`, else extruder 0 on a printer with one
    nozzle, whose report maps no AMS to an extruder."""
    extruder = extruder_of(ams_id, tray_id, status)
    if extruder is None and not two_nozzles(status):
        return 0
    return extruder


def nozzle_of(status: PrinterStatus, ams_id: int, tray_id: int) -> str | None:
    """The nozzle diameter of the extruder the slot feeds, as the report names it."""
    extruder = slot_extruder(status, ams_id, tray_id)
    if extruder is None or extruder >= len(status.nozzles):
        return None
    return status.nozzles[extruder].nozzle_diameter or None


@dataclass(frozen=True)
class SlotIds:
    """What a slot is configured with for a preset: its Bambu ``filament_id`` and its
    setting id, when the catalogue knows one."""

    tray_info_idx: str
    setting_id: str | None


def slot_ids(catalogue: Catalogue, slicer_filament: str) -> SlotIds | None:
    """The slot ids of a spool's preset ``slicer_filament`` on the native catalogue
    (`slicing/catalogue.py` ``catalogue_for(None, native=...)``, R-7276-15): a standard
    preset's name, a Bambu setting id (``native_setting_ids``), a BBL filament id (as a
    tray reports it), or a ScadBuddy preset's ``sb-<id>`` (offered by ``with_own``).
    ``None`` when the catalogue has no ``filament_id`` for it: a Bambuddy local id, a
    preset not offered, or one whose document names none."""
    ref = slicer_filament.strip()
    rows = catalogue.filament
    setting_ids = (catalogue.native_setting_ids or {}).get("filament", {})
    choice: PresetChoice | None
    setting_id: str | None = None
    if sb_id(ref) is not None:
        native = catalogue.native_ref(PresetRef(source="local", id=ref), "filament")
        choice = None if native is None else _row(rows, "local", native.id)
    elif ref in setting_ids:
        choice, setting_id = _row(rows, "standard", setting_ids[ref]), ref
    else:
        choice = _row(rows, "standard", ref)
        if choice is not None:
            setting_id = next((sid for sid, name in setting_ids.items() if name == ref), None)
        elif any(row.filament_id == ref for row in rows):
            return SlotIds(ref, None)
    if choice is None or not choice.filament_id:
        return None
    return SlotIds(choice.filament_id, setting_id)


def _row(rows: Sequence[PresetChoice], source: str, ref_id: str) -> PresetChoice | None:
    return next((r for r in rows if r.ref.source == source and r.ref.id == ref_id), None)


#: Bambu's generic filament id per material, for a slot with no preset of its own:
#: https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/utils/filament_ids.py#L48-L68
GENERIC_FILAMENT_IDS: dict[str, str] = {
    "PLA": "GFL99",
    "PETG": "GFG99",
    "ABS": "GFB99",
    "ASA": "GFB98",
    "PC": "GFC99",
    "PA": "GFN99",
    "NYLON": "GFN99",
    "TPU": "GFU99",
    "PVA": "GFS99",
    "HIPS": "GFS98",
    "PLA-CF": "GFL98",
    "PETG-CF": "GFG98",
    "PA-CF": "GFN98",
    "PETG HF": "GFG96",
    "PLA SILK": "GFL96",
    "PLA HIGH SPEED": "GFL95",
    "PCTG": "GFG97",
    "PE": "GFP99",
    "PP": "GFP97",
}


def generic_filament_id(material: str) -> str:
    """The generic for ``material``: its whole name, then its first word, as Bambuddy's
    configure route looks it up with no preset (L3172-L3187); ``""`` when none is known.
    Bambuddy's finish table and slot reuse are not ported (P3)."""
    name = material.upper().strip()
    return (
        GENERIC_FILAMENT_IDS.get(name)
        or GENERIC_FILAMENT_IDS.get(name.split("-")[0].split(" ")[0])
        or ""
    )


_SUNLU_FILAMENT_ID = re.compile(r"GFSNL(\d+)")


def filament_id_to_setting_id(filament_id: str) -> str:
    """A filament id's setting id (``GFL05`` to ``GFSL05``): a user preset (``P…``), a
    setting id already and anything unknown unchanged:
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/utils/filament_ids.py#L94-L124"""
    if not filament_id or filament_id.startswith("P"):
        return filament_id
    sunlu = _SUNLU_FILAMENT_ID.fullmatch(filament_id)
    if sunlu:
        return f"GFSNLS{sunlu.group(1)}"
    if filament_id.startswith("GF") and len(filament_id) >= 4:
        if filament_id[2] == "S" and filament_id[3].isalpha():
            return filament_id
        return f"GFS{filament_id[2:]}"
    return filament_id


def k_made_for(k: m.EffectiveKValue, slot_setting_id: str | None) -> bool:
    """The stored K row was calibrated for the slot's filament (P42). A row keeps no
    filament id, only its profile's ``setting_id`` (the printer's calibration table
    entry's, `filament/profile_fill.py`), which names the filament preset it was made
    with; it matches when that is the slot's setting id. A row with none is another
    filament's. The printer keys its calibrations by filament id and ``cali_idx``, so a
    profile of another filament selected on the slot is dropped (Bambuddy's configure
    route, which realigns the slot to the profile's filament instead:
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/api/routes/printers.py#L3190-L3223
    ). ScadBuddy keeps the slot's filament and sends no foreign ``cali_idx``."""
    return bool(k.setting_id) and k.setting_id == slot_setting_id


def configure_values(
    body: SlotConfigureBody,
    status: PrinterStatus,
    tray: AmsTray,
    ams_id: int,
    tray_id: int,
    *,
    printer: UUID,
    spool: m.Spool | None = None,
    presets: Sequence[m.EffectivePreset] = (),
    k_values: Sequence[m.EffectiveKValue] = (),
    catalogue: Catalogue | None = None,
) -> tuple[SlotParams, SlotConfigured]:
    """What a configure sends: the body's values over the spool's, the tray's
    temperatures where neither has one. The spool's preset for the nozzle gives the slot's
    ids through `slot_ids` on ``catalogue`` (409 ``no_preset`` when it has no filament id;
    503 with no catalogue); with no preset at all the material's generic is sent (P3).
    409 ``no_nozzle`` when the nozzle is not known; 422 naming what is still missing."""
    nozzle = body.nozzle_diameter or nozzle_of(status, ams_id, tray_id)
    if nozzle is None:
        raise refused(
            "no_nozzle",
            f"the report does not say which nozzle {tray_label(ams_id, tray_id)} feeds;"
            " send nozzle_diameter",
        )
    extruder = slot_extruder(status, ams_id, tray_id)
    material = body.material
    colour = body.colour
    preset_id = body.preset_id
    preset_name = body.preset_name
    low, high = body.nozzle_temp_min, body.nozzle_temp_max
    cali_idx, k_setting_id, k_value = body.cali_idx, body.setting_id, body.k_value
    preset_setting_id: str | None = None
    k: m.EffectiveKValue | None = None
    if spool is not None:
        preset = preset_for(presets, PRINTER_MODEL, nozzle)
        material = material or spool.names.material
        colour = colour or spool.effective.rgba
        if preset is not None and preset_id is None:
            if catalogue is None:
                raise ApiError(
                    http.HTTP_503_SERVICE_UNAVAILABLE,
                    "ScadBuddy has no slicer profiles here to read the spool preset's filament"
                    " id from; send preset_id",
                    code="presets_unavailable",
                )
            ids = slot_ids(catalogue, preset.slicer_filament)
            if ids is None:
                raise refused(
                    "no_preset",
                    f"the spool's preset {preset.slicer_filament_name or preset.slicer_filament}"
                    " has no Bambu filament id ScadBuddy knows; send preset_id",
                )
            preset_id, preset_setting_id = ids.tray_info_idx, ids.setting_id
        if preset_name is None:
            preset_name = (
                preset.slicer_filament_name if preset is not None else None
            ) or spool.names.subtype
        low = low if low is not None else spool.effective.nozzle_temp_min
        high = high if high is not None else spool.effective.nozzle_temp_max
        k = next(
            (
                row
                for row in k_values
                if row.printer_id == printer
                and row.extruder == extruder
                and row.nozzle_diameter == nozzle
            ),
            None,
        )
        if k is not None and (cali_idx is not None or k_value is not None):
            k = None
    if low is None and tray.nozzle_temp_min:
        low = tray.nozzle_temp_min
    if high is None and tray.nozzle_temp_max:
        high = tray.nozzle_temp_max
    missing = [
        name
        for name, value in (
            ("material", material),
            ("colour", colour),
            ("nozzle_temp_min", low),
            ("nozzle_temp_max", high),
        )
        if not value
    ]
    if missing:
        raise invalid(
            f"the slot needs {', '.join(missing)}: send them, or a spool_id whose spool has them"
        )
    assert material and colour and low is not None and high is not None
    if low > high:
        raise invalid("nozzle_temp_min is above nozzle_temp_max")
    rgba = _rgba(colour)
    name = preset_name or material
    tray_info_idx = preset_id or generic_filament_id(material)
    slot_setting_id = preset_setting_id or filament_id_to_setting_id(tray_info_idx) or None
    if k is not None:
        k_value = float(k.k_value)
        if k_made_for(k, slot_setting_id):
            cali_idx, k_setting_id = k.cali_idx, k_setting_id or k.setting_id
        # Else the stored profile was calibrated for another filament: selecting it under
        # this one would not stick (P42), so the value is sent as a new K instead.
    params = SlotParams(
        tray_info_idx=tray_info_idx,
        tray_type=material,
        tray_sub_brands=name,
        tray_color=rgba.lstrip("#"),
        nozzle_temp_min=low,
        nozzle_temp_max=high,
        nozzle_diameter=nozzle,
        cali_idx=-1 if cali_idx is None and k_value is not None else cali_idx,
        # #2604's back-fill (L3227-L3237): a filament id with no setting id leaves the slot
        # half configured.
        preset_setting_id=slot_setting_id,
        k_setting_id=k_setting_id,
        k_value=k_value,
    )
    sent = SlotConfigured(
        ams_id=ams_id,
        tray_id=tray_id,
        spool_id=None if spool is None else spool.id,
        material=material,
        colour=rgba,
        preset_id=tray_info_idx,
        preset_name=name,
        nozzle_temp_min=low,
        nozzle_temp_max=high,
        nozzle_diameter=nozzle,
        cali_idx=params.cali_idx,
        k_value=k_value,
    )
    return params, sent


def write(printer_id: str, name: str, payload: dict[str, Any]) -> LanCommandInput:
    """One slot command: a write that waits `LAN_REPLY_WAIT_AMS` for its reply."""
    return LanCommandInput(
        printer_id=printer_id, name=name, payload=payload, wait_s=LAN_REPLY_WAIT_AMS, kind="write"
    )


def configure_commands(
    params: SlotParams, ams_id: int, tray_id: int, *, external_count: int, printer_id: str
) -> list[LanCommandInput]:
    """A configure's commands in Bambuddy's order (``routes/printers.py`` L3239-L3291):
    ``ams_filament_setting`` with the preset's setting id, then ``extrusion_cali_sel``
    (``cali_idx`` or -1), then ``extrusion_cali_set`` of the slot only with a K value above
    0 and no profile selected; all writes. ``external_count`` is ``len(status.vt_tray)``
    (P1, P26). ``UnsupportedUnitError`` for a unit no builder addresses, and for a K set
    on an AMS 4-15 (P22)."""
    cali_idx = -1 if params.cali_idx is None else params.cali_idx
    commands = [
        write(
            printer_id,
            "ams_filament_setting",
            ams_filament_setting(
                wire_slot(ams_id, tray_id, external_count=external_count),
                tray_info_idx=params.tray_info_idx,
                tray_type=params.tray_type,
                tray_sub_brands=params.tray_sub_brands,
                tray_color=params.tray_color,
                nozzle_temp_min=params.nozzle_temp_min,
                nozzle_temp_max=params.nozzle_temp_max,
                setting_id=params.preset_setting_id,
                seq=SEQ,
            ),
        ),
        write(
            printer_id,
            "extrusion_cali_sel",
            extrusion_cali_sel(
                ams_id,
                tray_id,
                external_count=external_count,
                cali_idx=cali_idx,
                filament_id=params.tray_info_idx,
                nozzle_diameter=params.nozzle_diameter,
                seq=SEQ,
            ),
        ),
    ]
    if params.k_value is not None and params.k_value > 0 and cali_idx < 0:
        commands.append(
            write(
                printer_id,
                "extrusion_cali_set",
                extrusion_cali_set_slot(
                    ams_id,
                    tray_id,
                    k_value=params.k_value,
                    nozzle_diameter=params.nozzle_diameter,
                    filament_id=params.tray_info_idx,
                    setting_id=params.k_setting_id or "",
                    name=params.tray_sub_brands,
                    seq=SEQ,
                ),
            )
        )
    return commands


def _temp(value: Any) -> int:
    """A report temperature in degrees: ``device.extruder.info[].temp`` packs the target
    above the current (``target * 65536 + current``) once the heater is on."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    current = int(value) % 65536 if value > 500 else int(value)
    return current if -50 < current < 500 else 0


def nozzle_temps(report: Mapping[str, Any]) -> tuple[int, int]:
    """The temperatures an unload and an Ext-R load are sent at, as Bambuddy reads them
    (``nozzle`` and ``nozzle_2``): on a printer with two extruders in
    ``device.extruder.info``, the left's (id 1) and the right's (id 0), else
    ``nozzle_temper`` and none; 0 for one not reported, which the builders replace with
    their defaults:
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L5181-L5330"""
    device = report.get("device")
    extruder = device.get("extruder") if isinstance(device, dict) else None
    info = extruder.get("info") if isinstance(extruder, dict) else None
    if isinstance(info, list) and len(info) >= 2 and all(isinstance(e, dict) for e in info[:2]):
        return _temp(info[1].get("temp")), _temp(info[0].get("temp"))
    return _temp(report.get("nozzle_temper")), 0


def sent_message(outcome: LanCommandOutcome) -> str:
    return SENT if all(reply.confirmed for reply in outcome.replies) else UNCONFIRMED


def slot_view(run: LanCommandRun, outcome: LanCommandOutcome) -> BaseModel:
    """A slot route's body from its run (``LanCommandRun.answer``) and the outcome:
    `SlotCommandSent` for an answer that names its ``action``, else `SlotConfigured`
    (P37)."""
    if run.answer is None:
        return outcome
    body = {**run.answer, "message": sent_message(outcome)}
    if "action" in body:
        return SlotCommandSent.model_validate(body)
    return SlotConfigured.model_validate(body)
