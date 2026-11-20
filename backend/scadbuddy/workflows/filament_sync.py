"""``FilamentBbSync``: one per Bambuddy instance, pushing ScadBuddy's filament data to it
(#6265, plan `docs/superpowers/plans/2026-11-18-filament-bb-sync.md` Decisions 7, 11, 12
and 17).

The workflow and its pass activity (`filament/bb_sync_run.py`) are both on the
``bambuddy`` queue: the print worker serves them (`worker.build_print_deps`), or the API
in-process (`main._run_print_worker`). Its id is `bb_sync_nudge.workflow_id`, and
`FilamentSyncNudger` starts it with signal-with-start.

It waits for a ``changed`` signal or the timer. A signal is followed by `DEBOUNCE`, which
folds further signals into one pass; the pass is full when one of them asked for it
("Sync now"). The timer's pass is always full; ``interval_s`` 0 means no timer, so only
signals run passes. A pass that left work (``more``) is followed by the next at once, at
most `MAX_PASSES_IN_A_ROW` back to back (R-T4-21, R-T7-1); then the workflow waits as
usual. Each pass reports the interval the settings hold now, and the workflow waits that
long next. A pass for an instance that is no longer the configured one
(``instance_changed``), with no Bambuddy configured, or with no data type that pushes
(``nothing_pushes``, R-T8-2), ends the workflow: the next nudge (a settings save that
turns a direction on is one) starts the configured instance's. A skipped pass during
which a signal came does not end it: the next pass runs (R-T8-4). A pass whose every
attempt failed is logged, and the workflow waits for the next signal or the timer.

After `CONTINUE_AFTER_PASSES` passes, or when the server suggests it, the workflow
continues as new with the interval it holds and any signal it has not served yet.

On the versioned print worker the workflow is AUTO_UPGRADE (`client.print_deployment`),
so an open run moves to each new build: a change to ``FilamentBbSync.run``'s commands
goes behind ``workflow.patched``, and ``tests/test_filament_sync_replay.py`` replays the
committed histories.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from scadbuddy.filament.bb_sync_models import (
        SYNC_PASS_ACTIVITY,
        SyncPassInput,
        SyncPassResult,
    )
    from scadbuddy.filament.bb_sync_nudge import (
        CHANGED_SIGNAL,
        FILAMENT_BB_SYNC_WORKFLOW,
        SyncStart,
    )

#: How long after a signal the pass waits for more signals to fold in.
DEBOUNCE = timedelta(seconds=2)
#: Passes run back to back while each leaves work (decision 70).
MAX_PASSES_IN_A_ROW = 3
#: A run's passes before it continues as new.
CONTINUE_AFTER_PASSES = 200
#: One pass: at most 200 Bambuddy calls (Decision 11).
PASS_TIMEOUT = timedelta(minutes=15)
#: The pass heartbeats after every Bambuddy call.
PASS_HEARTBEAT = timedelta(seconds=60)
PASS_RETRY = RetryPolicy(
    maximum_attempts=3, initial_interval=timedelta(seconds=10), backoff_coefficient=2
)


@workflow.defn(name=FILAMENT_BB_SYNC_WORKFLOW)
class FilamentBbSync:
    def __init__(self) -> None:
        #: A signal came that no pass has served yet.
        self.pending = False
        #: One of those signals asked for a full pass.
        self.full = False

    @workflow.signal(name=CHANGED_SIGNAL)
    def changed(self, full: bool = False) -> None:
        self.pending = True
        self.full = self.full or full

    @workflow.run
    async def run(self, start: SyncStart) -> None:
        self.pending = self.pending or start.pending
        self.full = self.full or start.full
        interval_s = start.interval_s
        passes = 0
        while True:
            full = await self._wait(interval_s)
            for _ in range(MAX_PASSES_IN_A_ROW):
                result = await self._pass(start.instance, full, interval_s)
                passes += 1
                if result is None:
                    break
                interval_s = result.interval_s
                if result.skipped is not None and self.pending:
                    # R-T8-4: a signal came during the pass (a save that turned a
                    # direction on): the next pass reads the settings again.
                    break
                if result.instance_changed or result.skipped is not None:
                    workflow.logger.info(
                        "the filament sync has nothing to push to this Bambuddy; ending",
                        extra={"instance": start.instance, "skipped": result.skipped},
                    )
                    return
                if not result.more:
                    break
            else:
                workflow.logger.info(
                    "the filament sync ran its passes in a row and left work for the next",
                    extra={"passes": MAX_PASSES_IN_A_ROW},
                )
            if passes >= CONTINUE_AFTER_PASSES or workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(
                    SyncStart(
                        instance=start.instance,
                        interval_s=interval_s,
                        pending=self.pending,
                        full=self.full,
                    )
                )

    async def _wait(self, interval_s: float) -> bool:
        """Until a signal (then `DEBOUNCE`) or the timer; whether the pass is full."""
        if not self.pending:
            timeout = timedelta(seconds=interval_s) if interval_s > 0 else None
            try:
                await workflow.wait_condition(lambda: self.pending, timeout=timeout)
            except TimeoutError:
                return True
        await workflow.sleep(DEBOUNCE)
        full = self.full
        self.pending = False
        self.full = False
        return full

    async def _pass(self, instance: str, full: bool, interval_s: float) -> SyncPassResult | None:
        """One pass; None when its every attempt failed."""
        try:
            result: SyncPassResult = await workflow.execute_activity(
                SYNC_PASS_ACTIVITY,
                SyncPassInput(instance=instance, full=full, interval_s=interval_s),
                result_type=SyncPassResult,
                start_to_close_timeout=PASS_TIMEOUT,
                heartbeat_timeout=PASS_HEARTBEAT,
                retry_policy=PASS_RETRY,
            )
        except ActivityError:
            workflow.logger.warning(
                "a filament sync pass failed; the next signal or the timer runs another",
                exc_info=True,
            )
            return None
        return result
