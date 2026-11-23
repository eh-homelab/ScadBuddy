"""The spool refresh on a Temporal Schedule (#3818 tier A, plan 2026-10-18 D1, Task 6b).

``SpoolRefresh`` runs on the ``library`` queue, served in the API process, every
``spool_overlay_refresh_seconds`` (the Schedule ``scadbuddy-spool-providers-<queue>``,
overlap SKIP, and once at each boot; 0 deletes it; the names are older than what it
does now). A run keeps Bambuddy's local presets (``held_presets_refresh``, #4645,
`slicing/held_presets.py`), which a native run offers and slices from with no Bambuddy
call (hot-path plan D5), then pulls Bambuddy's spools into ScadBuddy's own
(``filament_bambuddy_pull``, #6261, `filament/bambuddy_pull.py`); with no Bambuddy, or
Bambuddy down, each step logs and returns. Since #6772 the pull's activity reads no
Bambuddy: it starts ``FilamentBbPull`` on the ``bambuddy`` queue, whose activity on the
print worker reads Bambuddy and sends the rows to the API's CRUD routes, and waits for it
(`filament/bb_pull_apply.py` `execute_pull`). The activity's name, timeout and result did
not change, so this workflow's commands did not. The kept presets are a read cache only:
Bambuddy's filament presets enter ScadBuddy's own (`filament/sb_presets.py`
`import_presets`) in the pull's activity, after the spools, and only while
``filament_sync.presets`` is ``bb_to_sb`` or ``two_way`` (#7244; before it, the kept
presets' activity imported them on every run, #7176). That moved no command either.
It is never started, signalled or awaited by
the print dialog: the hot path never waits on Bambuddy. A step that fails is not retried
within its run; the next tick is the retry, as it is for the sweeps.

Before ``spool-providers-gone`` (#6262 Task 12) a run first listed the spool providers
(``spool_providers_list``), refreshed each in an activity of its own
(``spool_provider_refresh``) and recorded the printers' trays (``spool_trays_seen``) for
the spool overlay, then the kept presets behind `HELD_PATCH` and the pull behind
`FILAMENT_PULL_PATCH`. The overlay is gone; a run opened before the patch replays that
branch, and its three activities answer as having nothing to do (`spools/activities.py`).

Behind ``filament-capture-backstop`` (#6263 Task 8), after the pull on every branch, a run
reconciles each printer's latest report with the filament ledger (`filament_capture_backstop`),
the backstop for what the printer watch missed.

Behind ``filament-rfid`` (#6264 Task 4), last, a run reconciles Bambuddy with ScadBuddy's
RFID state in one activity (`filament_rfid_reconcile`): the auto-add mode
(`filament/rfid_mode.py`), the tag bindings ScadBuddy changed (`filament/rfid_push.py`) and
the AMS slot of each override tag (`filament/rfid_slots.py`).

On the versioned ``library`` worker (#4090) it is AUTO_UPGRADE (`VersionedSpoolRefresh`),
so a run left open across a deploy replays on the new code: a change to the commands
``SpoolRefresh.run`` issues goes behind ``workflow.patched``
(``tests/test_spool_refresh_replay.py``).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client, ScheduleActionStartWorkflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ActivityError

from scadbuddy.workflows.housekeeping import ensure_workflow_schedule

SPOOL_REFRESH_WORKFLOW = "SpoolRefresh"
SPOOL_SCHEDULE = "scadbuddy-spool-providers"
LIST_ACTIVITY = "spool_providers_list"
PROVIDER_ACTIVITY = "spool_provider_refresh"
SEEN_ACTIVITY = "spool_trays_seen"
HELD_ACTIVITY = "held_presets_refresh"
#: The `workflow.patched` id that adds `HELD_ACTIVITY` to a run.
HELD_PATCH = "spool-refresh-held-presets"
FILAMENT_PULL_ACTIVITY = "filament_bambuddy_pull"
#: The `workflow.patched` id that adds `FILAMENT_PULL_ACTIVITY` to a run.
FILAMENT_PULL_PATCH = "filament-bb-pull"
#: The `workflow.patched` id after which a run lists no provider and records no tray: the
#: spool overlay is gone (#6262 Task 12). ``deprecate_patch`` and the removal of the old
#: branch come in a later PR, once no run opened before it is left (`RUN_TIMEOUT`).
PROVIDERS_GONE_PATCH = "spool-providers-gone"
CAPTURE_ACTIVITY = "filament_capture_backstop"
#: The `workflow.patched` id that adds `CAPTURE_ACTIVITY` to a run (#6263 Task 8).
CAPTURE_PATCH = "filament-capture-backstop"
FILAMENT_RFID_ACTIVITY = "filament_rfid_reconcile"
#: The `workflow.patched` id that adds `FILAMENT_RFID_ACTIVITY` to a run (#6264 Task 4).
FILAMENT_RFID_PATCH = "filament-rfid"
#: Listing the providers read nothing (before `PROVIDERS_GONE_PATCH`, as the next three).
LIST_TIMEOUT = timedelta(seconds=30)
#: One provider's refresh, which was bounded at a minute inside its activity.
PROVIDER_TIMEOUT = timedelta(seconds=90)
#: Every printer's latest report, then a write per tagged tray (now none).
SEEN_TIMEOUT = timedelta(minutes=2)
#: One list of Bambuddy's local presets, then a read of each one edited since.
HELD_TIMEOUT = timedelta(minutes=2)
#: The wait for ``FilamentBbPull`` (#6772): the print worker's list of Bambuddy's
#: spools, then the API's transaction per spool; the run itself is bounded under this
#: (`bb_pull_apply.PULL_RUN_TIMEOUT`).
FILAMENT_PULL_TIMEOUT = timedelta(minutes=5)
#: Every printer's latest report, reconciled with the ledger in a transaction each.
CAPTURE_TIMEOUT = timedelta(minutes=2)
#: The activity's three steps share this one budget (a timeout is per activity, and a change
#: to its commands needs a patch): the override mode (up to 5 s of lock wait, then a few
#: calls), the tag push (up to 50 tags, one to four calls each) and the slot step (a
#: configure call per override tray). Held, pull, capture and this one add up to
#: `RUN_TIMEOUT`.
FILAMENT_RFID_TIMEOUT = timedelta(minutes=6)
#: A run opened before `PROVIDERS_GONE_PATCH` grows with the providers; a run that can
#: never finish still ends, and the Schedule's overlap SKIP stops holding later ticks back.
RUN_TIMEOUT = timedelta(minutes=15)
_ONCE = RetryPolicy(maximum_attempts=1)


def spool_schedule_id_for(task_queue: str) -> str:
    """One Schedule per queue: a test's app (a queue of its own) never moves another's."""
    return f"{SPOOL_SCHEDULE}-{task_queue}"


@workflow.defn(name=SPOOL_REFRESH_WORKFLOW)
class SpoolRefresh:
    @workflow.run
    async def run(self) -> dict[str, str]:
        """Empty since `PROVIDERS_GONE_PATCH`. Before it, what came of each provider, in
        priority order: ``refreshed``, ``failed`` or ``skipped``. One step's failure
        never stops the next."""
        gone = workflow.patched(PROVIDERS_GONE_PATCH)
        outcome = await self._run_steps(gone)
        if workflow.patched(CAPTURE_PATCH):
            await self._capture()
        if workflow.patched(FILAMENT_RFID_PATCH):
            await self._rfid()
        return outcome

    async def _run_steps(self, gone: bool) -> dict[str, str]:
        if gone:
            await self._held()
            await self._pull()
            return {}
        names: list[str] = await workflow.execute_activity(
            LIST_ACTIVITY,
            result_type=list[str],
            start_to_close_timeout=LIST_TIMEOUT,
            retry_policy=_ONCE,
        )
        outcome: dict[str, str] = {}
        for name in names:
            try:
                outcome[name] = await workflow.execute_activity(
                    PROVIDER_ACTIVITY,
                    name,
                    result_type=str,
                    start_to_close_timeout=PROVIDER_TIMEOUT,
                    retry_policy=_ONCE,
                )
            except ActivityError:
                workflow.logger.warning("could not refresh the spool provider %s", name)
                outcome[name] = "failed"
        try:
            await workflow.execute_activity(
                SEEN_ACTIVITY, start_to_close_timeout=SEEN_TIMEOUT, retry_policy=_ONCE
            )
        except ActivityError:
            workflow.logger.warning("could not record what the printers' trays show")
        if workflow.patched(HELD_PATCH):
            await self._held()
        if workflow.patched(FILAMENT_PULL_PATCH):
            await self._pull()
        return outcome

    async def _held(self) -> None:
        try:
            await workflow.execute_activity(
                HELD_ACTIVITY,
                result_type=str,
                start_to_close_timeout=HELD_TIMEOUT,
                retry_policy=_ONCE,
            )
        except ActivityError:
            workflow.logger.warning("could not keep Bambuddy's local presets")

    async def _capture(self) -> None:
        try:
            await workflow.execute_activity(
                CAPTURE_ACTIVITY,
                result_type=str,
                start_to_close_timeout=CAPTURE_TIMEOUT,
                retry_policy=_ONCE,
            )
        except ActivityError:
            workflow.logger.warning("could not reconcile the printers' trays with the ledger")

    async def _rfid(self) -> None:
        try:
            await workflow.execute_activity(
                FILAMENT_RFID_ACTIVITY,
                result_type=str,
                start_to_close_timeout=FILAMENT_RFID_TIMEOUT,
                retry_policy=_ONCE,
            )
        except ActivityError:
            workflow.logger.warning("could not tell Bambuddy of the tag bindings")

    async def _pull(self) -> None:
        try:
            await workflow.execute_activity(
                FILAMENT_PULL_ACTIVITY,
                result_type=str,
                start_to_close_timeout=FILAMENT_PULL_TIMEOUT,
                retry_policy=_ONCE,
            )
        except ActivityError:
            workflow.logger.warning("could not pull Bambuddy's spools")


@workflow.defn(name=SPOOL_REFRESH_WORKFLOW, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedSpoolRefresh(SpoolRefresh):
    """``SpoolRefresh`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE: a run
    is minutes long, but one a rollout's old replica held must finish on the new one.
    Its own class because an unversioned worker cannot complete a workflow that
    declares a behavior."""

    @workflow.run
    async def run(self) -> dict[str, str]:
        return await super().run()


async def ensure_spool_schedule(client: Client, task_queue: str, interval: float) -> None:
    """The refresh's Schedule every ``interval`` seconds, then one run now; none at 0.
    A Schedule an operator paused stays paused, and is not run."""
    schedule_id = spool_schedule_id_for(task_queue)
    action = ScheduleActionStartWorkflow(
        SPOOL_REFRESH_WORKFLOW,
        id=schedule_id,
        task_queue=task_queue,
        execution_timeout=RUN_TIMEOUT,
    )
    await ensure_workflow_schedule(client, schedule_id, interval, action)
