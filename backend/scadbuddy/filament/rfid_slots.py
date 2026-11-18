"""The AMS slot of an override tag, configured with its type's preset (#6264).

A tag bound with ``override=True`` says: the spool's own values stand, not what the Bambu
tag says. Bambuddy's AMS reads the tag, so ScadBuddy tells the slot the spool's values with
Bambuddy's slot-configure route (`BambuddyClient.configure_slot`). Every step is
state-based: the report's tray values are read first, and a write follows only a
difference (`reconcile_slots`)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.extruders import FTS_INLET_EXTRUDER, LEFT, RIGHT
from scadbuddy.bambuddy.models import SlotConfigure
from scadbuddy.bambuddy.resolver import PRINTER_MODEL
from scadbuddy.bambuddy.trays import seen_trays
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.component import Filament
from scadbuddy.filament.presets import effective_k_values, effective_presets, preset_for
from scadbuddy.filament.rfid_push import error_label
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.reports import PrinterReportStore, report_status
from scadbuddy.printers.status import _fila_switch, _nozzles, _routing, is_fresh
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools.models import SeenTray

logger = logging.getLogger(__name__)

#: ``tray_color`` of a tray that reports none and whose spool has none: Bambu's own empty
#: value.
NO_COLOUR = "00000000"


def _extruder(report: Mapping[str, Any], ams_id: int) -> int | None:
    """The physical extruder (0 right, 1 left) the AMS unit ``ams_id`` feeds, or rests on
    with the Filament Track Switch (`bambuddy/extruders.py` `extruder_of`, the same
    reading); ``None`` when the report does not say."""
    data = dict(report)
    extruders, inlets = _routing(data, _fila_switch(data) is not None)
    mapped = extruders.get(str(ams_id))
    if mapped is not None:
        return mapped if mapped in (RIGHT, LEFT) else None
    inlet = inlets.get(str(ams_id))
    return FTS_INLET_EXTRUDER.get(inlet.upper()) if inlet else None


def installed_nozzle(report: Mapping[str, Any], ams_id: int) -> str | None:
    """The diameter of the nozzle installed on the extruder the AMS unit ``ams_id`` feeds
    (``"0.4"``), or ``None`` when the report does not say.

    Measured on `tests/printers/fixtures/lan-report-h2c-idle.json` (an H2C, firmware
    01.02.00.00, recorded 2026-10-24). The installed nozzle of extruder ``n`` is the entry
    of ``print.device.nozzle.info`` whose ``id`` is ``n`` (ids 0 and 1; ids from 16 are the
    rack), and its ``diameter`` is the key: ``0.6`` for id 0 and ``0.4`` for id 1. That
    fixture says nothing direct about which extruder an AMS feeds: no unit fixes one (bits
    8-11 of each unit's ``info`` are ``0xE``, "not fixed", as ``11002E03`` shows). With
    the Filament Track Switch, bits 24-27 of ``info`` give the inlet the unit rests on
    (``0x11`` is inlet A, ``0x10`` inlet B), and inlet A rests on the left extruder, B on
    the right. That mapping is Bambuddy's (`bambuddy/extruders.py` `FTS_INLET_EXTRUDER`,
    "measured on the maintainer's H2C"), and this function takes it as the documented
    field: with the switch fitted any AMS can reach either nozzle, so this is the nozzle
    the unit rests on, not one the printer promises. `lan-report-h2c.json` has no ``info``
    on its units, so there the answer is ``None``."""
    extruder = _extruder(report, ams_id)
    if extruder is None:
        return None
    mounted, _ = _nozzles(dict(report))
    if extruder >= len(mounted):
        return None
    return mounted[extruder].nozzle_diameter or None


def slot_params(
    spool: m.Spool,
    presets: Sequence[m.EffectivePreset],
    k_values: Sequence[m.EffectiveKValue],
    printer_id: UUID,
    printer_model: str,
    nozzle: str,
    tray_color: str,
    extruder: int | None,
) -> SlotConfigure | None:
    """What the slot is configured with: the spool's values and its type's preset for this
    printer model and ``nozzle``. ``None`` when there is no such preset, or the spool has no
    material or temperatures to send. ``tray_color`` (``RRGGBBAA``) is the tray's own, used
    when the spool has no colour. The K value is the one of this printer, ``extruder`` and
    nozzle; without one ``cali_idx`` is ``-1`` and the K value is empty."""
    preset = preset_for(presets, printer_model, nozzle)
    effective = spool.effective
    material = spool.names.material
    if (
        preset is None
        or material is None
        or effective.nozzle_temp_min is None
        or effective.nozzle_temp_max is None
    ):
        return None
    k = next(
        (
            row
            for row in k_values
            if row.printer_id == printer_id
            and row.extruder == extruder
            and row.nozzle_diameter == nozzle
        ),
        None,
    )
    colour = effective.rgba.lstrip("#").upper() if effective.rgba else tray_color
    return SlotConfigure(
        tray_info_idx=preset.slicer_filament,
        tray_type=material,
        tray_sub_brands=preset.slicer_filament_name or spool.names.subtype or material,
        tray_color=colour,
        nozzle_temp_min=effective.nozzle_temp_min,
        nozzle_temp_max=effective.nozzle_temp_max,
        cali_idx=-1 if k is None or k.cali_idx is None else k.cali_idx,
        nozzle_diameter=nozzle,
        setting_id=None if k is None else k.setting_id,
        k_value=None if k is None else float(k.k_value),
    )


def _tray_colour(seen: SeenTray) -> str:
    return seen.rgba.lstrip("#").upper() if seen.rgba else NO_COLOUR


def _shows(seen: SeenTray, params: SlotConfigure) -> bool:
    """Whether the report's tray already shows what ``params`` would set."""
    return (
        seen.tray_info_idx == params.tray_info_idx
        and (seen.material or "") == params.tray_type
        and _tray_colour(seen) == params.tray_color
    )


def _read(
    store: Filament, printer_id: UUID, ams_id: int, tray_id: int, spool_id: UUID
) -> tuple[bool, list[m.EffectivePreset], list[m.EffectiveKValue], dict[str, Any] | None]:
    """Whether the spool has an open override binding, its effective presets and K values,
    and the slot's row."""
    with store.spools.transaction() as conn:
        override = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM filament_tag_bindings"
            " WHERE spool_id = %s AND unbound_at IS NULL AND override) AS held",
            (spool_id,),
        ).fetchone()
        slot = conn.execute(
            "SELECT spool_id, params_hash FROM filament_override_slots"
            " WHERE printer_id = %s AND ams_id = %s AND tray_id = %s",
            (printer_id, ams_id, tray_id),
        ).fetchone()
        return (
            bool(override and override["held"]),
            effective_presets(conn, spool_id),
            effective_k_values(conn, spool_id),
            slot,
        )


def _drop_stale(store: Filament, printer_id: UUID, keep: Sequence[tuple[int, int]]) -> None:
    """Delete the printer's slot rows outside ``keep``."""
    with store.spools.transaction() as conn:
        conn.execute(
            "DELETE FROM filament_override_slots WHERE printer_id = %s"
            " AND (ams_id, tray_id) NOT IN (SELECT * FROM unnest(%s::int[], %s::int[]))",
            (printer_id, [a for a, _ in keep], [t for _, t in keep]),
        )


def _configured(
    store: Filament, printer_id: UUID, ams_id: int, tray_id: int, spool_id: UUID, digest: str
) -> None:
    with store.spools.transaction() as conn:
        conn.execute(
            "INSERT INTO filament_override_slots"
            " (printer_id, ams_id, tray_id, spool_id, params_hash, configured_at)"
            " VALUES (%s, %s, %s, %s, %s, now())"
            " ON CONFLICT (printer_id, ams_id, tray_id) DO UPDATE SET"
            " spool_id = EXCLUDED.spool_id, params_hash = EXCLUDED.params_hash,"
            " configured_at = EXCLUDED.configured_at, attempts = 0, last_error = NULL",
            (printer_id, ams_id, tray_id, spool_id, digest),
        )


def _failed(
    store: Filament, printer_id: UUID, ams_id: int, tray_id: int, spool_id: UUID, label: str
) -> None:
    with store.spools.transaction() as conn:
        conn.execute(
            "INSERT INTO filament_override_slots"
            " (printer_id, ams_id, tray_id, spool_id, attempts, last_error)"
            " VALUES (%s, %s, %s, %s, 1, %s)"
            " ON CONFLICT (printer_id, ams_id, tray_id) DO UPDATE SET"
            " spool_id = EXCLUDED.spool_id, params_hash = NULL,"
            " attempts = filament_override_slots.attempts + 1, last_error = EXCLUDED.last_error",
            (printer_id, ams_id, tray_id, spool_id, label),
        )


async def reconcile_slots(
    store: Filament,
    client: BambuddyClient,
    *,
    printers: PrinterStore,
    reports: PrinterReportStore,
) -> m.SlotReport:
    """Configure the slot of every override tag a printer's fresh report shows. A printer
    counts when it has a ``bambuddy_printer_id`` and a fresh report. A filled tray counts
    only when its holder was found by its tag (`FilamentInventory.trays`) and the holder
    spool has an open binding with ``override=True``.

    A counted slot is ``unchanged`` only when both hold: the row's hash equals the digest of
    the params, for the same spool (the hash is an attempt guard: what ScadBuddy sent), and
    the report's tray already shows the params' ``tray_info_idx``, material and colour. The
    AMS re-reads a Bambu tag when it reloads and resets the slot, so the hash is never
    trusted alone. Any difference is configured, and the row is written with the hash and
    ``configured_at``. A failure records ``attempts`` and ``last_error`` (`error_label`),
    stores no hash, and the next slot goes on. A preset for no nozzle size is ``no_preset``,
    a tray whose nozzle the report does not say is ``no_nozzle``; neither calls Bambuddy.

    A slot whose holder no longer counts (its override binding is closed or turned off, or
    the tray has no spool) is sent nothing: ScadBuddy cannot know the tag's own values, and
    the AMS reads a Bambu tag again when the spool is reloaded. Its row is deleted, so that
    a later override on the slot configures again. A spool's deletion cascades its row away;
    closing or turning off a binding does not, so this deletes it. Only a printer with a
    fresh report is touched, so a stale report deletes nothing."""
    report = m.SlotReport()
    now = datetime.now(UTC)
    for row in await asyncio.to_thread(printers.list):
        if row.bambuddy_printer_id is None:
            continue
        stored = await asyncio.to_thread(reports.get, row.id)
        if stored is None or not is_fresh(stored[1], now):
            continue
        printer = UUID(row.id)
        seen = {
            (tray.ams_id, tray.tray_id): tray
            for tray in seen_trays(report_status(row, stored, now=now))
        }
        view = await asyncio.to_thread(store.inventory.trays, printer, list(seen.values()))
        keep: list[tuple[int, int]] = []
        for holder in view.trays:
            if holder.found_by != "tag" or holder.spool is None:
                continue
            ams_id, tray_id, spool = holder.ams_id, holder.tray_id, holder.spool
            override, presets, k_values, slot = await asyncio.to_thread(
                _read, store, printer, ams_id, tray_id, spool.id
            )
            if not override:
                continue
            keep.append((ams_id, tray_id))
            tray = seen[ams_id, tray_id]
            nozzle = installed_nozzle(stored[0], ams_id)
            if nozzle is None:
                report.no_nozzle += 1
                continue
            params = slot_params(
                spool,
                presets,
                k_values,
                printer,
                PRINTER_MODEL,
                nozzle,
                _tray_colour(tray),
                _extruder(stored[0], ams_id),
            )
            if params is None:
                report.no_preset += 1
                continue
            digest = params.digest()
            if (
                slot is not None
                and slot["spool_id"] == spool.id
                and slot["params_hash"] == digest
                and _shows(tray, params)
            ):
                report.unchanged += 1
                continue
            try:
                await client.configure_slot(row.bambuddy_printer_id, ams_id, tray_id, params)
            except ApiError as exc:
                label = error_label(exc)
                logger.warning(
                    "could not configure an override slot",
                    extra={
                        "printer_id": row.id,
                        "ams_id": ams_id,
                        "tray_id": tray_id,
                        "error": label,
                    },
                )
                await asyncio.to_thread(_failed, store, printer, ams_id, tray_id, spool.id, label)
                report.failed += 1
                continue
            await asyncio.to_thread(_configured, store, printer, ams_id, tray_id, spool.id, digest)
            report.configured += 1
        await asyncio.to_thread(_drop_stale, store, printer, keep)
    return report


async def reconcile_slots_now(
    store: Filament,
    load: Callable[[], StoredSettings],
    *,
    printers: PrinterStore,
    reports: PrinterReportStore,
) -> m.SlotReport | None:
    """`reconcile_slots` with a client of its own; ``None`` (the step is skipped) when no
    Bambuddy is configured."""
    settings = await asyncio.to_thread(load)
    if not settings.bambuddy_url:
        return None
    async with client_for(settings) as client:
        return await reconcile_slots(store, client, printers=printers, reports=reports)
