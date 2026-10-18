"""``PrinterWatch``: one per LAN-linked printer, keeping its latest MQTT report in
Postgres (#3823, plan 2026-10-16-printer-identity-native-path, Task 6).

The workflow runs on the ``bambuddy`` queue and its one activity (`printers/watch.py`)
on `follow_queue`, as `FollowPrint`'s does: the activity holds its MQTT connection for
as long as the printer is reachable, so it must not take a slot a print run's short
activities wait for. Both are served by the print worker (`worker.run_print_worker`)
and, in-process, by the API (`main._run_print_worker`). Its id is
``printer-<serial>``: one per physical printer, whichever registry row names it.

The activity's attempts retry without end (`FOLLOW_RETRY`): a printer switched off is
an attempt that fails to connect, then the next. Every `CONTINUE_AFTER`, or when the
server suggests it, the watch continues as new. The ``stop`` signal ends it (the LAN
unlink and the printer's delete send it); so does an activity that finds the row gone,
unlinked, or no KEK to open its access code with. `ensure_watches` is the housekeeping
sweep's: it starts a watch for every LAN-linked row, so a stopped or failed one comes
back.

A change to ``PrinterWatch.run``'s commands goes behind ``workflow.patched``: a watch
lasts a day per run and moves to each new build (AUTO_UPGRADE on the versioned print
worker, `client.print_deployment`).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

with workflow.unsafe.imports_passed_through():
    from scadbuddy.workflows.follow import FOLLOW_RETRY, RPC_TIMEOUT, follow_queue

if TYPE_CHECKING:
    from scadbuddy.printers.store import PrinterStore

logger = logging.getLogger(__name__)

PRINTER_WATCH_WORKFLOW = "PrinterWatch"
PRINTER_WATCH_ACTIVITY = "printer_watch"
STOP_SIGNAL = "stop"
#: The activity heartbeats at least every `watch.HEARD_EVERY` (10 s).
WATCH_HEARTBEAT = timedelta(seconds=30)
#: One attempt's bound, and a run's: past it the watch continues as new.
CONTINUE_AFTER = timedelta(hours=24)


class PrinterWatchInput(BaseModel):
    printer_id: str


def watch_id(serial: str) -> str:
    return f"printer-{serial}"


@workflow.defn(name=PRINTER_WATCH_WORKFLOW)
class PrinterWatch:
    def __init__(self) -> None:
        self.stopped = False

    @workflow.signal(name=STOP_SIGNAL)
    def stop(self) -> None:
        self.stopped = True

    @workflow.run
    async def run(self, printer_id: str) -> str:
        attempt: workflow.ActivityHandle[str] = workflow.start_activity(
            PRINTER_WATCH_ACTIVITY,
            PrinterWatchInput(printer_id=printer_id),
            result_type=str,
            task_queue=follow_queue(workflow.info().task_queue),
            start_to_close_timeout=CONTINUE_AFTER,
            heartbeat_timeout=WATCH_HEARTBEAT,
            retry_policy=FOLLOW_RETRY,
        )
        with suppress(TimeoutError):
            await workflow.wait_condition(
                lambda: (
                    self.stopped or attempt.done() or workflow.info().is_continue_as_new_suggested()
                ),
                timeout=CONTINUE_AFTER,
            )
        if attempt.done() and not self.stopped:
            ended: str = await attempt
            return ended
        attempt.cancel()
        with suppress(ActivityError, asyncio.CancelledError):
            await attempt
        if self.stopped:
            return "stopped"
        workflow.continue_as_new(args=[printer_id])


async def ensure_watch(client: Client, task_queue: str, printer_id: str, serial: str) -> None:
    """Start ``serial``'s watch unless one is running."""
    with suppress(WorkflowAlreadyStartedError):
        await client.start_workflow(
            PRINTER_WATCH_WORKFLOW,
            printer_id,
            id=watch_id(serial),
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            rpc_timeout=RPC_TIMEOUT,
        )


async def stop_watch(client: Client, serial: str) -> None:
    """Signal ``serial``'s watch to end; a watch that is not running is left alone."""
    try:
        await client.get_workflow_handle(watch_id(serial)).signal(
            STOP_SIGNAL, rpc_timeout=RPC_TIMEOUT
        )
    except RPCError as error:
        if error.status != RPCStatusCode.NOT_FOUND:
            raise


async def ensure_watches(printers: PrinterStore, client: Client, task_queue: str) -> list[str]:
    """The housekeeping sweep: a watch for every LAN-linked row. Returns their ids."""
    rows = await asyncio.to_thread(printers.list)
    started: list[str] = []
    for row in rows:
        if row.lan_host is None:
            continue
        try:
            await ensure_watch(client, task_queue, row.id, row.serial)
        except Exception:
            logger.warning("could not start a printer watch", extra={"printer_id": row.id})
            continue
        started.append(row.id)
    return started
