"""A LAN print's progress, from the printer's own report (#3823, plan 2026-10-16 Task 9).

A plate started over the LAN has no queue item: its run's result names it by the
``subtask_name`` it was started under (`PrintRunResult.lan`), and its progress is the
printer's latest report (`PrinterReportStore`, written by its `PrinterWatch`), read
through the same `PrintProgress` the Bambuddy route answers, on the same
``print:<subject>`` topic. ``gcode_state`` is OpenBambuAPI ``mqtt.md``'s:
``PREPARE``/``SLICING``/``RUNNING`` is running, ``PAUSE`` running and paused,
``FINISH`` done, ``FAILED`` failed with its ``print_error``.

A report that stops (the printer off, its Wi-Fi gone) leaves the last one standing:
the progress stays what it said until it is `MAX_AGE` old, then settles as ``unknown``
(Review Focus 5). A report that names another ``subtask_name`` after the start says the
printer has moved on to another print, so this one settles as ``unknown`` too.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from scadbuddy.bambuddy.follow import MAX_AGE
from scadbuddy.bambuddy.progress import CopyProgress, PrintProgress, from_plates
from scadbuddy.bambuddy.runs import LatestRun, PrintRun, given_run, read_latest
from scadbuddy.bambuddy.stages import Stage
from scadbuddy.core.problems import DATABASE_ERRORS
from scadbuddy.printers.lan_models import LanStarted
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.status import print_error_text
from scadbuddy.printers.store import PrinterStore

logger = logging.getLogger(__name__)

_RUNNING = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})
PAUSED = "Paused on the printer."
MOVED_ON = "The printer has started another print since, so how this one ended is not known."
NOT_HEARD = "The printer has not reported for a while, so how this print ended is not known."


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def from_lan_report(
    report: tuple[dict[str, Any], datetime] | None,
    started: LanStarted,
    *,
    printer_name: str | None,
    now: datetime,
    max_age: timedelta = MAX_AGE,
) -> PrintProgress:
    """One LAN plate's progress from its printer's latest report (``None`` when there is
    none yet), and when that report was seen."""
    stage: Stage = "unknown"
    message: str | None = None
    waiting: str | None = None
    settled = False
    seen_at = started.started_at
    if report is not None and report[1] >= started.started_at:
        data, seen_at = report
        state = data.get("gcode_state")
        if data.get("subtask_name") != started.subtask_name:
            settled, message = True, MOVED_ON
        elif state == "FINISH":
            stage, settled = "done", True
        elif state == "FAILED":
            code = _int(data.get("print_error"))
            stage, settled = "failed", True
            message = (
                f"The printer reported print error {print_error_text(code)}."
                if code
                else "The print was stopped on the printer."
            )
        elif state in _RUNNING:
            stage = "running"
            waiting = PAUSED if state == "PAUSE" else None
    if not settled and now - seen_at > max_age:
        stage, settled, message = "unknown", True, NOT_HEARD
    copy = CopyProgress(
        printer_name=printer_name, stage=stage, message=message, waiting_reason=waiting
    )
    return PrintProgress(
        route="lan",
        stage=stage,
        settled=settled,
        copies_completed=1 if stage == "done" else 0,
        copies_failed=1 if stage == "failed" else 0,
        copies_in_progress=0 if settled else 1,
        error_message=message if stage == "failed" else None,
        copies_detail=[copy],
        # Nothing is on Bambuddy's queue to link to.
        bambuddy_url="",
    )


class LanProgress:
    """`bambuddy.progress.LanReads`: a subject's print, when its newest run printed over
    the LAN; ``None`` otherwise, and the Bambuddy route is read as before. ``latest``
    reads the newest run: a route's, read once already (`runs.given_run`), or a follow's
    `runs.RecentRuns`, which its newest-failure read shares, so a progress read adds no
    query of ``print_runs`` (#4540 review). A database that does not answer reads as no
    LAN print."""

    def __init__(
        self,
        latest: LatestRun,
        printers: PrinterStore,
        reports: PrinterReportStore,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_age: timedelta = MAX_AGE,
    ) -> None:
        self.latest = latest
        self.printers = printers
        self.reports = reports
        self.now = now
        self.max_age = max_age

    def given(self, run: PrintRun | None) -> LanProgress:
        """The same reads, over a newest run read already."""
        return LanProgress(
            given_run(run), self.printers, self.reports, now=self.now, max_age=self.max_age
        )

    async def read(self, run_subject: str) -> PrintProgress | None:
        run = await read_latest(self.latest, run_subject)
        if run is None or run.status != "succeeded" or run.result is None:
            return None
        started = run.result.lan
        printer = run.printer
        if not started or printer is None:
            return None
        try:
            report = await asyncio.to_thread(self.reports.get, printer)
            row = await asyncio.to_thread(self.printers.get, printer)
        except DATABASE_ERRORS:
            logger.warning("the printer registry is unreadable; a LAN print's progress is not read")
            return None
        name = row.name if row is not None else None
        now = self.now()
        plates = [
            from_lan_report(report, plate, printer_name=name, now=now, max_age=self.max_age)
            for plate in started
        ]
        if len(plates) == 1:
            return plates[0]
        merged = from_plates(
            plates,
            list(range(1, len(plates) + 1)),
            slice_job_id=None,
            queue_item_id=None,
            bambuddy_url="",
        )
        return merged.model_copy(update={"route": "lan"})
