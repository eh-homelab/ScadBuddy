"""The Bambuddy instance a sync pass belongs to, and the nudge that tells its workflow SB
changed (#6265, plan `docs/superpowers/plans/2026-11-18-filament-bb-sync.md` Decisions 12
and 17).

`FilamentSyncNudger.nudge` does a signal-with-start of ``FilamentBbSync``
(`workflows/filament_sync.py`) on the ``bambuddy`` queue, where the print worker
registers both the workflow and its pass activity. The API's writes nudge through
`FilamentSyncNudger.nudge_soon`, in a task of its own, so a request never waits on
Temporal. The names the start uses live here and the workflow imports them, since the
workflow's module imports the pass, and the pass imports `instance_for` from here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Collection, Mapping
from typing import Any

from pydantic import BaseModel
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from scadbuddy.filament.bb_sync_models import skip_reason

logger = logging.getLogger(__name__)

FILAMENT_BB_SYNC_WORKFLOW = "FilamentBbSync"
CHANGED_SIGNAL = "changed"
#: The whole nudge's bound: the settings read and the signal-with-start (Decision 17).
NUDGE_TIMEOUT = 5.0
#: What a nudge reads: the instance, the timer's interval a new workflow starts with, and
#: the directions (nothing pushes: no workflow, R-T8-2).
NUDGE_SETTINGS = frozenset({"bambuddy_url", "spool_overlay_refresh_seconds", "filament_sync"})


class SyncStart(BaseModel, extra="forbid"):
    """The workflow's input. ``interval_s`` is the timer's (0: no timer, signals only).
    ``pending`` and ``full`` carry a signal the last run had not served yet across a
    continue-as-new: ``pending`` that one came, ``full`` that one asked for a full pass."""

    instance: str
    interval_s: float = 300
    pending: bool = False
    full: bool = False


def instance_for(url: str) -> str:
    """The instance id of the Bambuddy at ``url``: the first 12 hex of the SHA-256 of the
    URL without a trailing slash, in lower case. The links belong to one instance, and a
    pass for another one plans nothing (Decision 12)."""
    return hashlib.sha256(url.rstrip("/").lower().encode()).hexdigest()[:12]


def workflow_id(instance: str) -> str:
    """One sync workflow per Bambuddy instance (Decision 12)."""
    return f"filament-bb-sync-{instance}"


class FilamentSyncNudger:
    """``client_getter`` gives the Temporal client (the API's lazy one); ``load_settings``
    is `SettingsStore.read` (blocking, so run in a thread); ``task_queue`` is the
    ``bambuddy`` queue."""

    def __init__(
        self,
        client_getter: Callable[[], Client],
        load_settings: Callable[[Collection[str]], Mapping[str, Any]],
        task_queue: str,
    ) -> None:
        self.client_getter = client_getter
        self.load_settings = load_settings
        self.task_queue = task_queue
        self._running: set[asyncio.Task[None]] = set()

    async def nudge(self, *, full: bool = False) -> None:
        """Signal the configured Bambuddy's workflow ``changed``, and start it when none
        runs. Nothing without a ``bambuddy_url``, or when no data type pushes (R-T8-2). A
        failure is logged, never raised: the timer's full pass makes up for a lost nudge
        (Decision 7)."""
        try:
            async with asyncio.timeout(NUDGE_TIMEOUT):
                read = await asyncio.to_thread(self.load_settings, NUDGE_SETTINGS)
                url = read["bambuddy_url"]
                if skip_reason(url, read["filament_sync"]) is not None:
                    return
                instance = instance_for(url)
                await self.client_getter().start_workflow(
                    FILAMENT_BB_SYNC_WORKFLOW,
                    SyncStart(
                        instance=instance, interval_s=float(read["spool_overlay_refresh_seconds"])
                    ),
                    id=workflow_id(instance),
                    task_queue=self.task_queue,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    start_signal=CHANGED_SIGNAL,
                    start_signal_args=[full],
                )
        except Exception:
            logger.warning("could not nudge the filament sync", extra={"full": full}, exc_info=True)

    def nudge_soon(self, *, full: bool = False) -> None:
        """`nudge` in a task of its own, kept until it ends."""
        task = asyncio.create_task(self.nudge(full=full), name="filament sync nudge")
        self._running.add(task)
        task.add_done_callback(self._running.discard)
