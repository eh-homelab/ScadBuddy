"""``FilamentBbPull``: one Bambuddy pull, read on the print worker (#6772, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md` Decisions 172-180).

The API starts it on the ``bambuddy`` queue and waits for its outcome
(`filament/bb_pull_apply.py` `execute_pull`): the spool refresh's ``filament_bambuddy_pull``
activity for the Schedule's pull, and ``POST /filament/bambuddy/import`` for a person's.
Its one activity (`filament/bb_pull_run.py` `FilamentPullActivities`) reads Bambuddy and
sends what it read to SB's CRUD routes, so the CRUD service makes no Bambuddy call for the
pull. The activity runs once: the next tick of the Schedule is the retry, as it was for the
API's activity, and a person imports again.

On the versioned print worker it is AUTO_UPGRADE (`client.print_deployment`): a run is
minutes long, but one left open across a deploy replays on the new code, so a change to
``FilamentBbPull.run``'s commands goes behind ``workflow.patched``, and
``tests/test_filament_pull_replay.py`` replays the committed histories.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from scadbuddy.filament.bb_pull_models import (
        FILAMENT_BB_PULL_WORKFLOW,
        PULL_READ_ACTIVITY,
        PullOutcome,
        PullStart,
    )

#: The reads and SB's writes, as the API's activity had 5 min for them, less the margin
#: the run's own timeout (`bb_pull_apply.PULL_RUN_TIMEOUT`) leaves.
PULL_READ_TIMEOUT = timedelta(minutes=4)
_ONCE = RetryPolicy(maximum_attempts=1)


@workflow.defn(name=FILAMENT_BB_PULL_WORKFLOW)
class FilamentBbPull:
    @workflow.run
    async def run(self, start: PullStart) -> PullOutcome:
        """The activity's outcome; ``failed`` when it raised (logged). An import's route
        answers a ``failed`` with no error as the pull not finishing."""
        try:
            outcome: PullOutcome = await workflow.execute_activity(
                PULL_READ_ACTIVITY,
                start,
                result_type=PullOutcome,
                start_to_close_timeout=PULL_READ_TIMEOUT,
                retry_policy=_ONCE,
            )
        except ActivityError:
            workflow.logger.warning("could not pull Bambuddy's spools", exc_info=True)
            return PullOutcome(outcome="failed")
        return outcome
