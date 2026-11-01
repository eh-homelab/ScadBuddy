"""``lan_wait`` keeps whether a report named the print across a retry (#4541, #4965)."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.printers.lan_models import LanStarted
from scadbuddy.printers.print_activities import LanActivities, LanDeps
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.workflows.print_models import LanWaitInput

STARTED = LanStarted(
    subtask_name="scadbuddy-run-p1", started_at=datetime(2026, 10, 29, 12, tzinfo=UTC)
)
#: The printer idle under another print's name, seen after the start.
MOVED_ON = (
    {"gcode_state": "IDLE", "subtask_name": "theirs"},
    STARTED.started_at + timedelta(hours=3),
)


class Reports:
    def __init__(self, report: tuple[dict[str, Any], datetime]) -> None:
        self.report = report

    def get(self, printer_id: str) -> tuple[dict[str, Any], datetime]:
        return self.report


def _wait() -> LanActivities:
    deps = LanDeps(
        printers=cast(PrinterStore, None),
        settings_store=cast(SettingsStore, None),
        reports=cast(PrinterReportStore, Reports(MOVED_ON)),
        wait_poll=0.05,
    )
    return LanActivities(deps)


async def test_a_retry_that_had_seen_the_print_reads_another_name_as_its_end() -> None:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, heartbeat_details=[True])
    async with asyncio.timeout(5):
        ended: LanStarted = await env.run(
            _wait().lan_wait, LanWaitInput(printer="p", started=STARTED)
        )
    assert ended.ended == "unknown" and ended.subtask_name == STARTED.subtask_name


class Rack:
    def __init__(self) -> None:
        self.settled: list[tuple[str, str | None, bool]] = []

    async def settle_lan(
        self, run_id: str, subtask_name: str | None = None, *, timed: bool = True
    ) -> int:
        self.settled.append((run_id, subtask_name, timed))
        return 1


@pytest.mark.parametrize(
    ("report", "timed"),
    [
        (({"gcode_state": "FINISH", "subtask_name": STARTED.subtask_name}, MOVED_ON[1]), True),
        (MOVED_ON, False),
    ],
    ids=["finished", "moved on"],
)
async def test_a_start_it_waited_out_settles_its_rack_picks(
    report: tuple[dict[str, Any], datetime], timed: bool
) -> None:
    """#4543: each copy's pick is settled as the printer ends it, so Least used counts
    its wear before the next copy is picked; timed only when the report said it ended."""
    rack = Rack()
    deps = LanDeps(
        printers=cast(PrinterStore, None),
        settings_store=cast(SettingsStore, None),
        reports=cast(PrinterReportStore, Reports(report)),
        rack=rack,  # type: ignore[arg-type]
        wait_poll=0.05,
    )
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, heartbeat_details=[True])
    started = STARTED.model_copy(update={"run_id": "run-1"})
    async with asyncio.timeout(5):
        await env.run(LanActivities(deps).lan_wait, LanWaitInput(printer="p", started=started))
    assert rack.settled == [("run-1", STARTED.subtask_name, timed)]


async def test_a_start_from_before_its_run_was_named_settles_nothing() -> None:
    rack = Rack()
    deps = LanDeps(
        printers=cast(PrinterStore, None),
        settings_store=cast(SettingsStore, None),
        reports=cast(PrinterReportStore, Reports(MOVED_ON)),
        rack=rack,  # type: ignore[arg-type]
        wait_poll=0.05,
    )
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, heartbeat_details=[True])
    async with asyncio.timeout(5):
        await env.run(LanActivities(deps).lan_wait, LanWaitInput(printer="p", started=STARTED))
    assert rack.settled == []


async def test_a_first_attempt_that_never_saw_the_print_keeps_waiting() -> None:
    beats: list[Any] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            await env.run(_wait().lan_wait, LanWaitInput(printer="p", started=STARTED))
    # What a retry would read: still not seen.
    assert beats and all(details == (False,) for details in beats)
