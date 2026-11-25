"""A registry printer's AMS slots over ScadBuddy's own LAN link (#7276, plan
``docs/superpowers/plans/2026-11-25-filament-hardware-off-bambuddy.md`` Task 8; the routes
are #7153's).

Each command route (configure, reset, refresh, load, unload) takes the LAN row first (409
``not_lan_linked``). An ``Idempotency-Key`` that already started the route's commands
answers that run, as it was started, and checks and does nothing else (`_resent`, P38,
P41). Otherwise the route checks the printer's stored report (`ams_slots.guard`,
R-7276-12), runs its MQTT commands
(the ``LanCommand`` workflow) and answers through `command_response`: 200 with the route's
model, or 202 with the run's workflow id when the printer is slow (P27; follow it at
``GET /printers/{id}/commands/{command_id}``, which answers the same model, P37). The slot
presets and the unit labels are ScadBuddy's own (`printers/command_data.py`, the unit's
place). No route here calls Bambuddy. Only the AMS label routes write a row the filament
sync sends (the unit's place), so only they nudge it; the rest are in `api/filament.py`
`NOT_NUDGED`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from fastapi import status as http

from scadbuddy.api.filament import nudge_after_write
from scadbuddy.api.operations import IdempotencyKey
from scadbuddy.bambuddy.catalogue import Catalogue
from scadbuddy.bambuddy.models import AmsTray, PrinterStatus
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
from scadbuddy.printers import ams_slots as sc
from scadbuddy.printers.component import LanCommandsDep, PrintersDep
from scadbuddy.printers.lan_command_models import (
    LanCommandOutcome,
    LanCommandPending,
    LanCommandRun,
)
from scadbuddy.printers.lan_commands import (
    UnsupportedUnitError,
    ams_change_filament_load,
    ams_change_filament_unload,
    ams_get_rfid,
    ams_reset,
    wire_slot,
)
from scadbuddy.printers.lan_models import BUSY_STATES
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.native import LanCommands, command_response, lan_row, live_status
from scadbuddy.printers.store import printer_uuid
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.component import SlicingDep
from scadbuddy.workflows.lan_command import LanCommandAnswer

router = APIRouter(prefix="/printers", tags=["printers"], dependencies=[Depends(nudge_after_write)])

AmsId = Annotated[int, Path(ge=0, le=255)]
TrayId = Annotated[int, Path(ge=0, le=15)]
SLOT = "/{printer_id}/trays/{ams_id}/{tray_id}"
FEATURE = "AMS"
ACCEPTED: dict[int | str, dict[str, Any]] = {http.HTTP_202_ACCEPTED: {"model": LanCommandPending}}
STATE_FIRST = (
    " It reads the printer's latest LAN report first and refuses with a 409 and a `code`:"
    " `printer_offline` (no fresh report), `printer_busy` (a print is active), `ams_busy`"
    " (the AMS changes filament or reads a tag), `no_slot`."
)
PRINTER = (
    " ScadBuddy reaches the printer over its own LAN link: 404 for an unknown printer; 409"
    " `not_lan_linked` for a printer not linked over the LAN; 422 for a unit or slot no"
    " printer has. A command the printer is slow to answer is a 202 with a `workflow_id`:"
    " follow it at `GET /printers/{id}/commands/{command_id}`. 409 `printer_refused` when the"
    " printer refuses it, `printer_cert_changed` when it presents another certificate; 502"
    " `printer_unreachable`; 503 `lan_unavailable`. `message` is `sent`, or says the printer"
    " did not acknowledge it in time. An `Idempotency-Key` sent again joins the run it"
    " started or reads its outcome, and answers what that first request sent, whatever this"
    " one's body and the printer's state now."
)
OWNED = (
    " ScadBuddy keeps it, not the printer: no printer state is read. 404 for an unknown printer."
)


async def _reach(printers: PrintersDep, printer_id: str) -> tuple[PrinterRow, PrinterStatus]:
    """The LAN row (404, 409 ``not_lan_linked``) and its status from the stored report."""
    row = await lan_row(printers.store, printer_id, feature=FEATURE)
    return row, await live_status(printers.reports, row, now=datetime.now(UTC))


async def _owner(printers: PrintersDep, printer_id: str) -> UUID:
    """The printer's canonical id: 404 for an unknown one. Presets and labels are
    ScadBuddy's own, so the printer needs no LAN link."""
    key = printer_uuid(printer_id)
    row = None if key is None else await asyncio.to_thread(printers.store.get, printer_id)
    if row is None:
        raise ApiError(http.HTTP_404_NOT_FOUND, f"no printer {printer_id}")
    return UUID(row.id)


def _wired[T](make: Callable[[], T]) -> T:
    """``make()``, a builder's `UnsupportedUnitError` as 422 ``unsupported_unit`` (P22)."""
    try:
        return make()
    except UnsupportedUnitError as error:
        raise ApiError(
            http.HTTP_422_UNPROCESSABLE_CONTENT, str(error), code="unsupported_unit"
        ) from None


#: The command names each route's run may have, which name its workflow (P9) with the
#: route's action (`_keyed`).
KINDS: dict[sc.Action, list[list[str]]] = {
    "configure": [
        ["ams_filament_setting", "extrusion_cali_sel"],
        ["ams_filament_setting", "extrusion_cali_sel", "extrusion_cali_set"],
    ],
    "reset": [["ams_filament_setting"]],
    "refresh": [["ams_get_rfid"]],
    "load": [["ams_change_filament"]],
    "unload": [["ams_change_filament"]],
}


def _keyed(action: sc.Action, key: str | None) -> str | None:
    """The run key of the route ``action`` for the request's ``key``: the action goes into
    the run's workflow id (P9), since load and unload send the same command, so a key
    reused for another action is a run of its own, never the first one's answer."""
    return None if key is None else f"{action}-{key}"


async def _resent(
    commands: LanCommands, row: PrinterRow, action: sc.Action, key: str | None
) -> Response | None:
    """The answer of the run ``key`` already started for the route (P38, P41): joined or
    read, built from that run's own ``answer`` (not the resend's body), with no state
    check and nothing sent again; ``None`` when the key started none."""
    run = await commands.keyed(row.id, KINDS[action], _keyed(action, key))
    if run is None:
        return None
    return command_response(await commands.run(row.id, run, key=_keyed(action, key)), run)


async def _started(
    commands: LanCommands,
    row: PrinterRow,
    status: PrinterStatus,
    action: sc.Action,
    ams_id: int,
    tray_id: int,
    key: str | None,
    build: Callable[[AmsTray], LanCommandRun],
) -> tuple[LanCommandAnswer, LanCommandRun]:
    """Run what ``build`` makes of the slot once `guard` passes (P36): the answer, and the
    run it belongs to, which is the first request's when two first sends of one key meet
    (`LanCommands.run_started`)."""
    run = build(sc.guard(status, action, ams_id, tray_id))
    return await commands.run_started(row.id, run, key=_keyed(action, key))


async def _send(
    commands: LanCommands,
    row: PrinterRow,
    status: PrinterStatus,
    action: sc.Action,
    ams_id: int,
    tray_id: int,
    key: str | None,
    build: Callable[[AmsTray], LanCommandRun],
) -> Response:
    """`_started`'s answer as the route's response."""
    return command_response(
        *await _started(commands, row, status, action, ams_id, tray_id, key, build)
    )


def _one(
    row: PrinterRow, name: str, payload: dict[str, Any], answer: sc.SlotCommandSent
) -> LanCommandRun:
    return LanCommandRun(
        commands=[sc.write(row.id, name, payload)], answer=answer.model_dump(mode="json")
    )


def _sent(ams_id: int, tray_id: int, action: sc.Action) -> sc.SlotCommandSent:
    return sc.SlotCommandSent(ams_id=ams_id, tray_id=tray_id, action=action, message=sc.SENT)


@router.get(
    "/{printer_id}/ams",
    response_model=sc.AmsView,
    summary="A printer's AMS slots",
    description=(
        "Each AMS unit and external holder from the printer's latest LAN report: every slot's"
        " material, colour, preset, temperatures, whether it holds a spool and whether the"
        " printer is loaded from it, ScadBuddy's saved slot preset, each unit's place and"
        " label, and the printer's state (`connected` false with no fresh report, `printing`,"
        " `ams_busy`), so a client can tell what a write would refuse. No command is sent."
        " 404 for an unknown printer; 409 `not_lan_linked` for a printer not linked over the"
        " LAN."
    ),
)
async def get_printer_ams(
    printers: PrintersDep, filament: FilamentDep, printer_id: str
) -> sc.AmsView:
    row, status = await _reach(printers, printer_id)
    owner = UUID(row.id)
    saved, places = await asyncio.gather(
        asyncio.to_thread(printers.command_data.slot_presets, owner),
        asyncio.to_thread(filament.locations.ams_units, owner),
    )
    held = {(t.ams_id, t.tray_id) for t in seen_trays(status)}
    units: dict[int, list[sc.SlotView]] = {}
    for ams_id, tray in every_tray(status):
        global_id = sc.global_tray(ams_id, tray.id)
        units.setdefault(ams_id, []).append(
            sc.SlotView(
                tray_id=tray.id,
                label=tray_label(ams_id, tray.id),
                filled=(ams_id, tray.id) in held,
                loaded=sc.is_loaded(status, ams_id, tray.id),
                material=(tray.tray_type or "").strip() or None,
                colour=tray_rgba(tray.tray_color),
                preset_id=(tray.tray_info_idx or "").strip() or None,
                preset_name=(tray.tray_sub_brands or "").strip() or None,
                nozzle_temp_min=tray.nozzle_temp_min or None,
                nozzle_temp_max=tray.nozzle_temp_max or None,
                saved_preset=saved.get((ams_id, tray.id)),
                loadable=global_id is not None,
                refreshable=ams_id != EXTERNAL,
            )
        )

    def label(ams_id: int) -> str | None:
        place = places.get(ams_id)
        default = f"{row.name} {unit_label(ams_id)}"
        return None if place is None or place.name == default else place.name

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
                label=label(ams_id),
                place_id=None if ams_id not in places else places[ams_id].id,
                place_name=None if ams_id not in places else places[ams_id].name,
                slots=sorted(slots, key=lambda s: s.tray_id),
            )
            for ams_id, slots in sorted(units.items(), key=lambda u: (u[0] == EXTERNAL, u[0]))
        ],
    )


async def _catalogue(slicing: SlicingDep) -> Catalogue | None:
    """The native catalogue a spool's preset is read on (R-7276-15), with ScadBuddy's own
    presets; ``None`` without the image's slicer profiles."""
    profiles = slicing.profiles
    return None if profiles is None else await catalogue_for(None, native=profiles)


@router.post(
    f"{SLOT}/configure",
    response_model=sc.SlotConfigured,
    responses=ACCEPTED,
    summary="Configure an AMS slot by hand",
    description=(
        "Sets the slot's material, colour, preset and temperatures (`ams_filament_setting`),"
        " then selects its K profile (`extrusion_cali_sel`, `cali_idx` or the default), and"
        " with a `k_value` and no profile sets the slot's K value. Send a `spool_id` for any"
        " ScadBuddy spool's values (its type's preset for the slot's nozzle, whose Bambu"
        " filament id the slot takes, and its K value), the values themselves, or both: what"
        " is sent replaces the spool's, and the tray's own temperatures fill in. With no"
        " preset the material's generic filament id is sent. The nozzle is"
        " `nozzle_diameter`, else the one the report names for the slot (409 `no_nozzle` when"
        " neither). 404 for an unknown spool; 409 `no_preset` for a spool preset with no"
        " filament id ScadBuddy knows; 422 naming what is still missing, and"
        " `unsupported_unit` for a K value on an AMS past 3. It links no spool to the tray."
        " Answers what was sent." + STATE_FIRST + PRINTER
    ),
)
async def configure_printer_slot(
    printers: PrintersDep,
    commands: LanCommandsDep,
    filament: FilamentDep,
    slicing: SlicingDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    body: sc.SlotConfigureBody,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    sc.known_slot(ams_id, tray_id)
    row, status = await _reach(printers, printer_id)
    if (resent := await _resent(commands, row, "configure", idempotency_key)) is not None:
        return resent
    spool: m.Spool | None = None
    presets: list[m.EffectivePreset] = []
    k_values: list[m.EffectiveKValue] = []
    catalogue = None
    if body.spool_id is not None:
        spool = await asyncio.to_thread(filament.spools.get, body.spool_id)
        if spool is None:
            raise ApiError(http.HTTP_404_NOT_FOUND, f"no spool {body.spool_id}")
        presets, k_values = await asyncio.to_thread(_spool_presets, filament, body.spool_id)
        if presets and body.preset_id is None:
            catalogue = await _catalogue(slicing)

    def build(tray: AmsTray) -> LanCommandRun:
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
            catalogue=catalogue,
        )
        configure = _wired(
            lambda: sc.configure_commands(
                params,
                ams_id,
                tray_id,
                external_count=len(status.vt_tray),
                printer_id=row.id,
            )
        )
        return LanCommandRun(commands=configure, answer=sent.model_dump(mode="json"))

    return await _send(commands, row, status, "configure", ams_id, tray_id, idempotency_key, build)


def _spool_presets(
    filament: FilamentDep, spool_id: UUID
) -> tuple[list[m.EffectivePreset], list[m.EffectiveKValue]]:
    with filament.spools.transaction() as conn:
        return effective_presets(conn, spool_id), effective_k_values(conn, spool_id)


@router.post(
    f"{SLOT}/reset",
    response_model=sc.SlotCommandSent,
    responses=ACCEPTED,
    summary="Reset an AMS slot",
    description=(
        "Clears the slot's filament setting on the printer (an empty `ams_filament_setting`)."
        " ScadBuddy's saved slot preset is cleared only when the printer acknowledged the"
        " reset in this answer: a 202, or a `message` that says the printer did not"
        " acknowledge it, keeps the preset." + STATE_FIRST + PRINTER
    ),
)
async def reset_printer_slot(
    printers: PrintersDep,
    commands: LanCommandsDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    sc.known_slot(ams_id, tray_id)
    row, status = await _reach(printers, printer_id)
    # A resend clears no preset again: one saved since stands (P43).
    if (resent := await _resent(commands, row, "reset", idempotency_key)) is not None:
        return resent

    def build(_tray: AmsTray) -> LanCommandRun:
        slot = _wired(lambda: wire_slot(ams_id, tray_id, external_count=len(status.vt_tray)))
        payload = _wired(lambda: ams_reset(slot, sc.SEQ))
        return _one(row, "ams_filament_setting", payload, _sent(ams_id, tray_id, "reset"))

    answer, run = await _started(
        commands, row, status, "reset", ams_id, tray_id, idempotency_key, build
    )
    # Only a reset the printer acknowledged clears the preset: a run still open, or one
    # sent but not acknowledged, may not have reached the slot.
    if isinstance(answer, LanCommandOutcome) and all(r.confirmed for r in answer.replies):
        await asyncio.to_thread(
            printers.command_data.clear_slot_preset, UUID(row.id), ams_id, tray_id
        )
    return command_response(answer, run)


@router.post(
    f"{SLOT}/refresh",
    response_model=sc.SlotCommandSent,
    responses=ACCEPTED,
    summary="Read an AMS slot's tag again",
    description=(
        "The AMS reads the slot's RFID tag again (`ams_get_rfid`). Not for an external holder"
        " (422). Also 409 `filament_loaded` while filament is loaded, as the AMS must move"
        " it." + STATE_FIRST + PRINTER
    ),
)
async def refresh_printer_slot(
    printers: PrintersDep,
    commands: LanCommandsDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    sc.known_slot(ams_id, tray_id)
    if ams_id == EXTERNAL:
        raise sc.invalid("an external holder has no RFID reader to refresh")
    row, status = await _reach(printers, printer_id)
    if (resent := await _resent(commands, row, "refresh", idempotency_key)) is not None:
        return resent

    def build(_tray: AmsTray) -> LanCommandRun:
        payload = _wired(lambda: ams_get_rfid(ams_id, tray_id, sc.SEQ))
        return _one(row, "ams_get_rfid", payload, _sent(ams_id, tray_id, "refresh"))

    return await _send(commands, row, status, "refresh", ams_id, tray_id, idempotency_key, build)


def _global(ams_id: int, tray_id: int) -> int:
    tray = sc.global_tray(ams_id, tray_id)
    if tray is None:
        raise sc.invalid(f"AMS {ams_id} cannot be loaded or unloaded: only AMS 0-3 have a tray id")
    return tray


async def _temps(printers: PrintersDep, row: PrinterRow) -> tuple[int, int]:
    report = await asyncio.to_thread(printers.reports.get, row.id)
    return (0, 0) if report is None else sc.nozzle_temps(report[0])


@router.post(
    f"{SLOT}/load",
    response_model=sc.SlotCommandSent,
    responses=ACCEPTED,
    summary="Load filament from an AMS slot",
    description=(
        "The printer feeds the slot's filament to its hotend. AMS 0-3, an AMS-HT or an"
        " external holder (422 for an AMS past 3). Also 409 `tray_empty` (no spool in it),"
        " `already_loaded` (an extruder feeds from it now) and `extruder_unknown` (with the"
        " Filament Track Switch, the report does not say which hotend it feeds)."
        + STATE_FIRST
        + PRINTER
    ),
)
async def load_printer_slot(
    printers: PrintersDep,
    commands: LanCommandsDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    sc.known_slot(ams_id, tray_id)
    target = _global(ams_id, tray_id)
    row, status = await _reach(printers, printer_id)
    if (resent := await _resent(commands, row, "load", idempotency_key)) is not None:
        return resent
    _, right = await _temps(printers, row)

    def build(_tray: AmsTray) -> LanCommandRun:
        # Only with the Filament Track Switch does the load name the hotend (Bambuddy
        # ``routes/printers.py`` line 4770, v1.3.1).
        switch = status.fila_switch is not None and status.fila_switch.installed
        extruder = sc.slot_extruder(status, ams_id, tray_id) if switch else None
        if switch and extruder is None:
            raise sc.refused(
                "extruder_unknown",
                "the printer has the Filament Track Switch and its report does not say"
                f" which hotend {tray_label(ams_id, tray_id)} feeds",
            )
        payload = _wired(
            lambda: ams_change_filament_load(
                target,
                external_count=len(status.vt_tray),
                extruder_id=extruder,
                right_temp=right,
                seq=sc.SEQ,
            )
        )
        return _one(row, "ams_change_filament", payload, _sent(ams_id, tray_id, "load"))

    return await _send(commands, row, status, "load", ams_id, tray_id, idempotency_key, build)


@router.post(
    f"{SLOT}/unload",
    response_model=sc.SlotCommandSent,
    responses=ACCEPTED,
    summary="Unload filament fed from an AMS slot",
    description=(
        "The printer pulls back the filament its hotend takes from the slot. 409 `not_loaded`"
        " when no extruder is loaded from it (Ext-R's loaded state cannot be told, so it"
        " is always refused); 422 for an AMS past 3." + STATE_FIRST + PRINTER
    ),
)
async def unload_printer_slot(
    printers: PrintersDep,
    commands: LanCommandsDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    sc.known_slot(ams_id, tray_id)
    source = _global(ams_id, tray_id)
    row, status = await _reach(printers, printer_id)
    if (resent := await _resent(commands, row, "unload", idempotency_key)) is not None:
        return resent
    nozzle, _ = await _temps(printers, row)

    def build(_tray: AmsTray) -> LanCommandRun:
        payload = _wired(
            lambda: ams_change_filament_unload(
                source, external_count=len(status.vt_tray), nozzle_temp=nozzle, seq=sc.SEQ
            )
        )
        return _one(row, "ams_change_filament", payload, _sent(ams_id, tray_id, "unload"))

    return await _send(commands, row, status, "unload", ams_id, tray_id, idempotency_key, build)


@router.put(
    f"{SLOT}/preset",
    response_model=sc.SavedSlotPreset,
    summary="Save the slicer preset an AMS slot shows",
    description=(
        "Send the `tray_info_idx` the slot was configured with; `preset_source` is accepted"
        " and ignored. 422 for a unit or slot no printer has, or a bad body." + OWNED
    ),
)
async def set_printer_slot_preset(
    printers: PrintersDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    body: sc.SlotPresetBody,
) -> sc.SavedSlotPreset:
    sc.known_slot(ams_id, tray_id)
    owner = await _owner(printers, printer_id)
    return await asyncio.to_thread(
        printers.command_data.set_slot_preset, owner, ams_id, tray_id, body
    )


@router.delete(
    f"{SLOT}/preset",
    status_code=http.HTTP_204_NO_CONTENT,
    summary="Clear the slicer preset an AMS slot shows",
    description=(
        "One that is not there is cleared all the same. 422 for a unit or slot no printer"
        " has." + OWNED
    ),
)
async def clear_printer_slot_preset(
    printers: PrintersDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
) -> None:
    sc.known_slot(ams_id, tray_id)
    owner = await _owner(printers, printer_id)
    await asyncio.to_thread(printers.command_data.clear_slot_preset, owner, ams_id, tray_id)


def _unit(ams_id: int) -> None:
    if not (0 <= ams_id <= 15 or 128 <= ams_id <= 135):
        raise sc.invalid(f"no printer has AMS {ams_id}: an AMS is 0-15, an AMS-HT 128-135")


def _name_place(
    filament: FilamentDep, printer: UUID, ams_id: int, label: str | None
) -> tuple[m.Location | None, sc.PlaceProblem | None]:
    try:
        return filament.locations.name_ams_unit(printer, ams_id, label), None
    except NoAmsKindError:
        return None, "no_ams_kind"
    except CatalogRefError:
        return None, "no_printer"


LABEL = (
    " The label is the unit's place name: the place is made if missing. 200 all"
    " the same when it could not be named: `place_problem` `no_ams_kind` says no location"
    " kind is marked AMS, `no_printer` that the printer was deleted meanwhile. 422 for a"
    " unit no printer has." + OWNED
)


@router.put(
    "/{printer_id}/ams/{ams_id}/label",
    response_model=sc.AmsLabelSaved,
    summary="Label an AMS unit",
    description="Names ScadBuddy's place of the unit." + LABEL,
)
async def set_printer_ams_label(
    printers: PrintersDep,
    filament: FilamentDep,
    printer_id: str,
    ams_id: AmsId,
    body: sc.AmsLabelBody,
) -> sc.AmsLabelSaved:
    _unit(ams_id)
    owner = await _owner(printers, printer_id)
    place, problem = await asyncio.to_thread(_name_place, filament, owner, ams_id, body.label)
    return sc.AmsLabelSaved(ams_id=ams_id, label=body.label, place=place, place_problem=problem)


@router.delete(
    "/{printer_id}/ams/{ams_id}/label",
    response_model=sc.AmsLabelSaved,
    summary="Clear an AMS unit's label",
    description="Names ScadBuddy's place of the unit `<printer> <unit>` again." + LABEL,
)
async def clear_printer_ams_label(
    printers: PrintersDep,
    filament: FilamentDep,
    printer_id: str,
    ams_id: AmsId,
) -> sc.AmsLabelSaved:
    _unit(ams_id)
    owner = await _owner(printers, printer_id)
    place, problem = await asyncio.to_thread(_name_place, filament, owner, ams_id, None)
    return sc.AmsLabelSaved(ams_id=ams_id, label=None, place=place, place_problem=problem)
