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

A run of several plates or copies (#4541) starts one at a time, each once the printer has
finished the one before (`lan_ended`, which the run's ``lan_wait`` polls): a start the
run waited out carries how it ended (`LanStarted.ended`), since the printer reports the
next one by then, and each start still to come (`PrintRunResult.lan_waiting`) is a copy
``queued``, as one waiting in Bambuddy's queue is, until the run stops (``lan_stopped``).
While the run waits for a cleared plate (#4939, ``lan_awaiting_clear``) the next one says
so (`AWAITING_CLEAR`). The progress names the run's controls (`lan_control`, #4942): stop
the rest, and say the plate is clear.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from scadbuddy.bambuddy.follow import MAX_AGE, ReadHook
from scadbuddy.bambuddy.progress import CopyProgress, LanRunControl, PrintProgress, from_plates
from scadbuddy.bambuddy.runs import LatestRun, PrintRun, read_latest
from scadbuddy.bambuddy.stages import Stage
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.problems import DATABASE_ERRORS
from scadbuddy.printers.lan_models import LanStarted, LanStopped
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


#: The last report of a start the printer went idle after without saying how it ended:
#: restarted, say, which clears ``FINISH`` (`lan_ended`).
IDLE_SINCE = "The printer has been idle since, so how this print ended is not known."


def shows(report: tuple[dict[str, Any], datetime] | None, started: LanStarted) -> bool:
    """Whether ``report`` names ``started``'s print (each start's name is its own)."""
    return report is not None and report[0].get("subtask_name") == started.subtask_name


def lan_ended(
    report: tuple[dict[str, Any], datetime] | None, started: LanStarted, *, seen: bool
) -> LanStarted | None:
    """``started`` with how it ended (`LanStarted.ended`), once a report seen after the
    start shows the printer no longer printing: its own ``FINISH`` or ``FAILED``, or
    another print's end, or ``IDLE``. ``None`` while it, or a print started since, runs,
    and while no report has been seen since the start. A report that stops is waited
    out, never read as an end: the next start needs the printer to answer anyway.

    Another ``subtask_name`` is read as an end (``MOVED_ON``). That holds because the
    report is whole: `PrinterWatch` asks for a ``pushall`` on every connect
    (`lan.LanPrinter.reports`) and merges each delta over it, so a report that names
    no ``subtask_name`` yet never reaches this.

    Another name, or ``IDLE`` under one, counts only once ``seen``: a report has named this
    print (`shows`). ``started_at`` is the print worker's clock and the report's time the
    watch's, maybe another pod's, and the watch may not have merged the start's delta yet,
    so a report from before the start can look newer. Read as an end, it would start the
    next copy over this one."""
    if report is None or report[1] <= started.started_at:
        return None
    data, seen_at = report
    if not seen and not shows(report, started):
        return None
    state = data.get("gcode_state")
    if state in _RUNNING:
        return None
    progress = from_lan_report(report, started, printer_name=None, now=seen_at)
    if progress.settled:
        detail = progress.copies_detail
        message = detail[0].message if detail else progress.error_message
        return started.model_copy(update={"ended": progress.stage, "ended_message": message})
    if state == "IDLE":
        return started.model_copy(update={"ended": "unknown", "ended_message": IDLE_SINCE})
    return None


def _ended(started: LanStarted, printer_name: str | None) -> PrintProgress:
    """A start the run waited out: how it ended, as its run recorded it."""
    stage = started.ended or "unknown"
    copy = CopyProgress(printer_name=printer_name, stage=stage, message=started.ended_message)
    return PrintProgress(
        route="lan",
        stage=stage,
        settled=True,
        copies_completed=1 if stage == "done" else 0,
        copies_failed=1 if stage == "failed" else 0,
        error_message=started.ended_message if stage == "failed" else None,
        copies_detail=[copy],
        bambuddy_url="",
    )


#: A copy still to start, while the printer prints the one before (#4541).
WAITING = "Waiting for the printer to finish the print before it."
#: The next copy, once the printer has ended the one before, while the run waits for
#: someone to say the plate is clear (#4939, ``lan_require_plate_clear``).
AWAITING_CLEAR = "Waiting for someone to say the plate of {printer} is clear."


def _waiting(
    printer_name: str | None, stopped: LanStopped | None, reason: str = WAITING
) -> PrintProgress:
    """A start still to come: ``queued`` as one in Bambuddy's queue, or, once the run has
    stopped, never to start."""
    if stopped is None:
        copy = CopyProgress(printer_name=printer_name, stage="queued", waiting_reason=reason)
        return PrintProgress(route="lan", stage="queued", copies_detail=[copy], bambuddy_url="")
    copy = CopyProgress(printer_name=printer_name, stage=stopped.stage, message=stopped.message)
    failed = stopped.stage == "failed"
    return PrintProgress(
        route="lan",
        stage=stopped.stage,
        settled=True,
        copies_failed=1 if failed else 0,
        copies_cancelled=0 if failed else 1,
        error_message=stopped.message if failed else None,
        copies_detail=[copy],
        bambuddy_url="",
    )


def lan_control(run: PrintRun) -> LanRunControl | None:
    """A succeeded native run's prints still to start and its controls (#4942, #4939);
    ``None`` once it started every one or they were stopped."""
    result = run.result
    if run.status != "succeeded" or result is None or result.path != "native":
        return None
    if not result.lan_waiting or result.lan_stopped is not None:
        return None
    return LanRunControl(
        run_id=run.id, waiting=len(result.lan_waiting), awaiting_clear=result.lan_awaiting_clear
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
        result = run.result
        plates = [
            _ended(plate, name)
            if plate.ended is not None
            else from_lan_report(report, plate, printer_name=name, now=now, max_age=self.max_age)
            for plate in started
        ]
        reasons = [WAITING] * len(result.lan_waiting)
        if reasons and result.lan_awaiting_clear:
            reasons[0] = AWAITING_CLEAR.format(printer=name or "the printer")
        plates += [_waiting(name, result.lan_stopped, reason) for reason in reasons]
        if len(plates) == 1:
            return plates[0].model_copy(update={"run_id": run.id})
        merged = from_plates(
            plates,
            [
                *(plate.plate_id or index for index, plate in enumerate(started, start=1)),
                *result.lan_waiting,
            ],
            slice_job_id=None,
            queue_item_id=None,
            bambuddy_url="",
        )
        return merged.model_copy(
            update={"route": "lan", "lan_run": lan_control(run), "run_id": run.id}
        )


class LanLastPrints(Protocol):
    def record_lan_progress(
        self, output_id: str, run_id: str, *, stage: Stage, settled: bool
    ) -> None: ...


def lan_last_print(prints: LanLastPrints) -> ReadHook:
    """The print follow's read hook for an output's LAN print (#4644): its last print
    (`library.output_prints`) says how the print goes, as each read finds it. A library
    file's print has no last-print row."""

    async def hook(run_subject: str, progress: PrintProgress) -> None:
        if progress.route != "lan" or progress.run_id is None:
            return
        if PrintSubject.from_run_subject(run_subject).kind != "output":
            return
        await asyncio.to_thread(
            prints.record_lan_progress,
            run_subject,
            progress.run_id,
            stage=progress.stage,
            settled=progress.settled,
        )

    return hook
