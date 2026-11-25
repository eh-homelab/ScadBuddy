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
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
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
from scadbuddy.filament.rfid_push import acquire_lock, error_label, failure_label
from scadbuddy.printers import ams_slots as sc
from scadbuddy.printers.lan_command_models import LanCommandPending, LanCommandRun
from scadbuddy.printers.lan_commands import SlotParams, UnsupportedUnitError
from scadbuddy.printers.native import LanCommandSender, is_lan_linked, live_status
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.spools.models import SeenTray
from scadbuddy.workflows.print_models import LAN_ROUTE_WAIT_S

logger = logging.getLogger(__name__)

#: The session advisory lock (hashed with ``hashtextextended``) one `reconcile_slots_now`
#: holds, across processes, for the whole step.
SLOTS_LOCK = "scadbuddy:filament:rfid_slots"
#: After this many consecutive acknowledged configures of a slot whose params did not
#: change, a warning is logged: the AMS may not report back what was sent (see
#: `reconcile_slots`).
RECONFIGURE_WARNING = 3
#: At most this many slots' runs are open at once in one pass (P46), and at most one per
#: printer (P58): each run opens an MQTT connection of its own, and a printer has few LAN
#: client slots. Each run waits up to `LAN_ROUTE_WAIT_S`, so a pass waits about one of those
#: per slot of its busiest printer, until its deadline.
MAX_CONCURRENT_RUNS = 8
#: The reconcile route's budget, in seconds from the request's start, under Envoy's 15 s
#: route timeout (P46): a slot's run starts only while the run's whole wait still ends
#: within it (`start_deadline`).
ROUTE_BUDGET_S = 12.0
#: What the ``filament_rfid_reconcile`` activity keeps back of its ``start_to_close``
#: timeout (`FILAMENT_RFID_TIMEOUT`) for the writes after the last run (P46).
ACTIVITY_MARGIN_S = 30.0
#: ``last_error`` of a slot whose configure was sent but not acknowledged (P48).
UNCONFIRMED = "unconfirmed"
#: ``last_error`` of a slot on a unit the LAN commands cannot address (P22).
UNSUPPORTED_UNIT = "unsupported_unit"

#: Builds the native catalogue; `reconcile_slots` calls it at most once, at the first slot
#: that needs it.
CatalogueFactory = Callable[[], Awaitable[Catalogue | None]]


def start_deadline(started: float, budget: float) -> float:
    """The `time.monotonic` instant from which a pass that began at ``started`` with
    ``budget`` seconds starts no more runs: a run started later may wait `LAN_ROUTE_WAIT_S`
    past the budget."""
    return started + budget - LAN_ROUTE_WAIT_S


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
            "SELECT spool_id, params_hash, last_error, run_key, configured_at"
            " FROM filament_override_slots"
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
    store: Filament,
    printer_id: UUID,
    ams_id: int,
    tray_id: int,
    spool_id: UUID,
    digest: str,
    *,
    repeat: bool,
    confirmed: bool,
    run_key: str | None,
) -> int:
    """Write the row after a configure and return its ``attempts``: the consecutive
    acknowledged configures of an unchanged hash for the same spool. It is 0 when
    ``repeat`` is false (`_repeat`, read before `_started` wrote the hash): a new hash or
    spool, or a row whose last outcome was not an acknowledged configure (a failed run, an
    unacknowledged one, or a run that only `_started` recorded). An unacknowledged one
    (``confirmed`` false) does not count, writes ``last_error`` `UNCONFIRMED`, and keeps
    ``run_key`` while its run is still open."""
    with store.spools.transaction() as conn:
        row = conn.execute(
            "INSERT INTO filament_override_slots"
            " (printer_id, ams_id, tray_id, spool_id, params_hash, configured_at, last_error,"
            " run_key)"
            " VALUES (%s, %s, %s, %s, %s, now(), %s, %s)"
            " ON CONFLICT (printer_id, ams_id, tray_id) DO UPDATE SET"
            " spool_id = EXCLUDED.spool_id, params_hash = EXCLUDED.params_hash,"
            " configured_at = EXCLUDED.configured_at, last_error = EXCLUDED.last_error,"
            " run_key = EXCLUDED.run_key,"
            " attempts = CASE WHEN %s THEN filament_override_slots.attempts + %s ELSE 0 END"
            " RETURNING attempts",
            (
                printer_id,
                ams_id,
                tray_id,
                spool_id,
                digest,
                None if confirmed else UNCONFIRMED,
                run_key,
                repeat,
                1 if confirmed else 0,
            ),
        ).fetchone()
    assert row is not None
    return int(row["attempts"])


def _failed(
    store: Filament,
    printer_id: UUID,
    ams_id: int,
    tray_id: int,
    spool_id: UUID,
    label: str,
    digest: str | None = None,
    run_key: str | None = None,
) -> None:
    """Record a configure that did not happen, or whose outcome is not known:
    ``attempts`` and ``last_error``. The hash is stored only with ``digest`` (an
    `UNSUPPORTED_UNIT` slot, so the next pass knows it was recorded, or a run that may still
    be open); else none. ``run_key`` is kept for the next pass to join (`_run_key`)."""
    with store.spools.transaction() as conn:
        conn.execute(
            "INSERT INTO filament_override_slots"
            " (printer_id, ams_id, tray_id, spool_id, params_hash, attempts, last_error,"
            " run_key)"
            " VALUES (%s, %s, %s, %s, %s, 1, %s, %s)"
            " ON CONFLICT (printer_id, ams_id, tray_id) DO UPDATE SET"
            " spool_id = EXCLUDED.spool_id, params_hash = EXCLUDED.params_hash,"
            " attempts = filament_override_slots.attempts + 1, run_key = EXCLUDED.run_key,"
            " last_error = EXCLUDED.last_error",
            (printer_id, ams_id, tray_id, spool_id, digest, label, run_key),
        )


def _started(
    store: Filament,
    printer_id: UUID,
    ams_id: int,
    tray_id: int,
    spool_id: UUID,
    digest: str,
    run_key: str,
) -> None:
    """Write the run's key and the params' hash before the run is sent (I1), so a pass
    that is cancelled, or whose task raises, while it waits for the answer leaves a key
    the next pass joins (`_run_key`) rather than sending the commands again. ``attempts``
    restarts at 0, and ``configured_at`` is cleared, for a new hash or spool;
    ``last_error`` is left for the outcome."""
    with store.spools.transaction() as conn:
        conn.execute(
            "INSERT INTO filament_override_slots"
            " (printer_id, ams_id, tray_id, spool_id, params_hash, run_key)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (printer_id, ams_id, tray_id) DO UPDATE SET"
            " attempts = CASE WHEN filament_override_slots.params_hash"
            " IS NOT DISTINCT FROM EXCLUDED.params_hash"
            " AND filament_override_slots.spool_id = EXCLUDED.spool_id"
            " THEN filament_override_slots.attempts ELSE 0 END,"
            " configured_at = CASE WHEN filament_override_slots.params_hash"
            " IS NOT DISTINCT FROM EXCLUDED.params_hash"
            " AND filament_override_slots.spool_id = EXCLUDED.spool_id"
            " THEN filament_override_slots.configured_at ELSE NULL END,"
            " spool_id = EXCLUDED.spool_id, params_hash = EXCLUDED.params_hash,"
            " run_key = EXCLUDED.run_key",
            (printer_id, ams_id, tray_id, spool_id, digest, run_key),
        )


def _settled(store: Filament, printer_id: UUID, ams_id: int, tray_id: int) -> None:
    """Clear an `UNCONFIRMED` mark once the tray shows what was sent."""
    with store.spools.transaction() as conn:
        conn.execute(
            "UPDATE filament_override_slots SET last_error = NULL, run_key = NULL"
            " WHERE printer_id = %s AND ams_id = %s AND tray_id = %s",
            (printer_id, ams_id, tray_id),
        )


def _repeat(slot: dict[str, Any] | None, spool_id: UUID, digest: str) -> bool:
    """Whether the row's last outcome was an acknowledged configure of ``digest`` for
    ``spool_id`` (N1). `_started` and a failed run (``last_error`` ``internal``) keep the
    hash with no acknowledged configure, and an unacknowledged one is `UNCONFIRMED`: none
    of them is a repeat."""
    return (
        slot is not None
        and slot["spool_id"] == spool_id
        and slot["params_hash"] == digest
        and slot["configured_at"] is not None
        and slot["last_error"] not in ("internal", UNCONFIRMED)
    )


def _run_key(ams_id: int, tray_id: int, digest: str, slot: dict[str, Any] | None) -> str:
    """The key of a slot's configure run (P47). A run left open by an earlier pass for the
    same spool and params is joined by its kept key (``run_key``). Otherwise a new key:
    the slot, the params' digest and a nonce, since a key whose run has closed reads that
    run's outcome and never sends again (`execute_lan_commands`), and a tray the AMS reset
    must be sent the same params again. The printer's id is in the run's workflow id
    already (`lan_command_id`)."""
    if slot is not None and slot["run_key"] and slot["params_hash"] == digest:
        return str(slot["run_key"])
    return f"slot-{ams_id}-{tray_id}-{digest[:16]}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class _Send:
    """One slot's configure, decided and ready to run."""

    printer_id: str
    printer: UUID
    ams_id: int
    tray_id: int
    spool: UUID
    digest: str
    run: LanCommandRun
    key: str
    #: Whether the row's last outcome was an acknowledged configure of this hash for this
    #: spool (`_repeat`, `_configured`).
    repeat: bool


async def reconcile_slots(
    store: Filament,
    commands: LanCommandSender,
    *,
    printers: PrinterStore,
    reports: PrinterReportStore,
    catalogue: CatalogueFactory,
    deadline: float,
) -> m.SlotReport:
    """Configure the slot of every override tag a printer's fresh report shows. A printer
    counts when it is linked over the LAN (`is_lan_linked`) and has a fresh report, read
    through `live_status`; its Bambuddy link does not matter. A filled tray counts only
    when its holder was found by its tag (`FilamentInventory.trays`) and the holder spool
    has an open binding with ``override=True``.

    The slot's values are the AMS slot configure's (`ams_slots.configure_values` with the
    spool, its presets and K values, P42): the preset's ids come from the native catalogue
    that ``catalogue`` builds, called once at the first slot with a preset, never in a pass
    with none (`slot_ids`; with none, every preset is ``no_preset``), and a K profile made
    for another filament is sent as a new K value. A counted slot is ``unchanged`` only
    when both hold: the row's hash equals the digest of the params, for the same spool (the
    hash is an attempt guard: what ScadBuddy sent), and the report's tray already shows the
    params' ``tray_info_idx``, material and colour. The AMS re-reads a Bambu tag when it
    reloads and resets the slot, so the hash is never trusted alone. Any difference is
    configured: `guard` checks the stored report first (R-7276-12), and a printer that
    prints, or whose AMS is busy, is sent nothing and counts ``waiting``, so a later pass
    configures it. A printer with no fresh report is skipped before that and counted nowhere.

    The runs (`configure_commands`) are sent concurrently, at most `MAX_CONCURRENT_RUNS` at
    once (P46) and one per printer (P58), each under its slot's key (`_run_key`, P47),
    which the row takes before the run is sent (`_started`), so a pass waits about one
    `LAN_ROUTE_WAIT_S` per slot of its busiest printer. ``deadline`` (`time.monotonic`,
    see `start_deadline`) is when the pass starts no more runs: a slot not started by then
    counts ``waiting``. A run the printer acknowledged is ``configured``; one still open
    (`LanCommandPending`) or with a write it did not acknowledge is ``unconfirmed`` (P36,
    P48: it may be on its way, so it is neither done nor failed). Both write the row with
    the hash and ``configured_at``, since the next pass checks the tray anyway; an
    unconfirmed one writes ``last_error`` `UNCONFIRMED`, keeps the open run's key for the
    next pass to join, logs a warning of its own, and is not a repeat
    (`RECONFIGURE_WARNING`). A refused or unanswered run (an `ApiError`:
    ``printer_refused``, ``printer_no_answer``, ``printer_unreachable``) records
    ``attempts`` and ``last_error`` (`_label`), stores no hash, and counts ``failed``. Any
    other error of a run counts ``failed`` too, with ``last_error`` ``internal``, and keeps
    the hash and the key for the next pass to join; the other slots go on. A unit the LAN
    commands cannot address (`UnsupportedUnitError`, P22) counts ``unsupported_unit``, is
    never sent, and its row is written once per hash.

    A type with no preset for the nozzle, or one whose preset has no filament id in the
    catalogue, is ``no_preset``; a spool with no temperatures of its own and none on the
    tray (or no material or colour) is ``no_temps``; temperatures whose minimum is above
    their maximum are ``invalid_temps``; a tray whose nozzle the report does not say is
    ``no_nozzle``; none sends a command. The spool's temperatures come first; the tray's
    reported ones fill in.

    Not verifiable from a recording: whether the AMS reports the preset's ``tray_info_idx``
    back after a configure. If it does not, the tray never shows what was sent, and every
    refresh configures the slot again. That is idempotent. The consecutive acknowledged
    configures of an unchanged hash are counted in the row's ``attempts``, and a warning is
    logged from `RECONFIGURE_WARNING` on, so the case shows up; #6270's live sweep checks
    it.

    A slot whose holder no longer counts (its override binding is closed or turned off, or
    the tray has no spool) is sent nothing: ScadBuddy cannot know the tag's own values, and
    the AMS reads a Bambu tag again when the spool is reloaded. Its row is deleted, so that
    a later override on the slot configures again. A spool's deletion cascades its row away;
    closing or turning off a binding does not, so this deletes it. Only a counted printer is
    touched, so a stale report or a printer with no LAN link deletes nothing."""
    report = m.SlotReport()
    now = datetime.now(UTC)
    built: list[Catalogue | None] = []

    async def native() -> Catalogue | None:
        if not built:
            built.append(await catalogue())
        return built[0]

    sends: list[_Send] = []
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
            catalogue_now = None if preset is None else await native()
            if (
                preset is None
                or catalogue_now is None
                or sc.slot_ids(catalogue_now, preset.slicer_filament) is None
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
                    catalogue=catalogue_now,
                )
            except ApiError as exc:
                if exc.status != http.HTTP_422_UNPROCESSABLE_CONTENT:
                    raise
                if exc.extensions.get("code") == sc.INVALID_TEMPS:
                    report.invalid_temps += 1
                else:
                    report.no_temps += 1
                continue
            digest = params.digest()
            same = slot is not None and slot["spool_id"] == spool.id
            if (
                same
                and slot is not None
                and slot["params_hash"] == digest
                and _shows(seen[ams_id, tray_id], params)
            ):
                if slot["last_error"] == UNCONFIRMED or slot["run_key"]:
                    await asyncio.to_thread(_settled, store, printer, ams_id, tray_id)
                report.unchanged += 1
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
            except UnsupportedUnitError:
                report.unsupported_unit += 1
                recorded = (
                    same
                    and slot is not None
                    and slot["params_hash"] == digest
                    and slot["last_error"] == UNSUPPORTED_UNIT
                )
                if not recorded:
                    logger.warning(
                        "an override slot is on a unit ScadBuddy cannot configure",
                        extra={"printer_id": row.id, "ams_id": ams_id, "tray_id": tray_id},
                    )
                    await asyncio.to_thread(
                        _failed,
                        store,
                        printer,
                        ams_id,
                        tray_id,
                        spool.id,
                        UNSUPPORTED_UNIT,
                        digest,
                    )
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
                report.waiting += 1
                continue
            sends.append(
                _Send(
                    printer_id=row.id,
                    printer=printer,
                    ams_id=ams_id,
                    tray_id=tray_id,
                    spool=spool.id,
                    digest=digest,
                    run=run,
                    key=_run_key(ams_id, tray_id, digest, slot if same else None),
                    repeat=_repeat(slot, spool.id, digest),
                )
            )
        await asyncio.to_thread(_drop_stale, store, printer, keep)

    gate = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
    # One run open per printer at once (P58); a slot waits for its printer before it takes
    # a place under the global cap, so one printer's slots never hold the others' places.
    per_printer = {job.printer_id: asyncio.Semaphore(1) for job in sends}

    async def send(job: _Send) -> None:
        async with per_printer[job.printer_id], gate:
            if time.monotonic() >= deadline:
                report.waiting += 1
                return
            await _send(store, commands, job, report, deadline)

    # A TaskGroup, not gather: when the pass ends, or is cancelled, no slot's task is still
    # running, so none writes its row after the caller releases SLOTS_LOCK. `_send` records
    # every error of a slot's send (the run and `_started`) but a cancel, so such an error
    # never cancels the other slots. A database error in `_configured` or `_failed` still
    # raises and cancels the siblings; their keys are already stored, so nothing is sent
    # twice. A cancelled task's run may stay open on Temporal: its row already holds the
    # run's key (`_started`), and the next pass joins that run.
    async with asyncio.TaskGroup() as group:
        for job in sends:
            group.create_task(send(job))
    return report


async def _send(
    store: Filament,
    commands: LanCommandSender,
    job: _Send,
    report: m.SlotReport,
    deadline: float,
) -> None:
    """Run one slot's configure and record what came of it (`reconcile_slots`). The row
    takes the run's key first (`_started`). When that write takes the pass to ``deadline``
    (N2), the run is not sent: the slot counts ``waiting``, and its row keeps the key for
    the next pass. A problem (`ApiError`) is the run's closed
    outcome: the row drops the key and the hash. Any other error (a Temporal RPC error, a
    database error) may leave the run open: the row keeps the key and the hash, so the next
    pass joins the run. Both count ``failed``. A cancel is not caught."""
    where = {"printer_id": job.printer_id, "ams_id": job.ams_id, "tray_id": job.tray_id}
    try:
        await asyncio.to_thread(
            _started, store, job.printer, job.ams_id, job.tray_id, job.spool, job.digest, job.key
        )
        if time.monotonic() >= deadline:
            report.waiting += 1
            return
        answer = await commands.run(job.printer_id, job.run, key=job.key)
    except ApiError as exc:
        label = _label(exc)
        logger.warning("could not configure an override slot", extra={**where, "error": label})
        await asyncio.to_thread(
            _failed, store, job.printer, job.ams_id, job.tray_id, job.spool, label
        )
        report.failed += 1
        return
    except Exception as exc:
        label = failure_label(exc)
        logger.warning(
            "could not configure an override slot",
            extra={**where, "error": label},
            exc_info=True,
        )
        await asyncio.to_thread(
            _failed,
            store,
            job.printer,
            job.ams_id,
            job.tray_id,
            job.spool,
            label,
            job.digest,
            job.key,
        )
        report.failed += 1
        return
    pending = isinstance(answer, LanCommandPending)
    confirmed = not isinstance(answer, LanCommandPending) and all(
        reply.confirmed for reply in answer.replies
    )
    repeats = await asyncio.to_thread(
        _configured,
        store,
        job.printer,
        job.ams_id,
        job.tray_id,
        job.spool,
        job.digest,
        repeat=job.repeat,
        confirmed=confirmed,
        run_key=job.key if pending else None,
    )
    if not confirmed:
        report.unconfirmed += 1
        logger.warning(
            "an override slot's configure was sent, not acknowledged",
            extra={**where, "pending": pending},
        )
        return
    report.configured += 1
    if repeats >= RECONFIGURE_WARNING:
        logger.warning(
            "an override slot was configured again with the same values",
            extra={**where, "repeats": repeats},
        )


async def reconcile_slots_now(
    store: Filament,
    *,
    printers: PrinterStore,
    reports: PrinterReportStore,
    commands: LanCommandSender,
    profiles: Profiles | None,
    deadline: float,
) -> m.SlotReport | None:
    """`reconcile_slots` on the native catalogue of ``profiles`` (with ScadBuddy's own
    presets, `catalogue_for`, built only when a slot needs it; with no slicer profiles,
    none, so a slot with a preset is ``no_preset``), one at a time across processes
    (`SLOTS_LOCK`, on a connection of its own), starting no run from ``deadline`` on.
    ``None`` (the step is skipped, not a failure) only when another holds the lock."""
    lock = await acquire_lock(store.spools.conninfo, SLOTS_LOCK)
    if lock is None:
        return None

    async def native() -> Catalogue | None:
        return None if profiles is None else await catalogue_for(None, native=profiles)

    try:
        return await reconcile_slots(
            store,
            commands,
            printers=printers,
            reports=reports,
            catalogue=native,
            deadline=deadline,
        )
    finally:
        await asyncio.to_thread(lock.close)
