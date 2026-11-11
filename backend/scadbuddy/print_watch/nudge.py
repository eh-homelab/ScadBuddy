"""The print watcher's LAN nudge (#4699 phase 3, design §2 "LAN path"): a LAN job start
starts one watcher pass at once, so the archive is taken in seconds, not at the next
prune Schedule tick (up to `PRUNE_INTERVAL`, 300 s, later).

The printer's watch (`printers/watch.py`) sees the start: a report write whose running
print is new (`printers/reports.py` `started_print`). It calls `Nudger.nudge`, which
starts a pass only when all of these are true (`wanted`):

- no native run claims the start: every LAN start a ScadBuddy run makes is named
  `scadbuddy-<run id[:8]>-p<plate>[-c<copy>]` (`workflows/printing.py` `_start_lan`), and
  its run records the print itself. A job of that name that a person started again at
  the printer gets no nudge; the Schedule's pass still takes it (its archive's hash links
  it to its output);
- the watcher is on (`print_auto_import`);
- the printer is linked to a Bambuddy printer (only Bambuddy archives the job; a
  LAN-only printer is phase 4), and that printer is not in
  `print_auto_import_skip_printers`.

The pass is a ``Housekeeping`` run with `AUTO_IMPORT_SWEEP` only, on the ``library``
queue, so it runs in the API's ``library`` worker, the one process with SQL access (root
CLAUDE.md, "Database access (#5295)"). The watch runs in the print worker (or the API,
in-process): the nudge is a Temporal start, with no database client, grant or CRUD route
of its own. It reads the settings over the CRUD route (`crud/print.py`
`RemoteSettingsStore`) and the printer row through the watch's own ``PrinterStore.get``,
the read each watch attempt already makes (on the print worker's SQL until #5298 Task
29 moves it to a route). ``Housekeeping.run``
already takes the sweeps to run, so its commands are unchanged and no patch is needed.

Debounce:

- One nudge per job: the row's ``running_subtask`` changes once per job, under its row
  lock, so a job's repeated reports, a reconnect's ``pushall`` and a new watch attempt
  start nothing again.
- One nudge run in flight: every nudge starts the workflow id `nudge_id` with
  ``USE_EXISTING``, so a start while a nudge run waits or runs joins that run. A closed
  run's id is free again (``ALLOW_DUPLICATE``), so the next job's nudge starts a new one.

An early nudge: Bambuddy makes the archive only once it has read the 3MF from the
printer (`archive_print`, see `api/print_history.py` `_run_by_file`), so the run waits
`NUDGE_DELAY` before its pass (``start_delay``). A pass that still runs before the
archive exists loses nothing: the job's archive is newer than the watcher's floor and has
no row, so the prune Schedule's next pass takes it, as it did before this nudge. A second
nudge run per job would only shorten that rare wait, and would need a workflow change for
its timer; the Schedule is the retry.

The settings read is bounded by `SETTINGS_TIMEOUT` (#6034). The watch runs each nudge
as a task of its own, off its report loop, and cancels it past
`printers.watch.NUDGE_TIMEOUT` or when its attempt ends. A nudge that fails is logged
and never ends the watch: the Schedule's pass still comes.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Protocol

from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.models import PrinterRow
from scadbuddy.workflows.housekeeping import (
    AUTO_IMPORT_SWEEP,
    HOUSEKEEPING_WORKFLOW,
    housekeeping_timeout,
)

#: How long a nudge run waits before its pass: Bambuddy reads the 3MF from the printer
#: before it makes the archive.
NUDGE_DELAY = timedelta(seconds=30)
#: The bound of the settings read, retries included (#6034), below the watch's
#: `NUDGE_TIMEOUT`, so a stalled API never leaves the read's thread running past it.
SETTINGS_TIMEOUT = 5.0
#: The ``subtask_name`` of a native run's LAN start (`workflows/printing.py` `_start_lan`).
NATIVE_SUBTASK = re.compile(r"scadbuddy-[0-9a-f]{8}-p\d+(?:-c\d+)?")


def nudge_id(task_queue: str) -> str:
    """The one nudge run's workflow id per ``library`` queue."""
    return f"scadbuddy-print-watch-nudge-{task_queue}"


def claimed(subtask: str) -> bool:
    """Whether a native run's start has this name, so its run records the print."""
    return NATIVE_SUBTASK.fullmatch(subtask) is not None


def wanted(settings: StoredSettings, row: PrinterRow | None, subtask: str) -> bool:
    """Whether a pass would take the job ``subtask`` started on ``row``'s printer."""
    if claimed(subtask) or not settings.print_auto_import:
        return False
    if row is None or row.bambuddy_printer_id is None:
        return False
    return row.bambuddy_printer_id not in (settings.print_auto_import_skip_printers or [])


async def start_pass(
    client: Client, task_queue: str, *, delay: timedelta = NUDGE_DELAY
) -> WorkflowHandle[Any, Any]:
    """Start the nudge run, or join the one in flight."""
    sweeps = (AUTO_IMPORT_SWEEP,)
    return await client.start_workflow(
        HOUSEKEEPING_WORKFLOW,
        list(sweeps),
        id=nudge_id(task_queue),
        task_queue=task_queue,
        execution_timeout=housekeeping_timeout(sweeps) + delay,
        start_delay=delay,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )


Start = Callable[[str], Awaitable[object]]


class SettingsLoader(Protocol):
    """`SettingsReader.load`: ``timeout`` bounds the whole read, retries included."""

    def __call__(self, timeout: float | None = None) -> StoredSettings: ...


class Nudger:
    """The watch's nudge. ``load_settings`` and ``get_printer`` are blocking (run in a
    thread); ``start`` defaults to `start_pass` on the activity's own client."""

    def __init__(
        self,
        load_settings: SettingsLoader,
        get_printer: Callable[[str], PrinterRow | None],
        task_queue: str,
        *,
        start: Start | None = None,
    ) -> None:
        self.load_settings = load_settings
        self.get_printer = get_printer
        self.task_queue = task_queue
        self.start = start

    async def nudge(self, printer_id: str, subtask: str) -> bool:
        """Start a pass for the job ``subtask`` that started on ``printer_id``, when the
        watcher would take it (`wanted`). True when it asked for one."""
        if claimed(subtask):
            return False
        settings = await asyncio.to_thread(self.load_settings, timeout=SETTINGS_TIMEOUT)
        if not settings.print_auto_import:
            return False
        row = await asyncio.to_thread(self.get_printer, printer_id)
        if not wanted(settings, row, subtask):
            return False
        if self.start is not None:
            await self.start(self.task_queue)
        else:
            await start_pass(activity.client(), self.task_queue)
        return True
