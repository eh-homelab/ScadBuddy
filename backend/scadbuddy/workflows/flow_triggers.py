"""A flow trigger's fire, and its Temporal Schedule (#2484, spec 2026-10-11 §12).

A schedule trigger is the Schedule ``flow-trigger-<trigger id>`` on the ``library`` queue,
whose action starts ``FlowTriggerFire`` with the trigger id only; an event trigger starts
the same workflow from the event bus (``flow_triggers/events.py``). The fire's one
activity (``flow_triggers/fire.py``) reads everything it acts on from the trigger's row,
so nothing a Schedule or an event carries can say who a run is or what it may do.

On the versioned ``library`` worker (#4090) it is AUTO_UPGRADE
(`VersionedFlowTriggerFire`): ``FlowTriggerFire.run`` must stay replay-compatible, and
a change to its command sequence goes behind ``workflow.patched``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.service import RPCError, RPCStatusCode

with workflow.unsafe.imports_passed_through():
    from scadbuddy.flow_triggers.models import ScheduleTiming, Trigger

FIRE_WORKFLOW = "FlowTriggerFire"
FIRE_ACTIVITY = "flow_trigger_fire"
SCHEDULE_PREFIX = "flow-trigger-"
#: A fire reads a few rows, type-checks a script and starts a run.
FIRE_ACTIVITY_TIMEOUT = timedelta(minutes=2)
#: Its attempts, then its whole run: a fire that cannot finish ends, and the Schedule's
#: overlap SKIP stops holding later fires back.
FIRE_ATTEMPTS = 5
FIRE_TIMEOUT = timedelta(minutes=15)


def schedule_id(trigger_id: str) -> str:
    """The trigger's Schedule; also the prefix of its fires' workflow ids."""
    return f"{SCHEDULE_PREFIX}{trigger_id}"


@workflow.defn(name=FIRE_WORKFLOW)
class FlowTriggerFire:
    @workflow.run
    async def run(self, trigger_id: str, event: dict[str, Any] | None = None) -> str:
        """Fire the trigger once (§12.2), under a fire key a retry keeps, so it starts one
        run. The fire's outcome. An event's key is this workflow's id, one per trigger and
        event; a Schedule's adds the run id, since two fires it is asked for in one second
        share a workflow id."""
        info = workflow.info()
        key = info.workflow_id if event is not None else f"{info.workflow_id}/{info.run_id}"
        outcome: str = await workflow.execute_activity(
            FIRE_ACTIVITY,
            args=[trigger_id, key, event],
            result_type=str,
            start_to_close_timeout=FIRE_ACTIVITY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=FIRE_ATTEMPTS),
        )
        return outcome


@workflow.defn(name=FIRE_WORKFLOW, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedFlowTriggerFire(FlowTriggerFire):
    """``FlowTriggerFire`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE,
    though a fire is short: its activity retries for up to `FIRE_TIMEOUT` (15 min),
    longer than an API pod's termination grace, and Pinned, a fire whose attempt ran on
    the old build's last replica would wait, unserved, until that timeout ended it and
    the event went unfired. Its one activity is the same in every build, so moving
    builds costs nothing. Its own class because an unversioned worker cannot complete a
    workflow that declares a behavior."""

    @workflow.run
    async def run(self, trigger_id: str, event: dict[str, Any] | None = None) -> str:
        return await super().run(trigger_id, event)


def _overlap(trigger: Trigger) -> ScheduleOverlapPolicy:
    # The fire's own policy; the fire checks the trigger's previous run as well (Ruling 21).
    if trigger.overlap == "allow_all":
        return ScheduleOverlapPolicy.ALLOW_ALL
    return ScheduleOverlapPolicy.SKIP


def build_schedule(trigger: Trigger, timing: ScheduleTiming, task_queue: str) -> Schedule:
    """The trigger's Schedule as its row says, its pause included."""
    intervals = (
        [
            ScheduleIntervalSpec(
                every=timedelta(seconds=timing.interval_s),
                offset=timedelta(seconds=timing.offset_s),
            )
        ]
        if timing.interval_s is not None
        else []
    )
    return Schedule(
        action=ScheduleActionStartWorkflow(
            FIRE_WORKFLOW,
            args=[trigger.id, None],
            id=schedule_id(trigger.id),
            task_queue=task_queue,
            execution_timeout=FIRE_TIMEOUT,
        ),
        spec=ScheduleSpec(
            cron_expressions=list(timing.cron or []),
            intervals=intervals,
            time_zone_name=timing.time_zone,
        ),
        policy=SchedulePolicy(
            overlap=_overlap(trigger),
            catchup_window=timedelta(seconds=timing.catchup_window_s),
        ),
        state=ScheduleState(paused=trigger.paused, note=trigger.paused_reason or ""),
    )


async def ensure_trigger_schedule(client: Client, trigger: Trigger, task_queue: str) -> None:
    """Make the trigger's Schedule what its row says: created or replaced for a schedule
    trigger, removed for any other."""
    if trigger.kind != "schedule" or trigger.schedule is None:
        await delete_trigger_schedule(client, trigger.id)
        return
    schedule = build_schedule(trigger, trigger.schedule, task_queue)
    try:
        await client.create_schedule(schedule_id(trigger.id), schedule)
    except ScheduleAlreadyRunningError:

        def replace(_: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(schedule=schedule)

        await client.get_schedule_handle(schedule_id(trigger.id)).update(replace)


async def delete_trigger_schedule(client: Client, trigger_id: str) -> None:
    try:
        await client.get_schedule_handle(schedule_id(trigger_id)).delete()
    except RPCError as error:
        if error.status != RPCStatusCode.NOT_FOUND:
            raise


async def next_fires(client: Client, trigger_id: str) -> list[datetime]:
    """When the trigger's Schedule fires next; none for a paused or missing one."""
    try:
        described = await client.get_schedule_handle(schedule_id(trigger_id)).describe()
    except RPCError as error:
        if error.status != RPCStatusCode.NOT_FOUND:
            raise
        return []
    if described.schedule.state.paused:
        return []
    return list(described.info.next_action_times)
