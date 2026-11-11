"""``LanAutoImport``: the print watcher's record of one job on a LAN-only printer (#4699
phase 4, design §2 "LAN path").

A LAN-only printer (a registry printer with no Bambuddy link) gets no Bambuddy archive,
so the watcher's Schedule pass never sees its jobs. The printer's LAN watch sees each job
start (`printers/reports.py` `started_print`), and its nudge (`print_watch/nudge.py`)
starts this workflow for a job that no native run claims. It runs one activity
(`print_watch/lan.py` `LanImportActivities`): read the job's 3MF over the printer's own
FTPS while it prints, keep it in ScadBuddy's own library, and record the print through
the API (link it to a model with confidence, or put it in Unsorted imports).

The workflow and its activity run on the ``bambuddy`` queue: the print worker opens the
printer's access code there (or the API in-process, `main._run_print_worker`). The
activity reaches the database only through the API's CRUD route (root CLAUDE.md,
"Database access (#5295)").

One run per job: its id is `lan_import_id` (the printer, the job's name and its start),
started with ``USE_EXISTING`` and ``REJECT_DUPLICATE``, so a nudge sent again joins the
run or starts nothing. The record is natural on the same key, so an attempt that is
tried again records the job once.

The activity retries a failed read (`LAN_IMPORT_RETRY`) while the job prints: the printer
keeps the file in ``/cache`` until a later job takes its place. Its last attempt
(`MAX_ATTEMPTS`) records the print in the inbox with the reason, so a printer that never
answers still leaves a record.

A new workflow type: no history of it exists, so no ``workflow.patched`` guards it. A
change to ``LanAutoImport.run``'s commands goes behind one.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

LAN_AUTO_IMPORT_WORKFLOW = "LanAutoImport"
LAN_AUTO_IMPORT_ACTIVITY = "lan_auto_import"
#: How many times the activity runs at most; the last records the print, read or not.
MAX_ATTEMPTS = 6
#: 30 s, 1, 2, 4 and 8 minutes between attempts: about a quarter of an hour, inside most
#: prints, so the file is still in the printer's cache.
LAN_IMPORT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=10),
    maximum_attempts=MAX_ATTEMPTS,
)
#: One attempt's bound: a few FTPS reads of at most the library download cap, one upload
#: and one record.
ATTEMPT_TIMEOUT = timedelta(minutes=5)
#: The run's bound, with room for every attempt and the waits between them.
RUN_TIMEOUT = timedelta(hours=1)


class LanJob(BaseModel):
    """A job a LAN-only printer started: the registry printer, the job's
    ``subtask_name``, when the report that showed it start was received, and the plate
    G-code the printer named (``gcode_file``, e.g. ``/data/Metadata/plate_1.gcode``)."""

    printer_id: str
    subtask_name: str
    started_at: datetime
    gcode_file: str | None = None


def lan_import_id(job: LanJob) -> str:
    """The job's one run: the printer and a hash of the job's name and start. The name is
    the printer's text, so it is hashed, never put in the id as it is."""
    key = f"{job.subtask_name}\x00{job.started_at.isoformat()}".encode()
    return f"print-watch-lan-{job.printer_id}-{hashlib.sha256(key).hexdigest()[:16]}"


@workflow.defn(name=LAN_AUTO_IMPORT_WORKFLOW)
class LanAutoImport:
    @workflow.run
    async def run(self, job: LanJob) -> str:
        """The print's state as recorded: ``linked`` or ``inbox`` (or ``dismissed``, a
        record a person dismissed before this attempt)."""
        state: str = await workflow.execute_activity(
            LAN_AUTO_IMPORT_ACTIVITY,
            job,
            result_type=str,
            start_to_close_timeout=ATTEMPT_TIMEOUT,
            retry_policy=LAN_IMPORT_RETRY,
        )
        return state


async def start_lan_import(
    client: Client, task_queue: str, job: LanJob
) -> WorkflowHandle[Any, Any] | None:
    """Start the job's run, or join it while it runs; None when its run ended before (the
    job is recorded, or its run gave up), so nothing starts again."""
    try:
        return await client.start_workflow(
            LAN_AUTO_IMPORT_WORKFLOW,
            job,
            id=lan_import_id(job),
            task_queue=task_queue,
            execution_timeout=RUN_TIMEOUT,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return None
