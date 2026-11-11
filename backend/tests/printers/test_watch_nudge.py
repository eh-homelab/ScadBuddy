"""The watch's nudge off its loop (#6034): a nudge that hangs never holds back the next
report, its heartbeat or the rack settle of a print a later report ends. No Postgres:
the stores are fakes, so these run everywhere."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.printers import watch as watch_module
from scadbuddy.printers.lan import LanPrinter
from scadbuddy.printers.reports import (
    Advance,
    JobStart,
    PrinterReportStore,
    advance_print,
    started_print,
)
from scadbuddy.printers.store import PrinterStore
from scadbuddy.printers.watch import WatchActivities
from scadbuddy.rack.usage import LanUsage
from scadbuddy.workflows.printer_watch import PrinterWatchInput
from tests.printers.test_watch import FINISH_X, RUNNING_X, Lan, Settles

PID = "p1"
RUNNING_Y = {"gcode_state": "RUNNING", "subtask_name": "y", "mc_percent": 1}


class Printers:
    def get(self, printer_id: str) -> Any:
        return SimpleNamespace(
            lan_host="10.0.0.9", lan_cert_sha256="ab" * 32, kind="physical", serial="S"
        )

    def open_access_code(self, printer_id: str) -> str:
        return "12345678"


class Reports:
    """`PrinterReportStore`'s writes, in memory."""

    def __init__(self) -> None:
        self.running: str | None = None
        self.written: list[dict[str, Any]] = []

    def advance(self, printer_id: str, report: dict[str, Any], seen_at: datetime) -> Advance:
        self.written.append(report)
        before = self.running
        self.running, ended = advance_print(before, report)
        return Advance(ended=ended, started=started_print(before, self.running))

    def touch(self, printer_id: str, seen_at: datetime) -> None:
        pass


class Hangs:
    """A nudge that never answers (Temporal or the API stalled)."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.cancelled: list[str] = []
        self.starts: list[JobStart] = []

    async def nudge(self, printer_id: str, start: JobStart) -> bool:
        subtask = start.subtask_name
        self.seen.append(subtask)
        self.starts.append(start)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.append(subtask)
            raise
        return True


def _watches(lan: Lan, reports: Reports, nudge: Hangs, rack: Settles) -> WatchActivities:
    return WatchActivities(
        cast(PrinterStore, Printers()),
        cast(PrinterReportStore, reports),
        None,
        connect=lambda *a: cast(LanPrinter, lan),
        write_every=0,
        rack=cast(LanUsage, rack),
        nudge=nudge.nudge,
    )


async def _run_for(watches: WatchActivities, seconds: float) -> None:
    env = ActivityEnvironment()
    task = asyncio.ensure_future(env.run(watches.printer_watch, PrinterWatchInput(printer_id=PID)))
    await asyncio.sleep(seconds)
    env.cancel()
    with pytest.raises(BaseException):  # noqa: B017 - cancelled, however the env says it
        await task


async def test_a_hanging_nudge_never_holds_back_the_next_report_or_its_settle() -> None:
    """#6034: with the nudge awaited inline, the end of ``x`` and the start of ``y`` waited
    out `NUDGE_TIMEOUT` (10 s) behind ``x``'s nudge."""
    lan = Lan(subscribes=True, deltas=[RUNNING_X, FINISH_X, RUNNING_Y])
    reports, nudges, rack = Reports(), Hangs(), Settles()
    await _run_for(_watches(lan, reports, nudges, rack), 0.4)

    assert reports.written == [RUNNING_X, FINISH_X, RUNNING_Y]
    assert [(printer, name) for printer, name, _, _ in rack.ended] == [(PID, "x")]
    assert nudges.seen == ["x", "y"]
    # #6037: both nudges still hung when the attempt ended, and its cleanup ended them
    # (from a set, so in no set order).
    assert sorted(nudges.cancelled) == ["x", "y"]


async def test_a_nudge_past_its_timeout_is_cancelled_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#6034: `NUDGE_TIMEOUT` frees the nudge's task, and the loop goes on meanwhile."""
    monkeypatch.setattr(watch_module, "NUDGE_TIMEOUT", 0.05)
    lan = Lan(subscribes=True, deltas=[RUNNING_X, RUNNING_Y])
    reports, nudges, rack = Reports(), Hangs(), Settles()
    with caplog.at_level(logging.WARNING, logger=watch_module.__name__):
        await _run_for(_watches(lan, reports, nudges, rack), 0.4)

    assert reports.written == [RUNNING_X, RUNNING_Y]
    assert nudges.cancelled == ["x", "y"]
    assert sum("nudge failed" in record.getMessage() for record in caplog.records) == 2


async def test_a_nudge_in_flight_ends_with_its_attempt() -> None:
    """A nudge still running when the attempt ends is cancelled, not left behind: the
    Schedule's pass takes the job."""
    lan = Lan(subscribes=True, deltas=[RUNNING_X])
    reports, nudges, rack = Reports(), Hangs(), Settles()
    await _run_for(_watches(lan, reports, nudges, rack), 0.2)
    await asyncio.sleep(0)

    assert nudges.cancelled == ["x"]


async def test_a_start_carries_its_reports_time_and_plate_gcode() -> None:
    """#4699 phase 4: a LAN-only printer's record is keyed by the job's start, and its
    file is checked against the plate G-code the printer named."""
    started = {**RUNNING_X, "gcode_file": "/data/Metadata/plate_2.gcode"}
    lan = Lan(subscribes=True, deltas=[started])
    reports, nudges, rack = Reports(), Hangs(), Settles()
    await _run_for(_watches(lan, reports, nudges, rack), 0.2)

    [start] = nudges.starts
    assert start.subtask_name == "x"
    assert start.gcode_file == "/data/Metadata/plate_2.gcode"
    assert start.started_at.tzinfo is not None
