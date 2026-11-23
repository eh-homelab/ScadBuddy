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
from scadbuddy.bambuddy.extruders import extruder_of
from scadbuddy.bambuddy.models import PrinterStatus, SlotConfigure
from scadbuddy.bambuddy.resolver import PRINTER_MODEL
from scadbuddy.bambuddy.trays import seen_trays
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.component import Filament
from scadbuddy.filament.presets import effective_k_values, effective_presets, preset_for
from scadbuddy.filament.rfid_push import acquire_lock, error_label
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.reports import PrinterReportStore, report_status
from scadbuddy.printers.status import is_fresh, mounted_nozzles
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools.models import SeenTray

logger = logging.getLogger(__name__)

#: The session advisory lock (hashed with ``hashtextextended``) one `reconcile_slots_now`
#: holds, across processes, for the whole step.
SLOTS_LOCK = "scadbuddy:filament:rfid_slots"
#: After this many consecutive configures of a slot whose params did not change, a warning
#: is logged: the AMS may not report back what was sent (see `reconcile_slots`).
RECONFIGURE_WARNING = 3


def installed_nozzle(
    status: PrinterStatus, report: Mapping[str, Any], ams_id: int, tray_id: int = 0
) -> str | None:
    """The diameter of the nozzle installed on the extruder the AMS unit ``ams_id`` feeds
    (``"0.4"``), or ``None`` when the report does not say. ``status`` is the report's
    `PrinterStatus` (`report_status`); the extruder is `bambuddy/extruders.py`
    `extruder_of` of it.

    Measured on `tests/printers/fixtures/lan-report-h2c-idle.json` (an H2C, firmware
    01.02.00.00, recorded 2026-10-24). The installed nozzle of extruder ``n`` is the entry
    of ``print.device.nozzle.info`` whose ``id`` is ``n`` (ids 0 and 1; ids from 16 are the
    rack; `mounted_nozzles` indexes by id), and its ``diameter`` is the key: ``0.6`` for id
    0 and ``0.4`` for id 1. That fixture says nothing direct about which extruder an AMS
    feeds: no unit fixes one (bits 8-11 of each unit's ``info`` are ``0xE``, "not fixed",
    as ``11002E03`` shows). With the Filament Track Switch, bits 24-27 of ``info`` give the
    inlet the unit rests on (``0x11`` is inlet A, ``0x10`` inlet B), and inlet A rests on
    the left extruder, B on the right. That mapping is Bambuddy's ``FTS_INLET_EXTRUDER``
    (`bambuddy/extruders.py`, "measured on the maintainer's H2C", mirroring Bambuddy's
    ``utils/fts_routing.py``:
    https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/utils/fts_routing.py), and
    this function takes it as the documented field: with the switch fitted any AMS can
    reach either nozzle, so this is the nozzle the unit rests on, not one the printer
    promises. `lan-report-h2c.json` has no ``info`` on its units, so there the answer is
    ``None``."""
    extruder = extruder_of(ams_id, tray_id, status)
    if extruder is None:
        return None
    nozzle = mounted_nozzles(dict(report)).get(extruder)
    return None if nozzle is None else nozzle.nozzle_diameter or None


def _tray_temps(report: Mapping[str, Any], ams_id: int, tray_id: int) -> tuple[int, int] | None:
    """The nozzle temperatures a tray reports (``nozzle_temp_min`` and ``nozzle_temp_max``,
    strings in `lan-report-h2c.json`), or ``None`` when either is missing, blank, not a
    number or ``0``."""
    block = report.get("ams")
    units = block.get("ams") if isinstance(block, dict) else None
    for unit in units if isinstance(units, list) else []:
        if not isinstance(unit, dict) or str(unit.get("id")) != str(ams_id):
            continue
        trays = unit.get("tray")
        for tray in trays if isinstance(trays, list) else []:
            if not isinstance(tray, dict) or str(tray.get("id")) != str(tray_id):
                continue
            try:
                low, high = int(tray["nozzle_temp_min"]), int(tray["nozzle_temp_max"])
            except (KeyError, TypeError, ValueError):
                return None
            return (low, high) if low > 0 and high > 0 else None
    return None


def slot_params(
    spool: m.Spool,
    presets: Sequence[m.EffectivePreset],
    k_values: Sequence[m.EffectiveKValue],
    printer_id: UUID,
    printer_model: str,
    nozzle: str,
    tray_temps: tuple[int, int] | None,
    extruder: int | None,
) -> SlotConfigure | None:
    """What the slot is configured with: the spool's values and its type's preset for this
    printer model and ``nozzle``. ``None`` when there is no such preset, or it is a
    ScadBuddy preset Bambuddy has no mirror of (#7176; the caller tells both from the rest),
    or the spool has no material or colour to send, or no temperatures: the spool's own,
    else ``tray_temps`` (what the tray reports). The K value is the one of this printer,
    ``extruder`` and nozzle; without one ``cali_idx`` is ``-1`` and the K value is empty."""
    preset = preset_for(presets, printer_model, nozzle)
    # Bambuddy knows a ScadBuddy preset by its mirror's id only (#7176).
    ref = None if preset is None else preset.for_bambuddy()
    effective = spool.effective
    material = spool.names.material
    low, high = effective.nozzle_temp_min, effective.nozzle_temp_max
    if low is None or high is None:
        low, high = tray_temps if tray_temps else (None, None)
    if (
        preset is None
        or ref is None
        or material is None
        or effective.rgba is None
        or low is None
        or high is None
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
    return SlotConfigure(
        tray_info_idx=ref,
        tray_type=material,
        tray_sub_brands=preset.slicer_filament_name or spool.names.subtype or material,
        tray_color=effective.rgba.lstrip("#").upper(),
        nozzle_temp_min=low,
        nozzle_temp_max=high,
        cali_idx=-1 if k is None or k.cali_idx is None else k.cali_idx,
        nozzle_diameter=nozzle,
        setting_id=None if k is None else k.setting_id,
        k_value=None if k is None else float(k.k_value),
    )


def _shows(seen: SeenTray, params: SlotConfigure) -> bool:
    """Whether the report's tray already shows what ``params`` would set."""
    return (
        seen.tray_info_idx == params.tray_info_idx
        and (seen.material or "") == params.tray_type
        and (seen.rgba or "").lstrip("#").upper() == params.tray_color
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
) -> int:
    """Write the row after a configure; ``attempts`` counts the consecutive configures of
    an unchanged hash for the same spool (0 for a new hash or spool), and is returned."""
    with store.spools.transaction() as conn:
        row = conn.execute(
            "INSERT INTO filament_override_slots"
            " (printer_id, ams_id, tray_id, spool_id, params_hash, configured_at)"
            " VALUES (%s, %s, %s, %s, %s, now())"
            " ON CONFLICT (printer_id, ams_id, tray_id) DO UPDATE SET"
            " spool_id = EXCLUDED.spool_id, params_hash = EXCLUDED.params_hash,"
            " configured_at = EXCLUDED.configured_at, last_error = NULL,"
            " attempts = CASE WHEN filament_override_slots.params_hash = EXCLUDED.params_hash"
            " AND filament_override_slots.spool_id = EXCLUDED.spool_id"
            " THEN filament_override_slots.attempts + 1 ELSE 0 END"
            " RETURNING attempts",
            (printer_id, ams_id, tray_id, spool_id, digest),
        ).fetchone()
    assert row is not None
    return int(row["attempts"])


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
    stores no hash, and the next slot goes on. A type with no preset for the nozzle is
    ``no_preset``, a spool with no temperatures of its own and none on the tray (or no
    material or colour) is ``no_temps``, a tray whose nozzle the report does not say is
    ``no_nozzle``; none calls Bambuddy. The spool's temperatures come first; the tray's
    reported ones fill in.

    Not verifiable from a recording: whether the AMS reports the preset's ``tray_info_idx``
    back after a configure. If it does not, the tray never shows what was sent, and every
    refresh configures the slot again. That is idempotent. The consecutive configures of an
    unchanged hash are counted in the row's ``attempts``, and a warning is logged from
    `RECONFIGURE_WARNING` on, so the case shows up; #6270's live sweep checks it.

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
        status = report_status(row, stored, now=now)
        seen = {(tray.ams_id, tray.tray_id): tray for tray in seen_trays(status)}
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
            nozzle = installed_nozzle(status, stored[0], ams_id, tray_id)
            if nozzle is None:
                report.no_nozzle += 1
                continue
            found = preset_for(presets, PRINTER_MODEL, nozzle)
            if found is None or found.for_bambuddy() is None:
                # A ScadBuddy preset with no mirror (#7176) is none Bambuddy can set.
                report.no_preset += 1
                continue
            params = slot_params(
                spool,
                presets,
                k_values,
                printer,
                PRINTER_MODEL,
                nozzle,
                _tray_temps(stored[0], ams_id, tray_id),
                extruder_of(ams_id, tray_id, status),
            )
            if params is None:
                report.no_temps += 1
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
            repeats = await asyncio.to_thread(
                _configured, store, printer, ams_id, tray_id, spool.id, digest
            )
            report.configured += 1
            if repeats >= RECONFIGURE_WARNING:
                logger.warning(
                    "an override slot was configured again with the same values",
                    extra={
                        "printer_id": row.id,
                        "ams_id": ams_id,
                        "tray_id": tray_id,
                        "repeats": repeats,
                    },
                )
        await asyncio.to_thread(_drop_stale, store, printer, keep)
    return report


async def reconcile_slots_now(
    store: Filament,
    load: Callable[[], StoredSettings],
    *,
    printers: PrinterStore,
    reports: PrinterReportStore,
) -> m.SlotReport | None:
    """`reconcile_slots` with a client of its own, one at a time across processes
    (`SLOTS_LOCK`, on a connection of its own); ``None`` (the step is skipped, not a
    failure) when no Bambuddy is configured or another holds the lock."""
    settings = await asyncio.to_thread(load)
    if not settings.bambuddy_url:
        return None
    lock = await acquire_lock(store.spools.conninfo, SLOTS_LOCK)
    if lock is None:
        return None
    try:
        async with client_for(settings) as client:
            return await reconcile_slots(store, client, printers=printers, reports=reports)
    finally:
        await asyncio.to_thread(lock.close)
