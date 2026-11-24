"""The spool refresh's activities, on the ``library`` worker (`main._run_library_worker`):
Bambuddy's spools pulled into ScadBuddy's own (`filament_pull_activity`, #6261; read on the
print worker since #6772), then, while ``filament_sync.presets`` pulls, Bambuddy's filament
presets into ScadBuddy's own (#7244).

The spool providers, their overlay and the trays it recorded are gone (#6262 Task 12),
and so is the kept copy of Bambuddy's local presets (#7244). Their activities stay
registered for a run opened before ``spool-providers-gone``
(`workflows/spool_refresh.py`), which still calls them on the new code: no providers, so
no refresh, no trays to record, and no copy to keep.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from temporalio import activity

from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import capture
from scadbuddy.filament.bb_pull_apply import pull_start
from scadbuddy.filament.bb_pull_models import PullOutcome, PullStart
from scadbuddy.filament.component import Filament
from scadbuddy.filament.rfid_mode import BUSY, NO_BAMBUDDY, reconcile_mode_now
from scadbuddy.filament.rfid_push import push_pending
from scadbuddy.filament.rfid_slots import reconcile_slots_now
from scadbuddy.filament.sb_presets import PresetImport
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.component import Printers
from scadbuddy.workflows.spool_refresh import (
    CAPTURE_ACTIVITY,
    FILAMENT_PULL_ACTIVITY,
    FILAMENT_PULL_TIMEOUT,
    FILAMENT_RFID_ACTIVITY,
    HELD_ACTIVITY,
    LIST_ACTIVITY,
    PROVIDER_ACTIVITY,
    SEEN_ACTIVITY,
)


def spool_activities() -> list[Callable[..., Any]]:
    """The retired activities, for runs opened before ``spool-providers-gone`` (and, for
    the kept presets' step, before ``spool-refresh-held-gone``, #7244). Each answers with
    its old result type, so such a run replays and ends."""

    @activity.defn(name=HELD_ACTIVITY)
    async def held_presets_refresh() -> str:
        # spool-refresh-held-gone: no copy of Bambuddy's local presets is kept (#7244).
        return "skipped"

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

    return [
        held_presets_refresh,
        spool_providers_list,
        spool_provider_refresh,
        spool_trays_seen,
    ]


#: What the presets import leaves of the pull activity's timeout (`FILAMENT_PULL_TIMEOUT`),
#: so the activity answers before Temporal times it out.
IMPORT_MARGIN_S = 15.0


def filament_pull_activity(
    filament: Filament,
    load: Callable[[], StoredSettings],
    sync: Callable[[], FilamentSync],
    execute: Callable[[PullStart], Awaitable[PullOutcome]],
    import_presets: Callable[[], Awaitable[PresetImport]] | None = None,
    mirror: Callable[[], Awaitable[int]] | None = None,
) -> Callable[[], Any]:
    """``filament_bambuddy_pull``: Bambuddy's spools into ScadBuddy's own. It runs on the
    ``library`` worker, in the API process, and reads no Bambuddy (#6772): it decides what
    the pull reads (`bb_pull_apply.pull_start`) and has the print worker read it,
    through ``execute`` (`bb_pull_apply.execute_pull`: ``FilamentBbPull`` on the
    ``bambuddy`` queue, which sends what it read to the API's CRUD routes). With no
    Bambuddy, or when no data type of ``filament_sync`` pulls (``sync`` reads it) and no
    Home Assistant sensor is set up, it starts nothing and answers ``skipped``. Whenever
    Bambuddy is configured and a Home Assistant sensor is set up, the worker reads the
    sensors' states, even when nothing pulls. It answers the worker's ``pulled``,
    ``skipped`` or ``failed``, and ``failed`` when the settings could not be read or the run
    could not be started or ended (logged): it never raises, so the run never fails on it.

    Then, when the pull takes presets (``filament_sync.presets`` ``bb_to_sb`` or
    ``two_way``, and the links are the configured Bambuddy's), ``import_presets`` brings
    Bambuddy's filament presets into ScadBuddy's own (`filament/sb_presets.py`
    `import_presets`, #7244), whatever the pull answered. It has what is left of the
    activity's timeout, less `IMPORT_MARGIN_S`; one cut short keeps what it made, and the
    next tick goes on. A failure is logged and changes nothing in the answer. With
    ``presets`` ``off`` or ``sb_to_bb`` nothing is imported.

    Last, whatever the pull did and whatever the direction is, ``mirror`` sends
    ScadBuddy's own presets that Bambuddy has not taken yet (`filament/preset_mirror.py`
    `mirror_pending`, #7176: it sends only while the stored setting
    ``filament_preset_mirror`` is on), within what is left of the timeout. It never
    reads Bambuddy's presets into ScadBuddy."""

    async def pull(deadline: float) -> str:
        try:
            start = await pull_start(filament, load, sync)
            if start is None:
                return "skipped"
        except Exception as error:
            activity.logger.warning(
                "could not pull Bambuddy's spools", extra={"error": type(error).__name__}
            )
            return "failed"
        try:
            outcome = (await execute(start)).outcome
        except Exception as error:
            activity.logger.warning(
                "could not pull Bambuddy's spools", extra={"error": type(error).__name__}
            )
            outcome = "failed"
        if import_presets is not None and start.pulls is not None and start.pulls.presets:
            await _import_presets(import_presets, deadline - time.monotonic())
        return outcome

    @activity.defn(name=FILAMENT_PULL_ACTIVITY)
    async def filament_bambuddy_pull() -> str:
        deadline = time.monotonic() + FILAMENT_PULL_TIMEOUT.total_seconds() - IMPORT_MARGIN_S
        outcome = await pull(deadline)
        if mirror is not None:
            await _mirror(mirror, deadline - time.monotonic())
        return outcome

    return filament_bambuddy_pull


async def _mirror(run: Callable[[], Awaitable[int]], budget: float) -> None:
    """The presets mirror within ``budget`` seconds; never raises."""
    if budget <= 0:
        activity.logger.warning("no time was left to mirror ScadBuddy's presets")
        return
    try:
        await asyncio.wait_for(run(), budget)
    except Exception as error:
        activity.logger.warning(
            "could not mirror ScadBuddy's presets", extra={"error": type(error).__name__}
        )


async def _import_presets(run: Callable[[], Awaitable[PresetImport]], budget: float) -> None:
    """The pull's presets import within ``budget`` seconds; never raises."""
    if budget <= 0:
        activity.logger.warning(
            "no time was left to import Bambuddy's presets; the next pull goes on"
        )
        return
    try:
        done = await asyncio.wait_for(run(), budget)
    except TimeoutError:
        activity.logger.warning(
            "ran out of time importing Bambuddy's presets; the next pull goes on",
            extra={"budget_s": round(budget, 1)},
        )
        return
    except Exception as error:
        activity.logger.warning(
            "could not import Bambuddy's presets", extra={"error": type(error).__name__}
        )
        return
    if done.adopted or done.updated:
        activity.logger.info(
            "imported Bambuddy's presets",
            extra={"adopted": done.adopted, "updated": done.updated},
        )


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
