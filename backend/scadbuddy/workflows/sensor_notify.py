"""``SensorNotifyPass``: the sensors' queued alert notifications sent through Bambuddy
(#6646, plan `docs/superpowers/plans/2026-11-18-filament-weights-sensors.md` Decisions 40
and 41).

A Temporal Schedule, ``scadbuddy-sensor-notify-<queue>``, starts it every `INTERVAL` on
the ``bambuddy`` queue (overlap SKIP, and once at each API boot); the API sets the
Schedule up (`main._set_up_housekeeping`), and the print worker serves the run
(`client.bambuddy_worker`), or the API in-process. Its one activity
(`filament/sensor_notify.py` `SensorNotifyActivities`) claims, sends and ends at most
`sensor_notify.PASS_LIMIT` rows. It does not hang on ``spool_overlay_refresh_seconds``:
a notification waits at most about a minute for its pass, whatever that setting is.

The activity is tried once: a claimed row is never sent twice, and the next tick is the
retry for the rows still queued. On the versioned print worker the workflow is
AUTO_UPGRADE (`client.print_deployment`'s default): a change to its commands goes behind
``workflow.patched``. It is a new workflow type on the queue, so the release that adds it
rolls ``scadbuddy-print`` with ``Recreate`` (README, "A release that needs
``Recreate``"); a run an old build takes meanwhile fails its workflow task until its
`RUN_TIMEOUT`, and the next tick sends what it would have.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client, ScheduleActionStartWorkflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from scadbuddy.workflows.housekeeping import ensure_workflow_schedule

with workflow.unsafe.imports_passed_through():
    from scadbuddy.filament.sensor_notify import SENSOR_NOTIFY_ACTIVITY, DeliveryReport

SENSOR_NOTIFY_WORKFLOW = "SensorNotifyPass"
SENSOR_NOTIFY_SCHEDULE = "scadbuddy-sensor-notify"
#: How often a pass runs: Bambuddy takes 20 app messages a minute, a pass sends 10.
INTERVAL = timedelta(minutes=1)
#: One pass: a claim, then sends. The pass starts none after `sensor_notify.PASS_BUDGET`
#: (75 s), and one send takes at most 3 x `sensor_notify.SEND_PHASE_TIMEOUT` (15 s), so the
#: sends end by 90 s and the rest of the 2 minutes is for the ends; the rows it did not
#: try go back to ``queued``. Under `sensors.INTERRUPTED_AFTER`, so a live send is never
#: ended ``interrupted``.
SEND_TIMEOUT = timedelta(minutes=2)
#: A run that cannot finish (an old build that lacks the type) still ends, and the
#: Schedule's overlap SKIP stops holding the later ticks back.
RUN_TIMEOUT = timedelta(minutes=3)
_ONCE = RetryPolicy(maximum_attempts=1)


def sensor_notify_schedule_id_for(task_queue: str) -> str:
    """One Schedule per queue: a test's app (a queue of its own) never moves another's."""
    return f"{SENSOR_NOTIFY_SCHEDULE}-{task_queue}"


@workflow.defn(name=SENSOR_NOTIFY_WORKFLOW)
class SensorNotifyPass:
    @workflow.run
    async def run(self) -> DeliveryReport | None:
        """The pass's counts; None when its one attempt failed (logged)."""
        try:
            report: DeliveryReport = await workflow.execute_activity(
                SENSOR_NOTIFY_ACTIVITY,
                result_type=DeliveryReport,
                start_to_close_timeout=SEND_TIMEOUT,
                retry_policy=_ONCE,
            )
        except ActivityError:
            workflow.logger.warning("could not send the sensor notifications", exc_info=True)
            return None
        return report


async def ensure_sensor_notify_schedule(client: Client, task_queue: str) -> None:
    """The pass's Schedule every `INTERVAL` on ``task_queue`` (the ``bambuddy`` queue),
    then one run now. A Schedule an operator paused stays paused, and is not run."""
    schedule_id = sensor_notify_schedule_id_for(task_queue)
    action = ScheduleActionStartWorkflow(
        SENSOR_NOTIFY_WORKFLOW,
        id=schedule_id,
        task_queue=task_queue,
        execution_timeout=RUN_TIMEOUT,
    )
    await ensure_workflow_schedule(client, schedule_id, INTERVAL.total_seconds(), action)
