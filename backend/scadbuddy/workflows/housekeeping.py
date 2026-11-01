"""Housekeeping on a Temporal Schedule (#1054, spec 2026-10-01 §4.4).

The API process's periodic loops are two Schedules that start ``Housekeeping`` on the
``library`` queue: ``scadbuddy-housekeeping-<queue>`` runs every sweep every
``SCADBUDDY_ASSET_SWEEP_INTERVAL`` seconds (0: no Schedule), and
``scadbuddy-prune-<queue>`` prunes settled render jobs (as the render service's loop
did), sweeps old request claims and the files of lost slice starts, and repairs and
prunes the activity record every `PRUNE_INTERVAL`, whatever that interval. The
queue is served in the API process, which holds the data volume the sweeps read. Each
sweep is best effort, as the loop's were: one that fails is logged, fails its activity
(the run returns it, and Temporal's UI shows it), the rest still run, and the next tick
tries again.

The ``library`` worker is versioned (#4090): the ``scadbuddy-library`` Worker
Deployment, each API build one version of it, made current at start
(`main._run_library_worker`). ``Housekeeping`` is AUTO_UPGRADE
(`VersionedHousekeeping`): a run outlives a rollout (up to its `housekeeping_timeout`),
so it moves to the current build on its next workflow task and replays there.
``Housekeeping.run`` must stay replay-compatible: a change to its command sequence (a
step added, the loop reordered) goes behind ``workflow.patched``. A run that fails on
replay anyway ends at its `housekeeping_timeout`. The activity tasks of an
AUTO_UPGRADE run start on the run's target version, the current build, so a sweep a
new build adds to `SWEEPS` is never handed to an old replica that would fail it as
unregistered; an old replica only finishes the attempts it had already started.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleHandle,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ActivityError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

with workflow.unsafe.imports_passed_through():
    from scadbuddy.render.submit import DESCRIBE_BOUND, SETTLE_DESCRIBES
    from scadbuddy.workflows.operation_models import HEARTBEAT_THROTTLE
    from scadbuddy.workflows.registered import RegisteredActivities

logger = logging.getLogger(__name__)

HOUSEKEEPING_WORKFLOW = "Housekeeping"
HOUSEKEEPING_SCHEDULE = "scadbuddy-housekeeping"
PRUNE_SCHEDULE = "scadbuddy-prune"
#: The render service's prune loop's interval: a settled job outlives `job_ttl` by this.
PRUNE_INTERVAL = 300.0
#: A boot does not trigger a Schedule that already started a run this recently (or
#: within its interval, if shorter): each replica of each rollout booting used to run
#: the daily sweeps again, ~37 times a day (#2352). One run per window converges them.
BOOT_TRIGGER_WINDOW = 30 * 60.0


def schedule_id_for(task_queue: str) -> str:
    """One Schedule per queue: a test's app (a queue of its own) never moves another's."""
    return f"{HOUSEKEEPING_SCHEDULE}-{task_queue}"


def prune_schedule_id_for(task_queue: str) -> str:
    return f"{PRUNE_SCHEDULE}-{task_queue}"


#: The shortest interval a Temporal Schedule takes ("interval is too small" below it).
MIN_INTERVAL = 1.0

#: #902's backstop: a finished output re-render whose settling event no API heard.
BACKFILL_SWEEP = "housekeeping_attach_backfills"
#: #1007: release the Parts holds of outputs whose record is gone (library/outputs.py
#: `reap_orphan_holds`). Last, so what it frees waits for the next tick's blob sweep.
REAP_SWEEP = "housekeeping_reap_output_holds"
#: The `workflow.patched` id that adds `REAP_SWEEP` to a run (see the module docstring).
REAP_PATCH = "housekeeping-reap-output-holds"
#: #1057: end the flow runs whose execution closed without finishing them
#: (`flows/sweep.py`). Behind its own patch, like `REAP_SWEEP`.
FLOWS_SWEEP = "housekeeping_sweep_flow_runs"
FLOWS_PATCH = "housekeeping-sweep-flow-runs"
#: #2169: remove the print dialog's background slices no run queued
#: (`bambuddy.preview.sweep_preview_slices`). Behind its own patch, like `REAP_SWEEP`.
PREVIEWS_SWEEP = "housekeeping_sweep_preview_slices"
PREVIEWS_PATCH = "housekeeping-sweep-preview-slices"
#: #2715: remove what a print run's lost slice start sliced (`bambuddy.lost_slices`).
#: Its own sweep, out of `PREVIEWS_SWEEP`, so it stays on the prune Schedule: Bambuddy
#: keeps a finished job only 30 minutes. Behind its own patch. With the sweeps on it
#: runs from both Schedules, whose overlap SKIP is each its own, so two may overlap:
#: harmless, since a file is deleted only while it still has its job's name (a second
#: delete finds it gone, warns and settles on the next tick) and finishing a lost start
#: is an idempotent update.
LOST_SLICES_SWEEP = "housekeeping_sweep_lost_slices"
LOST_SLICES_PATCH = "housekeeping-sweep-lost-slices"
#: #2289: pack the models repository (`ModelHistory.maintain`) once loose objects pile
#: up, which git's own `gc --auto` lets reach thousands. Behind its own patch.
GIT_SWEEP = "housekeeping_maintain_models_git"
GIT_PATCH = "housekeeping-maintain-models-git"
#: Activity retention and repair (spec 2026-10-09 §6). Last: it reads what the sweeps
#: before it settled.
ACTIVITY_SWEEP = "housekeeping_activity"
#: The `workflow.patched` id that adds `ACTIVITY_SWEEP` to a run.
ACTIVITY_PATCH = "housekeeping-activity"
#: #3047: forget the attachments of archives deleted in Bambuddy. Behind its own patch,
#: and after `ACTIVITY_SWEEP`, so a run without the patch is today's run exactly.
ATTACHMENTS_SWEEP = "housekeeping_sweep_print_attachments"
ATTACHMENTS_PATCH = "housekeeping-sweep-print-attachments"
#: The native print library's trash purge and orphan blobs (#3827); after the attachments.
LIBRARY_SWEEP = "housekeeping_sweep_library"
LIBRARY_PATCH = "housekeeping-sweep-library"
#: The native slicer's settled slices (plan 2026-10-16 Task 12): rows past
#: `PREVIEW_GRACE` deleted with their holds (`sweep_content` then collects the blobs). It
#: obeys the sweeps' switch, as
#: `PREVIEWS_SWEEP` does: with the sweeps off, `sweep_content` does not run either.
NATIVE_SLICES_SWEEP = "housekeeping_sweep_native_slices"
NATIVE_SLICES_PATCH = "housekeeping-sweep-native-slices"
#: What the slicer worker PUT through the internal blob route (`transfer_puts`), a day
#: on. On the prune Schedule too, like the claims, so it runs with the sweeps off.
TRANSFER_PUTS_SWEEP = "housekeeping_prune_transfer_puts"
TRANSFER_PUTS_PATCH = "housekeeping-prune-transfer-puts"
#: The native slices whose execution is gone, failed (Task 12 review M1): a correctness
#: repair, not storage, so on the prune Schedule, with the sweeps off too; the TTL
#: delete (`NATIVE_SLICES_SWEEP`) stays under the switch.
LOST_NATIVE_SLICES_SWEEP = "housekeeping_stop_lost_native_slices"
LOST_NATIVE_SLICES_PATCH = "housekeeping-stop-lost-native-slices"
#: #3823: a `PrinterWatch` for every LAN-linked printer, so one that stopped (or never
#: started: its link's start failed) comes back. Last, behind its own patch; on the
#: prune Schedule too, so it comes back within `PRUNE_INTERVAL` (#4186 review I4).
PRINTER_WATCHES_SWEEP = "housekeeping_printer_watches"
PRINTER_WATCHES_PATCH = "housekeeping-printer-watches"
#: #4250 Ruling 3: drop the slice quotes' sliced-file holders §3.4 says should be gone
#: (`orders.lifecycle.reap_quote_holds`): a ref whose run never wrote its row, and one a
#: release lost between its commit and its drop. Last, behind its own patch; on the
#: prune Schedule too, so a lost drop leaks a blob for at most `PRUNE_INTERVAL`.
QUOTE_HOLDS_SWEEP = "housekeeping_reap_quote_holds"
QUOTE_HOLDS_PATCH = "housekeeping-reap-quote-holds"
#: #4439, #4747: the Bambuddy printer sync (`printers/bambuddy_sync.py`): import and link
#: Bambuddy's printers and virtual printers, and fill an empty LAN link (a failed probe is
#: tried again on the next pass). Last, behind its own patch; on the sweeps' Schedule
#: only, since it calls Bambuddy (not in `PRUNE_SWEEPS`). Runs in the API's process (the
#: ``library`` worker), so the access code it reads from Bambuddy never crosses a process
#: or a payload.
PRINTER_SYNC_SWEEP = "housekeeping_sync_bambuddy_printers"
PRINTER_SYNC_PATCH = "housekeeping-sync-bambuddy-printers"
#: #4337: the lifetime of a native run's per-print copies and slices
#: (`print_library.sweep.sweep_print_copies`): a folder's superseded copies and what
#: nothing records go to the native trash. Last, behind its own patch; on the sweeps'
#: Schedule only, so it obeys their switch as the asset sweep does: it trashes files.
NATIVE_COPIES_SWEEP = "housekeeping_sweep_native_copies"
NATIVE_COPIES_PATCH = "housekeeping-sweep-native-copies"
#: Today's order: settled jobs first (they hold blob refs), then what they freed.
SWEEPS = (
    "housekeeping_prune_jobs",
    "housekeeping_sweep_assets",
    "housekeeping_sweep_blobs",
    "housekeeping_sweep_staging",
    "housekeeping_sweep_claims",
    BACKFILL_SWEEP,
    REAP_SWEEP,
    FLOWS_SWEEP,
    PREVIEWS_SWEEP,
    LOST_SLICES_SWEEP,
    GIT_SWEEP,
    ACTIVITY_SWEEP,
    ATTACHMENTS_SWEEP,
    LIBRARY_SWEEP,
    NATIVE_SLICES_SWEEP,
    TRANSFER_PUTS_SWEEP,
    LOST_NATIVE_SLICES_SWEEP,
    PRINTER_WATCHES_SWEEP,
    QUOTE_HOLDS_SWEEP,
    PRINTER_SYNC_SWEEP,
    NATIVE_COPIES_SWEEP,
)
#: Every `PRUNE_INTERVAL`, whatever the sweep interval: settled jobs and request claims,
#: which would otherwise pile up for good with the sweeps off (review 3c M1); the files
#: of lost slice starts, findable only while Bambuddy keeps the job (#2715); and the
#: activity rows, which every start adds to (retention and repair, spec 2026-10-09 §6);
#: and the native print library's trash purge and orphan blobs (#3827), which the generic
#: blob sweep skips, so with the sweeps off nothing else would collect them. Behind
#: `LIBRARY_PATCH` as in `SWEEPS`, so a run recorded before it replays unchanged. With the
#: sweeps on it runs from both Schedules, whose overlap SKIP is each its own, so two may
#: overlap: harmless, since a purge deletes a row only while it is still trashed past the
#: cutoff and an orphan blob only while it is still stale (`purge_trashed`,
#: `delete_if_stale`), so the second run's attempt finds nothing to do.
#: Not the dialog's background slices (`PREVIEWS_SWEEP`, `NATIVE_SLICES_SWEEP`): they
#: obey the sweeps' switch, so with the sweeps off they stay. The transfer PUTs'
#: records (`TRANSFER_PUTS_SWEEP`) are pruned here too, behind their patch. And the
#: printer watches (#3823), behind `PRINTER_WATCHES_PATCH`: a lost one is started again
#: within the interval, not the sweeps' day. And the quote holds' reaper (#4250), behind
#: `QUOTE_HOLDS_PATCH`, so a release lost after its commit leaks for one interval. Not the
#: Bambuddy printer sync (#4439, #4747): it calls Bambuddy, so it obeys the sweeps'
#: switch, and the import button, a passing connection test and each virtual printer's
#: write run it besides.
PRUNE_SWEEPS = (
    SWEEPS[0],
    SWEEPS[4],
    LOST_SLICES_SWEEP,
    ACTIVITY_SWEEP,
    LIBRARY_SWEEP,
    TRANSFER_PUTS_SWEEP,
    LOST_NATIVE_SLICES_SWEEP,
    PRINTER_WATCHES_SWEEP,
    QUOTE_HOLDS_SWEEP,
)
#: An asset sweep converges with the store over Bambuddy, at length.
SWEEP_TIMEOUT = timedelta(minutes=30)
#: Every sweep heartbeats: a worker lost mid-sweep is noticed within this, not after
#: its start-to-close timeout, so the Schedules' overlap SKIP does not hold the next
#: ticks back.
HEARTBEAT_TIMEOUT = timedelta(minutes=1)
#: The prune is two deletes, then the render service's settle (#1707): two passes, each
#: a listing of the open runs and up to `SETTLE_DESCRIBES` describes, each bounded by
#: `DESCRIBE_BOUND`. All of that on a slow Temporal, plus the deletes.
PRUNE_TIMEOUT = timedelta(seconds=2 * (1 + SETTLE_DESCRIBES) * DESCRIBE_BOUND) + timedelta(
    minutes=2
)
#: The claims sweep is one delete.
CLAIMS_TIMEOUT = timedelta(minutes=2)
#: A few Bambuddy reads and deletes per background slice past its grace, and per lost
#: slice start.
PREVIEWS_TIMEOUT = timedelta(minutes=5)
#: `ModelHistory.maintain`'s `git gc` is bounded by `history.MAINTAIN_TIMEOUT` (10 min);
#: this adds a margin for the count and the kill (#2342). Not imported: the sandbox runs
#: this module, and `library.history` is not passed through.
GIT_TIMEOUT = timedelta(minutes=12)
#: The activity sweep is a repair pass over four tables and one delete.
ACTIVITY_TIMEOUT = timedelta(minutes=5)
#: One Bambuddy archive read per print with attachments, and a delete per gone one.
ATTACHMENTS_TIMEOUT = timedelta(minutes=10)
#: The library sweep purges trash past 30 days and deletes the blobs no row names.
LIBRARY_TIMEOUT = timedelta(minutes=10)
#: A delete, and a describe per running native slice past `LOST_AFTER`.
NATIVE_SLICES_TIMEOUT = timedelta(minutes=5)
#: A describe per running native slice past `LOST_AFTER`, each stopped in one update.
LOST_NATIVE_SLICES_TIMEOUT = timedelta(minutes=2)
#: One delete.
TRANSFER_PUTS_TIMEOUT = timedelta(minutes=2)
#: One start (or none) per LAN printer, each bounded by `follow.RPC_TIMEOUT`.
PRINTER_WATCHES_TIMEOUT = timedelta(minutes=2)
#: One read of the quotes that stand and one delete.
QUOTE_HOLDS_TIMEOUT = timedelta(minutes=2)
#: Two Bambuddy lists, then per printer with an empty LAN link a read and one probe
#: (`printers.component.PROBE_TIMEOUT`, 10 s), each in turn.
PRINTER_SYNC_TIMEOUT = timedelta(minutes=5)
#: Two describes per LAN run with prints still to start (`end_gone_sequences`; few run at
#: once); a few reads, then a trash, and per folder with superseded copies a lock, a read,
#: a trash and a forget per copy.
NATIVE_COPIES_TIMEOUT = timedelta(minutes=5)
#: What a run spends between its activities: workflow tasks, and a start on a busy worker.
RUN_MARGIN = timedelta(minutes=3)


def sweep_timeout(sweep: str) -> timedelta:
    """``sweep``'s start-to-close timeout."""
    if sweep == PRUNE_SWEEPS[0]:
        return PRUNE_TIMEOUT
    if sweep == PRUNE_SWEEPS[1]:
        return CLAIMS_TIMEOUT
    if sweep in (PREVIEWS_SWEEP, LOST_SLICES_SWEEP):
        return PREVIEWS_TIMEOUT
    if sweep == GIT_SWEEP:
        return GIT_TIMEOUT
    if sweep == ACTIVITY_SWEEP:
        return ACTIVITY_TIMEOUT
    if sweep == ATTACHMENTS_SWEEP:
        return ATTACHMENTS_TIMEOUT
    if sweep == LIBRARY_SWEEP:
        return LIBRARY_TIMEOUT
    if sweep == NATIVE_SLICES_SWEEP:
        return NATIVE_SLICES_TIMEOUT
    if sweep == LOST_NATIVE_SLICES_SWEEP:
        return LOST_NATIVE_SLICES_TIMEOUT
    if sweep == TRANSFER_PUTS_SWEEP:
        return TRANSFER_PUTS_TIMEOUT
    if sweep == PRINTER_WATCHES_SWEEP:
        return PRINTER_WATCHES_TIMEOUT
    if sweep == QUOTE_HOLDS_SWEEP:
        return QUOTE_HOLDS_TIMEOUT
    if sweep == PRINTER_SYNC_SWEEP:
        return PRINTER_SYNC_TIMEOUT
    if sweep == NATIVE_COPIES_SWEEP:
        return NATIVE_COPIES_TIMEOUT
    return SWEEP_TIMEOUT


def housekeeping_timeout(sweeps: tuple[str, ...]) -> timedelta:
    """A run's bound: each sweep's activity timeout, plus a margin. A run that can
    never finish (a workflow task that fails on replay retries forever) then ends,
    and the Schedule's overlap SKIP does not hold back every later tick."""
    return sum((sweep_timeout(sweep) for sweep in sweeps), RUN_MARGIN)


@workflow.defn(name=HOUSEKEEPING_WORKFLOW)
class Housekeeping:
    @workflow.run
    async def run(self, sweeps: list[str] | None = None) -> list[str]:
        failed: list[str] = []
        for sweep in SWEEPS if sweeps is None else sweeps:
            # Added after runs that may still replay; one recorded before it skips it.
            if sweep == REAP_SWEEP and not workflow.patched(REAP_PATCH):
                continue
            if sweep == FLOWS_SWEEP and not workflow.patched(FLOWS_PATCH):
                continue
            if sweep == PREVIEWS_SWEEP and not workflow.patched(PREVIEWS_PATCH):
                continue
            if sweep == LOST_SLICES_SWEEP and not workflow.patched(LOST_SLICES_PATCH):
                continue
            if sweep == GIT_SWEEP and not workflow.patched(GIT_PATCH):
                continue
            if sweep == ACTIVITY_SWEEP and not workflow.patched(ACTIVITY_PATCH):
                continue
            if sweep == ATTACHMENTS_SWEEP and not workflow.patched(ATTACHMENTS_PATCH):
                continue
            if sweep == LIBRARY_SWEEP and not workflow.patched(LIBRARY_PATCH):
                continue
            if sweep == NATIVE_SLICES_SWEEP and not workflow.patched(NATIVE_SLICES_PATCH):
                continue
            if sweep == TRANSFER_PUTS_SWEEP and not workflow.patched(TRANSFER_PUTS_PATCH):
                continue
            if sweep == LOST_NATIVE_SLICES_SWEEP and not workflow.patched(LOST_NATIVE_SLICES_PATCH):
                continue
            if sweep == PRINTER_WATCHES_SWEEP and not workflow.patched(PRINTER_WATCHES_PATCH):
                continue
            if sweep == QUOTE_HOLDS_SWEEP and not workflow.patched(QUOTE_HOLDS_PATCH):
                continue
            if sweep == PRINTER_SYNC_SWEEP and not workflow.patched(PRINTER_SYNC_PATCH):
                continue
            if sweep == NATIVE_COPIES_SWEEP and not workflow.patched(NATIVE_COPIES_PATCH):
                continue
            try:
                await workflow.execute_activity(
                    sweep,
                    start_to_close_timeout=sweep_timeout(sweep),
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    # The next tick is the retry, as it was for the loop.
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                workflow.logger.warning("housekeeping sweep %s failed", sweep)
                failed.append(sweep)
        return failed


@workflow.defn(name=HOUSEKEEPING_WORKFLOW, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedHousekeeping(Housekeeping):
    """``Housekeeping`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE: a
    Schedule's run lasts up to its `housekeeping_timeout`, longer than a rollout, and
    Pinned it would hold the old build's last replica until it ended or be stranded
    when that replica stopped. Its own class because an unversioned worker cannot
    complete a workflow that declares a behavior (the tests' workers). Changes still
    go behind ``workflow.patched``, in ``Housekeeping``."""

    @workflow.run
    async def run(self, sweeps: list[str] | None = None) -> list[str]:
        return await super().run(sweeps)


def library_worker(
    client: Client,
    task_queue: str,
    activities: Sequence[Callable[..., Any]],
    *,
    workflows: Sequence[type] = (),
    workflow_runner: WorkflowRunner | None = None,
    graceful_shutdown_timeout: timedelta = timedelta(seconds=30),
    deployment_config: WorkerDeploymentConfig | None = None,
) -> Worker:
    """The ``library`` worker: ``Housekeeping`` and its sweeps, plus ``workflows`` (the
    library commands' ``Operation``, the preview backfill). A stop gives a running
    sweep ``graceful_shutdown_timeout`` to finish (review #1095b 5). A sweep longer
    than that (an asset sweep's converge) is then cancelled, and gets
    `main.SWEEP_SHUTDOWN_JOIN` more for its thread to return (#1708), so the lifespan
    does not close the stores under it. ``workflow_runner`` is the app's
    `client.sandboxed_runner` (#2014), passed in: this module runs in the sandbox, so
    it does not import the client's. With ``deployment_config`` (the API's,
    `client.library_deployment`, #4090) it is versioned and serves
    `VersionedHousekeeping`, and every one of ``workflows`` must name its own
    versioning behavior: the deployment has no default, so the SDK refuses one that
    does not."""
    housekeeping = Housekeeping if deployment_config is None else VersionedHousekeeping
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[housekeeping, *workflows],
        activities=activities,
        workflow_runner=workflow_runner or SandboxedWorkflowRunner(),
        graceful_shutdown_timeout=graceful_shutdown_timeout,
        deployment_config=deployment_config,
        # A library command of a kind this build lacks waits for the build that has it
        # (#4139): a new API replica's request in the second before its build is current.
        interceptors=[RegisteredActivities(activities)],
        # The library commands' ``Operation`` runs heartbeat against a 30 s timeout (#4635).
        max_heartbeat_throttle_interval=HEARTBEAT_THROTTLE,
    )


def _schedule(interval: float, action: ScheduleActionStartWorkflow) -> Schedule:
    return Schedule(
        action=action,
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=max(interval, MIN_INTERVAL)))]
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def ensure_workflow_schedule(
    client: Client,
    schedule_id: str,
    interval: float,
    action: ScheduleActionStartWorkflow,
    *,
    boot_window: float | None = None,
) -> bool:
    """The Schedule starting ``action`` every ``interval`` seconds (0: none), then one
    run now: the boot's, unless a run is open or one started within ``boot_window``
    seconds (default `BOOT_TRIGGER_WINDOW`; or the interval, if shorter), which
    converges this boot too (#2352). A boot that changes the action (a rollout adding
    or changing a sweep) runs it anyway, queued behind an open run (#3057). A
    Schedule an operator paused stays paused, and is not run: True says so (review
    #1095 2), since then nothing converges until it resumes. Every Schedule the API
    keeps (housekeeping, the preview backfill) is made here."""
    handle = client.get_schedule_handle(schedule_id)
    if interval <= 0:
        try:
            await handle.delete()
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise
        return False
    schedule = _schedule(interval, action)
    #: Whether this boot changed what a run does (a sweep added or changed, #3057): the
    #: open or recent run did the old action's work, so it converges nothing new.
    changed = False
    try:
        await client.create_schedule(schedule_id, schedule)
    except ScheduleAlreadyRunningError:
        previous: list[Any] = []

        def replace(input: ScheduleUpdateInput) -> ScheduleUpdate:
            # The state (paused, its note) is the operator's, not the deploy's.
            schedule.state = input.description.schedule.state
            previous.append(input.description.schedule.action)
            return ScheduleUpdate(schedule=schedule)

        await handle.update(replace)
        changed = bool(previous) and await _action_changed(client, previous[-1], action)
    window = BOOT_TRIGGER_WINDOW if boot_window is None else boot_window
    if not schedule.state.paused and (
        changed or not await _ran_lately(handle, min(window, interval))
    ):
        # A run that opens between the look and this would SKIP this one; it queues
        # behind that run instead. In a rolling deploy the old build's replicas and the
        # new one's boot in turn, and each sees the other's action as changed and
        # triggers; BUFFER_ONE keeps at most one run queued behind the open one, so
        # that costs one extra run of the sweeps, which converge whichever action won.
        await handle.trigger(overlap=ScheduleOverlapPolicy.BUFFER_ONE)
    return schedule.state.paused


async def _action_changed(
    client: Client, previous: Any, action: ScheduleActionStartWorkflow
) -> bool:
    """Whether ``action`` starts another workflow, or passes it other arguments, than
    the ``previous`` action the Schedule held (described, so its arguments encoded).
    Deliberately ignores ``execution_timeout`` and ``task_queue``: every caller's
    timeout is either fixed or a deterministic function of the ``args`` already
    compared, so it never changes without ``args`` changing too, and a queue rename
    is not new work for the sweeps to converge."""
    if not isinstance(previous, ScheduleActionStartWorkflow):
        return True
    if previous.workflow != action.workflow:
        return True
    try:
        before = await client.data_converter.decode(previous.args)
    except Exception:
        # Arguments this converter cannot read (an older build's) count as changed: a
        # boot that cannot tell runs the sweeps rather than fail.
        return True
    return list(before) != list(action.args)


async def _ran_lately(handle: ScheduleHandle, window: float) -> bool:
    """Whether the Schedule has a run open, or started one within ``window`` seconds.
    An open run's activities move to a live worker when a rollout stops theirs, so it
    still finishes; another boot's trigger would only run the sweeps twice."""
    info = (await handle.describe()).info
    if info.running_actions:
        return True
    since = datetime.now(UTC) - timedelta(seconds=window)
    return any(action.started_at >= since for action in info.recent_actions)


async def ensure_schedule(
    client: Client,
    task_queue: str,
    interval: float,
    *,
    schedule_id: str | None = None,
    sweeps: tuple[str, ...] = SWEEPS,
    boot_window: float | None = None,
) -> bool:
    """The sweeps' Schedule at ``interval`` seconds (0: none), then one run now: the
    boot's converging sweep (`ensure_workflow_schedule`). True when it is paused."""
    schedule_id = schedule_id or schedule_id_for(task_queue)
    action = ScheduleActionStartWorkflow(
        HOUSEKEEPING_WORKFLOW,
        list(sweeps),
        id=schedule_id,
        task_queue=task_queue,
        execution_timeout=housekeeping_timeout(sweeps),
    )
    return await ensure_workflow_schedule(
        client, schedule_id, interval, action, boot_window=boot_window
    )


async def ensure_schedules(client: Client, task_queue: str, interval: float) -> bool:
    """Both Schedules: the prune's fixed one, and every sweep at ``interval``. True
    when an operator has paused the sweeps' one."""
    await ensure_schedule(
        client,
        task_queue,
        PRUNE_INTERVAL,
        schedule_id=prune_schedule_id_for(task_queue),
        sweeps=PRUNE_SWEEPS,
    )
    return await ensure_schedule(client, task_queue, interval)
