"""The AMS slot of an override tag, configured with its type's preset (#6264, #7276).

A tag bound with ``override=True`` says: the spool's own values stand, not what the Bambu
tag says. The AMS reads the tag, so ScadBuddy tells the slot the spool's values over its own
LAN link: the AMS slot configure's commands (`printers/ams_slots.py` `configure_values` and
`configure_commands`, plan 2026-11-25 Task 9, P42), run by a `LanCommandSender`. Bambuddy
is not called. Every step is state-based: the report's tray values are read first, and a
write follows only a difference (`reconcile_slots`)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import status as http

from scadbuddy.bambuddy.catalogue import Catalogue
from scadbuddy.bambuddy.resolver import PRINTER_MODEL
from scadbuddy.bambuddy.trays import seen_trays, tray_at
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.component import Filament
from scadbuddy.filament.presets import effective_k_values, effective_presets, preset_for
from scadbuddy.filament.rfid_push import acquire_lock, error_label
from scadbuddy.printers import ams_slots as sc
from scadbuddy.printers.lan_command_models import LanCommandPending, LanCommandRun
from scadbuddy.printers.lan_commands import SlotParams, UnsupportedUnitError
from scadbuddy.printers.native import LanCommandSender, is_lan_linked, live_status
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.spools.models import SeenTray

logger = logging.getLogger(__name__)

#: The session advisory lock (hashed with ``hashtextextended``) one `reconcile_slots_now`
#: holds, across processes, for the whole step.
SLOTS_LOCK = "scadbuddy:filament:rfid_slots"
#: After this many consecutive configures of a slot whose params did not change, a warning
#: is logged: the AMS may not report back what was sent (see `reconcile_slots`).
RECONFIGURE_WARNING = 3


def _shows(seen: SeenTray, params: SlotParams) -> bool:
    """Whether the report's tray already shows what ``params`` would set."""
    return (
        seen.tray_info_idx == params.tray_info_idx
        and (seen.material or "") == params.tray_type
        and (seen.rgba or "").lstrip("#").upper() == params.tray_color.upper()
    )


def _label(exc: ApiError) -> str:
    """What a failed configure records: the problem's ``code`` (``printer_refused``,
    ``printer_no_answer``, ``printer_unreachable``, ...), else `error_label`. Never the
    message."""
    code = exc.extensions.get("code")
    return code if isinstance(code, str) else error_label(exc)


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
    commands: LanCommandSender,
    *,
    printers: PrinterStore,
    reports: PrinterReportStore,
    catalogue: Catalogue | None,
) -> m.SlotReport:
    """Configure the slot of every override tag a printer's fresh report shows. A printer
    counts when it is linked over the LAN (`is_lan_linked`) and has a fresh report, read
    through `live_status`; its Bambuddy link does not matter. A filled tray counts only
    when its holder was found by its tag (`FilamentInventory.trays`) and the holder spool
    has an open binding with ``override=True``.

    The slot's values are the AMS slot configure's (`ams_slots.configure_values` with the
    spool, its presets and K values, P42): the preset's ids come from the native
    ``catalogue`` (`slot_ids`; with none, every preset is ``no_preset``), and a K profile
    made for another filament is sent as a new
    K value. A counted slot is ``unchanged`` only when both hold: the row's hash equals the
    digest of the params, for the same spool (the hash is an attempt guard: what ScadBuddy
    sent), and the report's tray already shows the params' ``tray_info_idx``, material and
    colour. The AMS re-reads a Bambu tag when it reloads and resets the slot, so the hash is
    never trusted alone. Any difference is configured: `guard` checks the stored report
    first (R-7276-12), and a printer that prints or whose AMS is busy is sent nothing and
    counted nowhere, so a later pass configures it. Then the commands
    (`configure_commands`) run once, and the row is written with the hash and
    ``configured_at``. A run the printer acknowledged is ``configured``; one still open
    (`LanCommandPending`) or with a write it did not acknowledge is ``unconfirmed`` (P36:
    it may be on its way, so it is neither done nor failed) and writes the row as a
    configure does, since the next pass checks the tray anyway. A refused or unanswered
    run (an `ApiError`: ``printer_refused``, ``printer_no_answer``,
    ``printer_unreachable``) records ``attempts`` and ``last_error`` (`_label`), stores no
    hash, and the next slot goes on. A type with no preset for the nozzle, or one whose
    preset has no filament id in the catalogue, is ``no_preset``; a spool with no
    temperatures of its own and none on the tray (or no material or colour) is
    ``no_temps``; a tray whose nozzle the report does not say is ``no_nozzle``; none sends
    a command. The spool's temperatures come first; the tray's reported ones fill in.

    Not verifiable from a recording: whether the AMS reports the preset's ``tray_info_idx``
    back after a configure. If it does not, the tray never shows what was sent, and every
    refresh configures the slot again. That is idempotent. The consecutive configures of an
    unchanged hash are counted in the row's ``attempts``, and a warning is logged from
    `RECONFIGURE_WARNING` on, so the case shows up; #6270's live sweep checks it.

    A slot whose holder no longer counts (its override binding is closed or turned off, or
    the tray has no spool) is sent nothing: ScadBuddy cannot know the tag's own values, and
    the AMS reads a Bambu tag again when the spool is reloaded. Its row is deleted, so that
    a later override on the slot configures again. A spool's deletion cascades its row away;
    closing or turning off a binding does not, so this deletes it. Only a counted printer is
    touched, so a stale report or a printer with no LAN link deletes nothing."""
    report = m.SlotReport()
    now = datetime.now(UTC)
    for row in await asyncio.to_thread(printers.list):
        if not is_lan_linked(row):
            continue
        status = await live_status(reports, row, now=now)
        if not status.connected:
            continue
        printer = UUID(row.id)
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
            tray = tray_at(status, ams_id, tray_id)
            nozzle = sc.nozzle_of(status, ams_id, tray_id)
            if tray is None or nozzle is None:
                report.no_nozzle += 1
                continue
            preset = preset_for(presets, PRINTER_MODEL, nozzle)
            if (
                preset is None
                or catalogue is None
                or sc.slot_ids(catalogue, preset.slicer_filament) is None
            ):
                report.no_preset += 1
                continue
            try:
                params, sent = sc.configure_values(
                    sc.SlotConfigureBody(),
                    status,
                    tray,
                    ams_id,
                    tray_id,
                    printer=printer,
                    spool=spool,
                    presets=presets,
                    k_values=k_values,
                    catalogue=catalogue,
                )
            except ApiError as exc:
                if exc.status != http.HTTP_422_UNPROCESSABLE_CONTENT:
                    raise
                report.no_temps += 1
                continue
            digest = params.digest()
            if (
                slot is not None
                and slot["spool_id"] == spool.id
                and slot["params_hash"] == digest
                and _shows(seen[ams_id, tray_id], params)
            ):
                report.unchanged += 1
                continue
            try:
                sc.guard(status, "configure", ams_id, tray_id)
            except ApiError as exc:
                logger.info(
                    "an override slot waits for its printer",
                    extra={
                        "printer_id": row.id,
                        "ams_id": ams_id,
                        "tray_id": tray_id,
                        "reason": _label(exc),
                    },
                )
                continue
            try:
                run = LanCommandRun(
                    commands=sc.configure_commands(
                        params,
                        ams_id,
                        tray_id,
                        external_count=len(status.vt_tray),
                        printer_id=row.id,
                    ),
                    answer=sent.model_dump(mode="json"),
                )
                answer = await commands.run(row.id, run)
            except (ApiError, UnsupportedUnitError) as exc:
                label = _label(exc) if isinstance(exc, ApiError) else "unsupported_unit"
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
            if isinstance(answer, LanCommandPending) or not all(
                reply.confirmed for reply in answer.replies
            ):
                report.unconfirmed += 1
            else:
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
    *,
    printers: PrinterStore,
    reports: PrinterReportStore,
    commands: LanCommandSender,
    profiles: Profiles | None,
) -> m.SlotReport | None:
    """`reconcile_slots` on the native catalogue of ``profiles`` (with ScadBuddy's own
    presets, `catalogue_for`; with no slicer profiles, none, so a slot with a preset is
    ``no_preset``), one at a time across processes (`SLOTS_LOCK`, on a connection of its
    own). ``None`` (the step is skipped, not a failure) only when another holds the lock."""
    lock = await acquire_lock(store.spools.conninfo, SLOTS_LOCK)
    if lock is None:
        return None
    try:
        catalogue = None if profiles is None else await catalogue_for(None, native=profiles)
        return await reconcile_slots(
            store, commands, printers=printers, reports=reports, catalogue=catalogue
        )
    finally:
        await asyncio.to_thread(lock.close)
