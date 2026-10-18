"""``PrinterWatch``: one per LAN-linked printer, keeping its latest MQTT report in
Postgres (#3823, plan 2026-10-16-printer-identity-native-path, Task 6).

The workflow runs on the ``bambuddy`` queue and its one activity (`printers/watch.py`)
on `follow_queue`, as `FollowPrint`'s does: the activity holds its MQTT connection for
as long as the printer is reachable, so it must not take a slot a print run's short
activities wait for. Both are served by the print worker (`worker.run_print_worker`)
and, in-process, by the API (`main._run_print_worker`). Its id is
``printer-<registry id>``: one per physical printer, since a serial is one row's. Never
the serial: a workflow id rides on every Temporal span (``temporalWorkflowID``) and on
the SDK's failed-attempt warnings (#4186 re-review N1). A LAN-linked row's id is stable:
a merge removes only synthetic rows, which take no LAN link.

The activity's attempts retry without end (`FOLLOW_RETRY`): a printer switched off is
an attempt that fails to connect, then the next. Every `CONTINUE_AFTER`, or when the
server suggests it, the watch continues as new. The ``stop`` signal ends it (the LAN
unlink and the printer's delete send it); so does an activity that finds the row gone,
unlinked, or no KEK to open its access code with. The ``restart`` signal, sent with
signal-with-start by `restart_watch` (the LAN link), starts the watch when none runs,
and otherwise ends the current attempt and starts one that reads the row again: a new
host, code or pin takes effect at once, and a link that follows an unlink keeps the
watch the unlink's ``stop`` was ending (#4186 review I4). `ensure_watches` is the
housekeeping prune Schedule's (every 300 s, `housekeeping.PRUNE_SWEEPS`): it starts a
watch for every LAN-linked row that has none, so a stopped or failed one comes back.

An attempt is cancelled with ``WAIT_CANCELLATION_COMPLETED``, and the run waits for it
before the next one starts (a restart, a continue-as-new) or the run ends, so one
printer never has two MQTT connections from ScadBuddy at once. The cancel reaches the
attempt on its next heartbeat (within the SDK's throttle: 0.8 of `WATCH_HEARTBEAT`).

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
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from scadbuddy.workflows.follow import FOLLOW_RETRY, RPC_TIMEOUT, follow_queue

if TYPE_CHECKING:
    from scadbuddy.printers.store import PrinterStore

logger = logging.getLogger(__name__)

PRINTER_WATCH_WORKFLOW = "PrinterWatch"
PRINTER_WATCH_ACTIVITY = "printer_watch"
STOP_SIGNAL = "stop"
RESTART_SIGNAL = "restart"
#: The activity heartbeats at least every `watch.HEARD_EVERY` (10 s).
WATCH_HEARTBEAT = timedelta(seconds=30)
#: One attempt's bound, and a run's: past it the watch continues as new.
CONTINUE_AFTER = timedelta(hours=24)


class PrinterWatchInput(BaseModel):
    printer_id: str


def watch_id(printer_id: str) -> str:
    return f"printer-{printer_id}"


@workflow.defn(name=PRINTER_WATCH_WORKFLOW)
class PrinterWatch:
    def __init__(self) -> None:
        self.stopped = False
        self.restart = False

    @workflow.signal(name=STOP_SIGNAL)
    def stop(self) -> None:
        self.stopped = True

    @workflow.signal(name=RESTART_SIGNAL)
    def restart_signal(self) -> None:
        self.stopped = False
        self.restart = True

    @workflow.run
    async def run(self, printer_id: str, continue_after_seconds: float | None = None) -> str:
        """``continue_after_seconds`` is the run's bound (`CONTINUE_AFTER` when
        ``None``), carried to the next run; a test shortens it."""
        bound = (
            CONTINUE_AFTER
            if continue_after_seconds is None
            else timedelta(seconds=continue_after_seconds)
        )
        deadline = workflow.now() + bound
        # A restart that came with this run's start (signal-with-start) asks for nothing
        # more than the start itself.
        self.restart = False
        while True:
            attempt: workflow.ActivityHandle[str] = workflow.start_activity(
                PRINTER_WATCH_ACTIVITY,
                PrinterWatchInput(printer_id=printer_id),
                result_type=str,
                task_queue=follow_queue(workflow.info().task_queue),
                start_to_close_timeout=bound,
                heartbeat_timeout=WATCH_HEARTBEAT,
                retry_policy=FOLLOW_RETRY,
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            with suppress(TimeoutError):
                await workflow.wait_condition(
                    lambda attempt=attempt: (  # type: ignore[misc]
                        self.stopped
                        or self.restart
                        or attempt.done()
                        or workflow.info().is_continue_as_new_suggested()
                    ),
                    timeout=max(deadline - workflow.now(), timedelta(seconds=1)),
                )
            if attempt.done() and not (self.stopped or self.restart):
                ended: str = await attempt
                return ended
            attempt.cancel()
            with suppress(ActivityError, asyncio.CancelledError):
                await attempt
            if self.stopped:
                return "stopped"
            if self.restart:
                self.restart = False
                continue
            workflow.continue_as_new(args=[printer_id, continue_after_seconds])


async def ensure_watch(client: Client, task_queue: str, printer_id: str) -> None:
    """Start the printer's watch unless one is running (the sweep's: a running watch is
    left as it is)."""
    with suppress(WorkflowAlreadyStartedError):
        await client.start_workflow(
            PRINTER_WATCH_WORKFLOW,
            args=[printer_id, None],
            id=watch_id(printer_id),
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            rpc_timeout=RPC_TIMEOUT,
        )


async def restart_watch(client: Client, task_queue: str, printer_id: str) -> None:
    """Start the printer's watch, or restart the running one on the row as it is now:
    signal-with-start, so a ``stop`` still being handled cannot swallow it."""
    await client.start_workflow(
        PRINTER_WATCH_WORKFLOW,
        args=[printer_id, None],
        id=watch_id(printer_id),
        task_queue=task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        start_signal=RESTART_SIGNAL,
        rpc_timeout=RPC_TIMEOUT,
    )


async def stop_watch(client: Client, printer_id: str) -> None:
    """Signal the printer's watch to end; a watch that is not running is left alone."""
    try:
        await client.get_workflow_handle(watch_id(printer_id)).signal(
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
            await ensure_watch(client, task_queue, row.id)
        except Exception:
            logger.warning("could not start a printer watch", extra={"printer_id": row.id})
            continue
        started.append(row.id)
    return started
