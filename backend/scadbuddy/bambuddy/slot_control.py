"""A printer's AMS slots through Bambuddy (#7153, plan
``docs/superpowers/plans/2026-11-23-ams-slot-control.md``).

The printer is SB's registry row, linked to a Bambuddy printer (R-7153-2). Every write that
reaches the printer reads Bambuddy's live status first and refuses what is unsafe
(`guard`, R-7153-3). Bambuddy's parameters are ``backend/app/api/routes/printers.py``
(maziggy/bambuddy v1.3.1)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Annotated, Literal
from uuid import UUID

from fastapi import status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.extruders import extruder_of
from scadbuddy.bambuddy.models import AmsTray, PrinterStatus, SlotConfigure
from scadbuddy.bambuddy.resolver import PRINTER_MODEL
from scadbuddy.bambuddy.trays import EXTERNAL, seen_trays, tray_at, tray_label
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.presets import preset_for
from scadbuddy.printers.lan_models import BUSY_STATES
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.store import PrinterStore, printer_uuid

#: A command that reaches the printer, each with its own checks (R-7153-3).
Action = Literal["configure", "reset", "refresh", "load", "unload"]
NozzleDiameter = Literal["0.2", "0.4", "0.6", "0.8"]
#: ``tray_now`` when nothing is loaded (``bambu_mqtt.py`` line 8590, v1.3.1).
NOTHING_LOADED = 255
#: The global tray of the external holders: Ext-L (or the only one) and Ext-R.
EXTERNAL_TRAY = 254
# Limit: Ext-R's global tray is 255, which is also NOTHING_LOADED, so `tray_now` cannot say
# Ext-R is loaded: it never shows `loaded`, and `already_loaded` never fires for it. The
# status's `extruder_slots` names each extruder's slot, but the only recording of it
# (`printer-status.json`) shows Ext-R's empty form (ams 254, slot 255), not a loaded one,
# so the two are not told apart here. A Bambuddy-routed stopgap, ported in #7276.
PRESET_ID = r"^[A-Za-z0-9_.+-]{0,64}$"
COLOUR = r"^#?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$"
#: Bambuddy's ``AmsLabelBody.label`` (``schemas/printer.py`` line 364, v1.3.1).
LABEL_MAX = 100


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
    """A slot configured by hand (R-7153-6): a ``spool_id`` (its values), the values
    themselves, or both, the values sent replacing the spool's."""

    spool_id: UUID | None = None
    material: Material | None = None
    colour: str | None = Field(default=None, pattern=COLOUR)
    #: The filament id or preset the slot reports (``tray_info_idx``); empty lets
    #: Bambuddy pick the slot's or the material's generic.
    preset_id: str | None = Field(default=None, pattern=PRESET_ID)
    preset_name: Printable64 | None = None
    nozzle_temp_min: Temp | None = None
    nozzle_temp_max: Temp | None = None
    nozzle_diameter: NozzleDiameter | None = None
    cali_idx: int | None = Field(default=None, ge=-1, le=10_000)
    setting_id: str | None = Field(default=None, pattern=PRESET_ID)
    k_value: float | None = Field(default=None, ge=0, lt=10)


class SlotConfigured(BaseModel):
    """What was sent to the slot."""

    ams_id: int
    tray_id: int
    spool_id: UUID | None
    material: str
    colour: str
    preset_id: str
    preset_name: str
    nozzle_temp_min: int
    nozzle_temp_max: int
    nozzle_diameter: str
    cali_idx: int | None
    k_value: float | None


class SlotCommandSent(BaseModel):
    """A reset, refresh, load or unload Bambuddy sent; it says the command went out, not
    that the printer finished it."""

    ams_id: int
    tray_id: int
    action: Action
    message: str


class SlotPresetBody(_Strict):
    """The slicer preset a slot shows in Bambuddy (R-7153-7)."""

    preset_id: str = Field(min_length=1, max_length=64, pattern=PRESET_ID)
    preset_name: Annotated[str, Field(min_length=1, max_length=128), AfterValidator(_printable)]
    preset_source: Literal["cloud", "local", "builtin", "orca_cloud"] = "cloud"
    #: The filament id the slot was configured with, so Bambuddy notices a later
    #: configure from elsewhere (``routes/printers.py`` line 2745, v1.3.1).
    tray_info_idx: str | None = Field(default=None, max_length=32, pattern=PRESET_ID)


class SavedSlotPreset(BaseModel):
    preset_id: str
    preset_name: str
    tray_info_idx: str | None = None


class AmsLabelBody(_Strict):
    label: Label


PlaceProblem = Literal["no_ams_kind"]


class AmsLabelSaved(BaseModel):
    """Bambuddy's label, then SB's place of the unit (R-7153-8). ``place_problem`` says
    why the place was not named: ``no_ams_kind`` (no location kind is marked AMS, so the
    unit has no place). Two units may share a name: a printer's places are outside the
    name index."""

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
    #: The printer feeds from it now (``tray_now``).
    loaded: bool
    material: str | None
    colour: str | None
    preset_id: str | None
    preset_name: str | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    #: Bambuddy's saved slot preset (R-7153-7).
    saved_preset: SavedSlotPreset | None = None
    #: Bambuddy's load and unload address it (R-7153-2).
    loadable: bool
    #: It has an RFID reader to refresh.
    refreshable: bool


class AmsUnitView(BaseModel):
    ams_id: int
    #: ``AMS-A``, ``HT-A``, ``External``.
    unit: str
    #: Bambuddy's label, if any.
    label: str | None = None
    #: SB's place of the unit, if made.
    place_id: UUID | None = None
    place_name: str | None = None
    slots: list[SlotView]


class AmsView(BaseModel):
    """A printer's AMS units and external holder, from Bambuddy's live status (R-7153-9)."""

    printer_id: str
    name: str
    connected: bool
    state: str | None
    #: A print is active (``PREPARE``, ``SLICING``, ``RUNNING``, ``PAUSE``).
    printing: bool
    #: The AMS changes filament or reads a tag.
    ams_busy: bool
    tray_now: int | None
    units: list[AmsUnitView]


def refused(code: str, detail: str) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, detail, code=code)


def invalid(detail: str) -> ApiError:
    return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


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
    """Bambuddy's global tray id for load and unload (``routes/printers.py`` line 4767,
    v1.3.1), or ``None`` for a unit it cannot address (an AMS past 3)."""
    if ams_id == EXTERNAL:
        return EXTERNAL_TRAY + tray_id
    if 128 <= ams_id <= 135:
        return ams_id
    if 0 <= ams_id <= 3:
        return ams_id * 4 + tray_id
    return None


def linked_row(row: PrinterRow | None, printer_id: str) -> int:
    """The Bambuddy printer a registry row links (R-7153-2): 404 for no row, 409 for a
    Bambuddy virtual printer or a row with no Bambuddy link."""
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no printer {printer_id}")
    if row.kind != "physical":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{row.name} is a Bambuddy virtual printer; it has no AMS of its own",
        )
    if row.bambuddy_printer_id is None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{row.name} is not linked to a Bambuddy printer, so ScadBuddy cannot reach its AMS",
        )
    return row.bambuddy_printer_id


async def read_row(printers: PrinterStore | None, printer_id: str) -> tuple[PrinterRow, int]:
    row = None
    if printers is not None and printer_uuid(printer_id) is not None:
        row = await asyncio.to_thread(printers.get, printer_id)
    bambuddy_id = linked_row(row, printer_id)
    assert row is not None
    return row, bambuddy_id


def filled(status: PrinterStatus, ams_id: int, tray_id: int) -> bool:
    return any((t.ams_id, t.tray_id) == (ams_id, tray_id) for t in seen_trays(status))


def ams_busy(status: PrinterStatus) -> bool:
    """1 (changing filament) and 2 (reading a tag) only. 3 is the AMS assist, which the
    recording shows on a running printer, so it is not busy by itself."""
    return status.ams_status_main in (1, 2)


def guard(status: PrinterStatus, action: Action, ams_id: int, tray_id: int) -> AmsTray:
    """The slot, once the status says ``action`` is safe now (R-7153-3); else a 409 with
    a ``code``."""
    name = status.name or f"printer {status.id}"
    slot = tray_label(ams_id, tray_id)
    if not status.connected:
        raise refused("printer_offline", f"Bambuddy is not connected to {name}")
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
    loaded = status.tray_now not in (None, NOTHING_LOADED)
    if action == "load":
        if not filled(status, ams_id, tray_id):
            raise refused("tray_empty", f"{name}'s {slot} holds no spool to load")
        if loaded and status.tray_now == global_tray(ams_id, tray_id):
            raise refused("already_loaded", f"{name} is already loaded from {slot}")
    if action == "refresh" and loaded:
        raise refused(
            "filament_loaded",
            f"{name} has filament loaded (tray {status.tray_now}); unload it before the"
            " AMS can read a tag again",
        )
    return tray


def _rgba(colour: str) -> str:
    """``#RRGGBBAA`` from ``RRGGBB`` or ``RRGGBBAA``, with or without ``#``."""
    hex_ = colour.lstrip("#").upper()
    return f"#{hex_}FF" if len(hex_) == 6 else f"#{hex_}"


def nozzle_of(status: PrinterStatus, ams_id: int, tray_id: int) -> str | None:
    """The nozzle diameter of the extruder the slot feeds, as Bambuddy's status names it."""
    extruder = extruder_of(ams_id, tray_id, status)
    if extruder is None or extruder >= len(status.nozzles):
        return None
    return status.nozzles[extruder].nozzle_diameter or None


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
) -> tuple[SlotConfigure, SlotConfigured]:
    """What a configure sends (R-7153-6): the body's values over the spool's, the tray's
    temperatures where neither has one. 409 ``no_nozzle`` when the nozzle is not known;
    422 naming what is still missing."""
    nozzle = body.nozzle_diameter or nozzle_of(status, ams_id, tray_id)
    if nozzle is None:
        raise refused(
            "no_nozzle",
            f"Bambuddy's status does not say which nozzle {tray_label(ams_id, tray_id)}"
            " feeds; send nozzle_diameter",
        )
    extruder = extruder_of(ams_id, tray_id, status)
    material = body.material
    colour = body.colour
    preset_id = body.preset_id
    preset_name = body.preset_name
    low, high = body.nozzle_temp_min, body.nozzle_temp_max
    cali_idx, setting_id, k_value = body.cali_idx, body.setting_id, body.k_value
    if spool is not None:
        preset = preset_for(presets, PRINTER_MODEL, nozzle)
        ref = None if preset is None else preset.for_bambuddy()
        material = material or spool.names.material
        colour = colour or spool.effective.rgba
        if preset_id is None and ref is not None:
            preset_id = ref
        if preset_name is None:
            preset_name = (
                preset.slicer_filament_name if preset is not None and ref is not None else None
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
        if k is not None and cali_idx is None and k_value is None:
            cali_idx = k.cali_idx
            setting_id = setting_id or k.setting_id
            k_value = float(k.k_value)
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
    params = SlotConfigure(
        tray_info_idx=preset_id or "",
        tray_type=material,
        tray_sub_brands=name,
        tray_color=rgba.lstrip("#"),
        nozzle_temp_min=low,
        nozzle_temp_max=high,
        cali_idx=-1 if cali_idx is None and k_value is not None else cali_idx,
        nozzle_diameter=nozzle,
        setting_id=setting_id,
        k_value=k_value,
    )
    sent = SlotConfigured(
        ams_id=ams_id,
        tray_id=tray_id,
        spool_id=None if spool is None else spool.id,
        material=material,
        colour=rgba,
        preset_id=preset_id or "",
        preset_name=name,
        nozzle_temp_min=low,
        nozzle_temp_max=high,
        nozzle_diameter=nozzle,
        cali_idx=params.cali_idx,
        k_value=k_value,
    )
    return params, sent
