"""A registry printer's AMS slots through Bambuddy (#7153, plan
``docs/superpowers/plans/2026-11-23-ams-slot-control.md``).

The printer is SB's registry row, linked to a Bambuddy printer (R-7153-2). Each write that
reaches the printer (configure, reset, refresh, load, unload) reads Bambuddy's live status
first and refuses with a 409 and a ``code`` what is unsafe now (R-7153-3). The routes call
Bambuddy in the request, as the SpoolBuddy tare does (R-7153-5). Only the AMS label routes
write a ScadBuddy row (the unit's place), so only they nudge the filament sync; the rest
are in `api/filament.py` `NOT_NUDGED`.
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from fastapi import status as http

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.api.filament import nudge_after_write
from scadbuddy.bambuddy import slot_control as sc
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.extruders import extruder_of
from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.bambuddy.trays import (
    EXTERNAL,
    every_tray,
    seen_trays,
    tray_label,
    tray_rgba,
    unit_label,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogRefError
from scadbuddy.filament.component import FilamentDep
from scadbuddy.filament.locations import NoAmsKindError
from scadbuddy.filament.presets import effective_k_values, effective_presets
from scadbuddy.printers.component import PrintersDep
from scadbuddy.printers.lan_models import BUSY_STATES

router = APIRouter(prefix="/printers", tags=["printers"], dependencies=[Depends(nudge_after_write)])

AmsId = Annotated[int, Path(ge=0, le=255)]
TrayId = Annotated[int, Path(ge=0, le=15)]
SLOT = "/{printer_id}/trays/{ams_id}/{tray_id}"
STATE_FIRST = (
    " It reads Bambuddy's status of the printer first and refuses with a 409 and a `code`:"
    " `printer_offline`, `printer_busy` (a print is active), `ams_busy` (the AMS changes"
    " filament or reads a tag), `no_slot`."
)
PRINTER = (
    " 404 for an unknown printer; 409 for a Bambuddy virtual printer or a printer not linked"
    " to Bambuddy; 422 for a unit or slot no printer has. Needs the Bambuddy key's Control"
    " Printer scope."
)
ADMIN = (
    " Bambuddy denies this write to every API key while its auth is on: then it is the scope"
    " problem naming `Printers (admin only)`."
)


async def _status(client: BambuddyClient, bambuddy_id: int) -> PrinterStatus:
    return await client.printer_status(bambuddy_id)


async def _command(
    printers: PrintersDep,
    store: SettingsStoreDep,
    printer_id: str,
    ams_id: int,
    tray_id: int,
    action: sc.Action,
) -> sc.SlotCommandSent:
    sc.known_slot(ams_id, tray_id)
    tray = sc.global_tray(ams_id, tray_id)
    if action in ("load", "unload") and tray is None:
        raise sc.invalid(
            f"Bambuddy cannot load or unload AMS {ams_id}: only AMS 0-3 have a tray id"
        )
    if action == "refresh" and ams_id == EXTERNAL:
        raise sc.invalid("an external holder has no RFID reader to refresh")
    _, bambuddy_id = await sc.read_row(printers.store, printer_id)
    async with client_for(await asyncio.to_thread(store.load)) as client:
        status = await _status(client, bambuddy_id)
        sc.guard(status, action, ams_id, tray_id)
        if action == "reset":
            answer = await client.reset_slot(bambuddy_id, ams_id, tray_id)
        elif action == "refresh":
            answer = await client.refresh_slot(bambuddy_id, ams_id, tray_id)
        elif action == "load":
            assert tray is not None
            # Only with the Filament Track Switch does Bambuddy need the hotend
            # (``routes/printers.py`` line 4770, v1.3.1).
            switch = status.fila_switch is not None and status.fila_switch.installed
            extruder = extruder_of(ams_id, tray_id, status) if switch else None
            if switch and extruder is None:
                raise sc.refused(
                    "extruder_unknown",
                    "the printer has the Filament Track Switch and its status does not say"
                    f" which hotend {tray_label(ams_id, tray_id)} feeds",
                )
            answer = await client.load_filament(bambuddy_id, tray, extruder_id=extruder)
        else:
            assert tray is not None
            answer = await client.unload_filament(bambuddy_id, tray)
    return sc.SlotCommandSent(ams_id=ams_id, tray_id=tray_id, action=action, message=answer.message)


@router.get(
    "/{printer_id}/ams",
    response_model=sc.AmsView,
    summary="A printer's AMS slots",
    description=(
        "Each AMS unit and the external holder from Bambuddy's live status: every slot's"
        " material, colour, preset, temperatures, whether it holds a spool and whether the"
        " printer is loaded from it, Bambuddy's saved slot preset, each unit's Bambuddy label"
        " and ScadBuddy place, and the printer's state (`printing`, `ams_busy`), so a client"
        " can tell what a write would refuse."
        + PRINTER.replace(" Needs the Bambuddy key's Control Printer scope.", " Needs Read Status.")
    ),
)
async def get_printer_ams(
    printers: PrintersDep, store: SettingsStoreDep, filament: FilamentDep, printer_id: str
) -> sc.AmsView:
    row, bambuddy_id = await sc.read_row(printers.store, printer_id)
    async with client_for(await asyncio.to_thread(store.load)) as client:
        status, presets, labels = await asyncio.gather(
            _status(client, bambuddy_id),
            client.slot_presets(bambuddy_id),
            client.ams_labels(bambuddy_id),
        )
    places = await asyncio.to_thread(filament.locations.ams_units, UUID(row.id))
    saved = {(p.ams_id, p.tray_id): p for p in presets}
    held = {(t.ams_id, t.tray_id) for t in seen_trays(status)}
    loaded = status.tray_now if status.tray_now not in (None, sc.NOTHING_LOADED) else None
    units: dict[int, list[sc.SlotView]] = {}
    for ams_id, tray in every_tray(status):
        preset = saved.get((ams_id, tray.id))
        global_id = sc.global_tray(ams_id, tray.id)
        units.setdefault(ams_id, []).append(
            sc.SlotView(
                tray_id=tray.id,
                label=tray_label(ams_id, tray.id),
                filled=(ams_id, tray.id) in held,
                loaded=loaded is not None and loaded == global_id,
                material=(tray.tray_type or "").strip() or None,
                colour=tray_rgba(tray.tray_color),
                preset_id=(tray.tray_info_idx or "").strip() or None,
                preset_name=(tray.tray_sub_brands or "").strip() or None,
                nozzle_temp_min=tray.nozzle_temp_min or None,
                nozzle_temp_max=tray.nozzle_temp_max or None,
                saved_preset=None
                if preset is None
                else sc.SavedSlotPreset(
                    preset_id=preset.preset_id,
                    preset_name=preset.preset_name,
                    tray_info_idx=preset.tray_info_idx,
                ),
                loadable=global_id is not None,
                refreshable=ams_id != EXTERNAL,
            )
        )
    return sc.AmsView(
        printer_id=row.id,
        name=row.name,
        connected=status.connected,
        state=status.state,
        printing=status.state in BUSY_STATES,
        ams_busy=sc.ams_busy(status),
        tray_now=status.tray_now,
        units=[
            sc.AmsUnitView(
                ams_id=ams_id,
                unit=unit_label(ams_id),
                label=labels.get(ams_id),
                place_id=None if ams_id not in places else places[ams_id].id,
                place_name=None if ams_id not in places else places[ams_id].name,
                slots=sorted(slots, key=lambda s: s.tray_id),
            )
            for ams_id, slots in sorted(units.items(), key=lambda u: (u[0] == EXTERNAL, u[0]))
        ],
    )


@router.post(
    f"{SLOT}/configure",
    response_model=sc.SlotConfigured,
    summary="Configure an AMS slot by hand",
    description=(
        "Sets the slot's material, colour, preset and temperatures (and K, when given) through"
        " Bambuddy's slot configure. Send a `spool_id` for any ScadBuddy spool's values (its"
        " type's preset for the slot's nozzle and its K value), the values themselves, or"
        " both: what is sent replaces the spool's, and the tray's own temperatures fill in."
        " The nozzle is `nozzle_diameter`, else the one Bambuddy's status names for the slot"
        " (409 `no_nozzle` when neither). 404 for an unknown spool; 422 naming what is still"
        " missing. It links no spool to the tray. Answers what was sent." + STATE_FIRST + PRINTER
    ),
)
async def configure_printer_slot(
    printers: PrintersDep,
    store: SettingsStoreDep,
    filament: FilamentDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    body: sc.SlotConfigureBody,
) -> sc.SlotConfigured:
    sc.known_slot(ams_id, tray_id)
    row, bambuddy_id = await sc.read_row(printers.store, printer_id)
    spool: m.Spool | None = None
    presets: list[m.EffectivePreset] = []
    k_values: list[m.EffectiveKValue] = []
    if body.spool_id is not None:
        spool = await asyncio.to_thread(filament.spools.get, body.spool_id)
        if spool is None:
            raise ApiError(http.HTTP_404_NOT_FOUND, f"no spool {body.spool_id}")
        presets, k_values = await asyncio.to_thread(_spool_presets, filament, body.spool_id)
    async with client_for(await asyncio.to_thread(store.load)) as client:
        status = await _status(client, bambuddy_id)
        tray = sc.guard(status, "configure", ams_id, tray_id)
        params, sent = sc.configure_values(
            body,
            status,
            tray,
            ams_id,
            tray_id,
            printer=UUID(row.id),
            spool=spool,
            presets=presets,
            k_values=k_values,
        )
        await client.configure_slot(bambuddy_id, ams_id, tray_id, params)
    return sent


def _spool_presets(
    filament: FilamentDep, spool_id: UUID
) -> tuple[list[m.EffectivePreset], list[m.EffectiveKValue]]:
    with filament.spools.transaction() as conn:
        return effective_presets(conn, spool_id), effective_k_values(conn, spool_id)


@router.post(
    f"{SLOT}/reset",
    response_model=sc.SlotCommandSent,
    summary="Reset an AMS slot",
    description=(
        "Clears the slot's filament setting on the printer and Bambuddy's saved slot preset."
        + STATE_FIRST
        + PRINTER
    ),
)
async def reset_printer_slot(
    printers: PrintersDep,
    store: SettingsStoreDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
) -> sc.SlotCommandSent:
    return await _command(printers, store, printer_id, ams_id, tray_id, "reset")


@router.post(
    f"{SLOT}/refresh",
    response_model=sc.SlotCommandSent,
    summary="Read an AMS slot's tag again",
    description=(
        "The AMS reads the slot's RFID tag again. Not for an external holder (422). Also 409"
        " `filament_loaded` while filament is loaded, as the AMS must move it."
        + STATE_FIRST
        + PRINTER
    ),
)
async def refresh_printer_slot(
    printers: PrintersDep,
    store: SettingsStoreDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
) -> sc.SlotCommandSent:
    return await _command(printers, store, printer_id, ams_id, tray_id, "refresh")


@router.post(
    f"{SLOT}/load",
    response_model=sc.SlotCommandSent,
    summary="Load filament from an AMS slot",
    description=(
        "The printer feeds the slot's filament to its hotend. AMS 0-3, an AMS-HT or an"
        " external holder (422 for an AMS past 3). Also 409 `tray_empty` (no spool in it),"
        " `already_loaded` (the printer feeds from it now) and `extruder_unknown` (with the"
        " Filament Track Switch, the status does not say which hotend it feeds)."
        + STATE_FIRST
        + PRINTER
    ),
)
async def load_printer_slot(
    printers: PrintersDep,
    store: SettingsStoreDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
) -> sc.SlotCommandSent:
    return await _command(printers, store, printer_id, ams_id, tray_id, "load")


@router.post(
    f"{SLOT}/unload",
    response_model=sc.SlotCommandSent,
    summary="Unload filament fed from an AMS slot",
    description=(
        "The printer pulls back the filament its hotend takes from the slot. Bambuddy's 409"
        " when no hotend is loaded from it." + STATE_FIRST + PRINTER
    ),
)
async def unload_printer_slot(
    printers: PrintersDep,
    store: SettingsStoreDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
) -> sc.SlotCommandSent:
    return await _command(printers, store, printer_id, ams_id, tray_id, "unload")


@router.put(
    f"{SLOT}/preset",
    response_model=sc.SavedSlotPreset,
    summary="Save the slicer preset an AMS slot shows",
    description=(
        "Bambuddy's slot preset: kept in its database, not on the printer, so no print state"
        " is read. Send the `tray_info_idx` the slot was configured with." + ADMIN + PRINTER
    ),
)
async def set_printer_slot_preset(
    printers: PrintersDep,
    store: SettingsStoreDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    body: sc.SlotPresetBody,
) -> sc.SavedSlotPreset:
    sc.known_slot(ams_id, tray_id)
    _, bambuddy_id = await sc.read_row(printers.store, printer_id)
    async with client_for(await asyncio.to_thread(store.load)) as client:
        saved = await client.set_slot_preset(
            bambuddy_id,
            ams_id,
            tray_id,
            preset_id=body.preset_id,
            preset_name=body.preset_name,
            preset_source=body.preset_source,
            tray_info_idx=body.tray_info_idx,
        )
    return sc.SavedSlotPreset(
        preset_id=saved.preset_id, preset_name=saved.preset_name, tray_info_idx=saved.tray_info_idx
    )


@router.delete(
    f"{SLOT}/preset",
    status_code=http.HTTP_204_NO_CONTENT,
    summary="Clear the slicer preset an AMS slot shows",
    description="One that is not there is cleared all the same." + ADMIN + PRINTER,
)
async def clear_printer_slot_preset(
    printers: PrintersDep,
    store: SettingsStoreDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
) -> None:
    sc.known_slot(ams_id, tray_id)
    _, bambuddy_id = await sc.read_row(printers.store, printer_id)
    async with client_for(await asyncio.to_thread(store.load)) as client:
        await client.delete_slot_preset(bambuddy_id, ams_id, tray_id)


def _unit(ams_id: int) -> None:
    if not (0 <= ams_id <= 15 or 128 <= ams_id <= 135):
        raise sc.invalid(f"no printer has AMS {ams_id}: an AMS is 0-15, an AMS-HT 128-135")


async def _serial(client: BambuddyClient, bambuddy_id: int, ams_id: int) -> str:
    status = await _status(client, bambuddy_id)
    unit = next((u for u in status.ams if u.id == ams_id), None)
    return "" if unit is None else (unit.serial_number or "").strip()[:50]


def _name_place(
    filament: FilamentDep, printer: UUID, ams_id: int, label: str | None
) -> tuple[m.Location | None, sc.PlaceProblem | None]:
    try:
        return filament.locations.name_ams_unit(printer, ams_id, label), None
    except NoAmsKindError:
        return None, "no_ams_kind"
    except CatalogRefError:
        return None, None


@router.put(
    "/{printer_id}/ams/{ams_id}/label",
    response_model=sc.AmsLabelSaved,
    summary="Label an AMS unit",
    description=(
        "Writes Bambuddy's AMS label (under the unit's serial, from Bambuddy's status), then"
        " names ScadBuddy's place of the unit the same, making it if missing. A refused"
        " Bambuddy write changes nothing. Once Bambuddy's label is written the answer is 200;"
        " `place_problem` `no_ams_kind` says the place was not made: no location kind is"
        " marked AMS."
        + ADMIN
        + " 404 for an unknown printer; 409 for a virtual or unlinked printer; 422 for a unit"
        " no printer has."
    ),
)
async def set_printer_ams_label(
    printers: PrintersDep,
    store: SettingsStoreDep,
    filament: FilamentDep,
    printer_id: str,
    ams_id: AmsId,
    body: sc.AmsLabelBody,
) -> sc.AmsLabelSaved:
    _unit(ams_id)
    row, bambuddy_id = await sc.read_row(printers.store, printer_id)
    async with client_for(await asyncio.to_thread(store.load)) as client:
        serial = await _serial(client, bambuddy_id, ams_id)
        await client.set_ams_label(bambuddy_id, ams_id, body.label, serial=serial)
    place, problem = await asyncio.to_thread(
        _name_place, filament, UUID(row.id), ams_id, body.label
    )
    return sc.AmsLabelSaved(ams_id=ams_id, label=body.label, place=place, place_problem=problem)


@router.delete(
    "/{printer_id}/ams/{ams_id}/label",
    response_model=sc.AmsLabelSaved,
    summary="Clear an AMS unit's label",
    description=(
        "Deletes Bambuddy's AMS label, then names ScadBuddy's place of the unit"
        " `<printer> <unit>` again." + ADMIN + " 404 for an unknown printer; 409 for a virtual"
        " or unlinked printer; 422 for a unit no printer has."
    ),
)
async def clear_printer_ams_label(
    printers: PrintersDep,
    store: SettingsStoreDep,
    filament: FilamentDep,
    printer_id: str,
    ams_id: AmsId,
) -> sc.AmsLabelSaved:
    _unit(ams_id)
    row, bambuddy_id = await sc.read_row(printers.store, printer_id)
    async with client_for(await asyncio.to_thread(store.load)) as client:
        serial = await _serial(client, bambuddy_id, ams_id)
        await client.delete_ams_label(bambuddy_id, ams_id, serial=serial)
    place, problem = await asyncio.to_thread(_name_place, filament, UUID(row.id), ams_id, None)
    return sc.AmsLabelSaved(ams_id=ams_id, label=None, place=place, place_problem=problem)
