"""The spool refresh's activities, on the ``library`` worker (`main._run_library_worker`):
Bambuddy's spools pulled into ScadBuddy's own (`filament_pull_activity`, #6261). The
kept presets' activity is `slicing/held_presets.py`'s.

The spool providers, their overlay and the trays it recorded are gone (#6262 Task 12).
Their three activities stay registered for a run opened before ``spool-providers-gone``
(`workflows/spool_refresh.py`), which still calls them on the new code: no providers, so
no refresh, and no trays to record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from temporalio import activity

from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import capture
from scadbuddy.filament.bambuddy_pull import scheduled_pull
from scadbuddy.filament.component import Filament
from scadbuddy.filament.rfid_mode import BUSY, NO_BAMBUDDY, reconcile_mode_now
from scadbuddy.filament.rfid_push import push_pending
from scadbuddy.filament.rfid_slots import reconcile_slots_now
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.component import Printers
from scadbuddy.workflows.spool_refresh import (
    CAPTURE_ACTIVITY,
    FILAMENT_PULL_ACTIVITY,
    FILAMENT_RFID_ACTIVITY,
    LIST_ACTIVITY,
    PROVIDER_ACTIVITY,
    SEEN_ACTIVITY,
)


def spool_activities() -> list[Callable[..., Any]]:
    """The retired activities, for runs opened before ``spool-providers-gone``. Each
    answers with its old result type, so such a run replays and ends."""

    @activity.defn(name=LIST_ACTIVITY)
    async def spool_providers_list() -> list[str]:
        # spool-providers-gone: no provider is left to list.
        return []

    @activity.defn(name=PROVIDER_ACTIVITY)
    async def spool_provider_refresh(name: str) -> str:
        # spool-providers-gone: never called after an empty list; kept for its name.
        return "skipped"

    @activity.defn(name=SEEN_ACTIVITY)
    async def spool_trays_seen() -> None:
        # spool-providers-gone: no overlay reads what the trays show.
        return None

    return [spool_providers_list, spool_provider_refresh, spool_trays_seen]


def filament_pull_activity(
    filament: Filament, load: Callable[[], StoredSettings], sync: Callable[[], FilamentSync]
) -> Callable[[], Any]:
    """``filament_bambuddy_pull``: Bambuddy's spools into ScadBuddy's own
    (`filament/bambuddy_pull.py`). It runs on the ``library`` worker, in the API process,
    which holds the store (`main._run_library_worker`), so it writes it directly. When no
    data type of ``filament_sync`` pulls (``sync`` reads it), with no Bambuddy, or with
    Bambuddy down, it pulls nothing: it logs and returns, so the run never fails on it.
    Before that gate, whenever Bambuddy is configured, it reads the Home Assistant sensors
    and writes their readings, even when nothing pulls."""

    @activity.defn(name=FILAMENT_PULL_ACTIVITY)
    async def filament_bambuddy_pull() -> str:
        return await scheduled_pull(filament, load, sync)

    return filament_bambuddy_pull


def capture_backstop_activity(printers: Printers) -> Callable[[], Any]:
    """``filament_capture_backstop``: every printer's latest fresh report reconciled with
    the ledger (`filament/capture.reconcile_all`), for what the printer watch missed. It
    is the successor of ``spool_trays_seen``, a no-op since #6262. It returns
    ``captured`` or ``failed`` and never raises, so the run never fails on it."""

    @activity.defn(name=CAPTURE_ACTIVITY)
    async def filament_capture_backstop() -> str:
        try:
            store = printers.store
            ids = [row.id for row in await asyncio.to_thread(store.list)]
            await asyncio.to_thread(capture.reconcile_all, store.pool, ids, now=datetime.now(UTC))
        except Exception as error:
            activity.logger.warning(
                "could not reconcile the printers' trays", extra={"error": type(error).__name__}
            )
            return "failed"
        return "captured"

    return filament_capture_backstop


def filament_rfid_activity(
    printers: Printers,
    filament: Filament,
    load: Callable[[], StoredSettings],
    sync: Callable[[], FilamentSync],
) -> Callable[[], Any]:
    """``filament_rfid_reconcile``: Bambuddy's auto-add of unknown RFID spools brought to
    the override mode (`filament/rfid_mode.reconcile_mode`), then ScadBuddy's tag bindings
    told to Bambuddy (`filament/rfid_push.drain_tags`), then the AMS slot of each override
    tag configured (`filament/rfid_slots.reconcile_slots`, #6264). Each step fails alone; the
    activity returns ``done`` or ``failed`` and never raises, so the run never fails on it.
    A mode left ``pending`` is a failure; ``blocked`` is shown on the status, not one."""

    @activity.defn(name=FILAMENT_RFID_ACTIVITY)
    async def filament_rfid_reconcile() -> str:
        failed = False
        try:
            mode = await reconcile_mode_now(filament.rfid, load)
            # No Bambuddy to reach, or another reconcile running (it does the work): not a failure.
            failed = mode.state == "pending" and mode.detail not in (NO_BAMBUDDY, BUSY)
        except Exception as error:
            activity.logger.warning(
                "could not reconcile Bambuddy's RFID auto-add",
                extra={"error": type(error).__name__},
            )
            failed = True
        try:
            report = await push_pending(filament, load, sync)
            failed = failed or report.failed > 0
        except Exception as error:
            activity.logger.warning(
                "could not push the tags to Bambuddy", extra={"error": type(error).__name__}
            )
            failed = True
        try:
            slots = await reconcile_slots_now(
                filament, load, printers=printers.store, reports=printers.reports
            )
            failed = failed or (slots is not None and slots.failed > 0)
        except Exception as error:
            activity.logger.warning(
                "could not configure the override slots", extra={"error": type(error).__name__}
            )
            failed = True
        return "failed" if failed else "done"

    return filament_rfid_reconcile
