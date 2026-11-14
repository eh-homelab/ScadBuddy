"""One watcher per staged customer file (#6133, spec 2026-11-13 §6.5).

``StagedFile`` runs on the ``library`` queue (the API's worker), one execution per upload,
id ``staged-file:<upload id>``. The presign route starts it before it answers the URL
(`orders/storefront/staged_files.py` `watch`), so no file lands in the staging area without
a watcher. It owns the file's life and ends when the file is done with: imported, or
cleaned up.

Each turn runs one activity, ``staged_file_step``, which reads the file's state from
Postgres under the order's lock and, when the file is due for clean-up, cleans it up in the
same step (`staged_files.step`): the database is the truth, so a lost signal costs time,
never a file. The step answers the phase and when the next deadline falls:

- ``unclaimed``: due `UNCLAIMED_GRACE` after the URL's expiry;
- ``claimed`` on a cart not submitted: due `STAGING_TTL` after the claim;
- ``submitted``: no deadline while the order is live; the run re-reads every
  `SUBMITTED_RECHECK` and waits for the import (a later PR) or a person's decline; once the
  order is cancelled or closed, due `ENDED_GRACE` after its end (a reopen clears it, #6164);
- ``ended``: cleaned up, imported, or gone; the run ends.

The workflow sleeps until that deadline or a signal, whichever comes first, then steps
again. Every signal (``claimed``, ``unclaimed``, ``submitted``, ``checked``, ``imported``,
``declined``, ``closed``, ``retry``) only wakes it: the step reads what changed. The step
retries with backoff and no limit, so a run does not end until its clean-up succeeded.
A run that waits long (a submitted file) continues as new once its history passes
`HISTORY_LIMIT` events.

On the versioned ``library`` worker it is AUTO_UPGRADE (`VersionedStagedFile`): a run lives
for days, so a change to the commands ``run`` issues goes behind ``workflow.patched``.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior

STAGED_FILE_WORKFLOW = "StagedFile"
STEP_ACTIVITY = "staged_file_step"
#: The workflow id's prefix: one watcher per upload.
WORKFLOW_PREFIX = "staged-file:"
#: How often a submitted file is read again while nothing signals it.
SUBMITTED_RECHECK = timedelta(hours=1)
#: A step reads a few rows, deletes at most two objects and writes once.
STEP_TIMEOUT = timedelta(minutes=2)
#: Past this many events a run continues as new (a submitted file may wait for days).
HISTORY_LIMIT = 2000
#: Woken a little after a deadline, so the step's clock (the database's) has passed it.
DEADLINE_MARGIN = timedelta(seconds=1)
#: The step retries for ever: a run must not end before its file is cleaned up.
STEP_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=10),
    maximum_attempts=0,
)

Phase = Literal["unclaimed", "claimed", "submitted", "ended"]
#: The signals the routes send. Each wakes the run; the step reads what changed.
SIGNALS = (
    "claimed",
    "unclaimed",
    "submitted",
    "checked",
    "imported",
    "declined",
    "closed",
    "retry",
)


class StagedFileState(BaseModel):
    """What `staged_file_step` found: the phase, the next deadline (None: none), and, once
    ``ended``, why (``unclaimed``, ``expired``, ``removed``, ``declined``, ``order_ended``,
    ``gone``)."""

    phase: Phase
    due: datetime | None = None
    reason: str | None = None


def workflow_id(upload_id: str) -> str:
    return f"{WORKFLOW_PREFIX}{upload_id}"


@workflow.defn(name=STAGED_FILE_WORKFLOW)
class StagedFile:
    def __init__(self) -> None:
        self._woken = False

    @workflow.run
    async def run(self, upload_id: str) -> str:
        """Why the file's life ended."""
        while True:
            # Cleared before the step, so a signal that lands while it runs wakes the
            # next wait at once instead of being lost.
            self._woken = False
            found: StagedFileState = await workflow.execute_activity(
                STEP_ACTIVITY,
                upload_id,
                result_type=StagedFileState,
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=STEP_RETRY,
            )
            if found.phase == "ended":
                return found.reason or "ended"
            if found.due is None:
                wait = SUBMITTED_RECHECK
            else:
                wait = max(found.due - workflow.now(), timedelta(0)) + DEADLINE_MARGIN
            with suppress(TimeoutError):
                await workflow.wait_condition(lambda: self._woken, timeout=wait)
            if workflow.info().get_current_history_length() > HISTORY_LIMIT:
                workflow.continue_as_new(upload_id)

    def _wake(self) -> None:
        self._woken = True

    @workflow.signal(name="claimed")
    def claimed(self) -> None:
        self._wake()

    @workflow.signal(name="unclaimed")
    def unclaimed(self) -> None:
        self._wake()

    @workflow.signal(name="submitted")
    def submitted(self) -> None:
        self._wake()

    @workflow.signal(name="checked")
    def checked(self) -> None:
        self._wake()

    @workflow.signal(name="imported")
    def imported(self) -> None:
        self._wake()

    @workflow.signal(name="declined")
    def declined(self) -> None:
        self._wake()

    @workflow.signal(name="closed")
    def closed(self) -> None:
        self._wake()

    @workflow.signal(name="retry")
    def retry(self) -> None:
        self._wake()


@workflow.defn(name=STAGED_FILE_WORKFLOW, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedStagedFile(StagedFile):
    """``StagedFile`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE: a run waits
    days for a cart or an import, so it must move to each new build. Its own class because
    an unversioned worker cannot complete a workflow that declares a behavior."""

    @workflow.run
    async def run(self, upload_id: str) -> str:
        return await super().run(upload_id)
