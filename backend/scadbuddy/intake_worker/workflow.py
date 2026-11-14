"""``CustomerFileCheck``: one check of a customer's staged file (#6133, spec 2026-11-13 §7), on
the intake worker's queue (``customer-files``). The CRUD service starts it after a claim
(`orders/storefront/intake.py` `start_checks`), and the staged files' backstop starts it
again for a source still waiting past `CHECK_STALE`.

The run calls the check activity, which posts its own verdict (ready or refused). One that
cannot finish (`CHECK_RETRY`'s attempts spent: a timeout, a crash, a download that failed)
posts ``failed``, which the shop can run again. The intake worker is not versioned: a
change to this workflow's commands goes behind `workflow.patched`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from scadbuddy.intake_worker.wire import CHECK_ACTIVITY, CHECK_WORKFLOW, FAILED_ACTIVITY

#: One attempt: a download of up to 256 MiB, then the child's 5 minutes.
CHECK_TIMEOUT: Final = timedelta(minutes=15)
#: The activity heartbeats every `core.heartbeat.HEARTBEAT_EVERY` (10 s) while it runs, so
#: an attempt whose pod died is tried again after this, not after `CHECK_TIMEOUT`.
CHECK_HEARTBEAT: Final = timedelta(minutes=1)
CHECK_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
#: Posting ``failed`` waits out a CRUD service that does not answer.
FAILED_TIMEOUT: Final = timedelta(minutes=2)
FAILED_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=5), maximum_interval=timedelta(minutes=10)
)


@workflow.defn(name=CHECK_WORKFLOW)
class CustomerFileCheck:
    @workflow.run
    async def run(self, source_id: str) -> str:
        """The check's outcome: ``ready``, ``refused``, ``failed`` or ``skipped``."""
        try:
            outcome: str = await workflow.execute_activity(
                CHECK_ACTIVITY,
                source_id,
                result_type=str,
                start_to_close_timeout=CHECK_TIMEOUT,
                heartbeat_timeout=CHECK_HEARTBEAT,
                retry_policy=CHECK_RETRY,
            )
        except ActivityError:
            outcome = await workflow.execute_activity(
                FAILED_ACTIVITY,
                source_id,
                result_type=str,
                start_to_close_timeout=FAILED_TIMEOUT,
                retry_policy=FAILED_RETRY,
            )
        return outcome
