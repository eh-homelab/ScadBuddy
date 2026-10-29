"""A LAN start counts only on a move this job made after the command (#4935).

#4933's live run showed the H2C naming a new job's ``subtask_name`` while its report
still carried the last job's values, and saying ``RUNNING`` within 2 s. The sequences
here are those shapes, fed to the real ``lan_print`` through a scripted printer: the
report read first (the baseline), then the reports after the command, in order. No
printer is contacted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import SlotChoice
from scadbuddy.bambuddy.trays import tray_spool_id
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.printers.lan import LanPrinter
from scadbuddy.printers.lan_models import LanReport, LanStarted
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.print_activities import LanActivities, LanDeps
from scadbuddy.printers.progress import from_lan_report
from scadbuddy.printers.store import PrinterStore
from scadbuddy.workflows.print_models import LAN_UNSURE, LanPrintInput, LanUploaded
from tests.conftest import PgPool
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"
NAME = "scadbuddy-run-p1"
#: The fake printer's whole report: an AMS, the AMS-HT and the toolhead.
REPORT = json.loads(
    (Path(__file__).parents[3] / "e2e-stack" / "fake-printer" / "report.json").read_text()
)["print"]
#: The last job's, as the printer left it.
LAST = {"subtask_name": "scadbuddy-last-p1", "layer_num": 54, "mc_percent": 100}
T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def state(**fields: Any) -> dict[str, Any]:
    return {**REPORT, **fields}


class Scripted:
    """A printer whose first report is ``baseline`` and whose reports after a command
    are ``after``, one a second, then silence."""

    def __init__(self, baseline: dict[str, Any], after: list[dict[str, Any]]) -> None:
        self.baseline = baseline
        self.after = after
        self.commands: list[dict[str, Any] | None] = []

    async def probe(self, timeout: float = 10.0) -> LanReport:
        return LanReport(serial=SERIAL, received_at=T0, print=self.baseline)

    async def reports(
        self, connected: asyncio.Event | None = None, *, command: dict[str, Any] | None = None
    ) -> AsyncIterator[LanReport]:
        self.commands.append(command)
        for at, report in enumerate(self.after, start=1):
            yield LanReport(serial=SERIAL, received_at=T0 + timedelta(seconds=at), print=report)
        await asyncio.Event().wait()


def _input() -> LanPrintInput:
    return LanPrintInput(
        printer="",
        uploaded=LanUploaded(remote_name=f"{NAME}.3mf", md5="0" * 32, bytes=1, filament_count=1),
        plate_id=1,
        subtask_name=NAME,
        plan=FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=tray_spool_id(0, 1))]),
    )


async def _start(pg_pool: PgPool, lan: Scripted) -> LanStarted:
    printers = PrinterStore(pg_pool, KEK)
    row = printers.create(PrinterCreate(serial=SERIAL, name="H2C"))
    printers.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    deps = LanDeps(
        printers=printers,
        settings_store=cast(SettingsStore, None),
        connect=lambda *_: cast(LanPrinter, lan),
        start_wait=0.5 + len(lan.after),
    )
    started: LanStarted = await ActivityEnvironment().run(
        LanActivities(deps).lan_print, _input().model_copy(update={"printer": row.id})
    )
    return started


async def test_from_finish_the_start_is_the_move_out_of_it(pg_pool: PgPool) -> None:
    """The printer names the job over the last job's ``FINISH`` (and its layer), then
    moves to ``PREPARE``: the start is the move, and a later read of the stale report
    is not this print's end."""
    stale = state(gcode_state="FINISH", **{**LAST, "subtask_name": NAME})
    lan = Scripted(
        state(gcode_state="FINISH", **LAST),
        [stale, state(gcode_state="PREPARE", subtask_name=NAME, layer_num=0)],
    )
    started = await _start(pg_pool, lan)
    assert started.started_at == T0 + timedelta(seconds=2)
    progress = from_lan_report(
        (stale, T0 + timedelta(seconds=1)), started, printer_name=None, now=T0
    )
    assert progress.stage == "unknown" and not progress.settled


async def test_the_name_over_finish_alone_is_no_start(pg_pool: PgPool) -> None:
    lan = Scripted(
        state(gcode_state="FINISH", **LAST),
        [state(gcode_state="FINISH", **{**LAST, "subtask_name": NAME})],
    )
    with pytest.raises(Exception) as raised:
        await _start(pg_pool, lan)
    assert isinstance(raised.value, ApplicationError) and raised.value.type == LAN_UNSURE


async def test_from_failed_the_last_jobs_error_is_no_failure(pg_pool: PgPool) -> None:
    """A printer left in ``FAILED`` may keep the last job's ``print_error`` over the new
    job's name, even while it runs it."""
    error = {"print_error": 0x0500C010}
    lan = Scripted(
        state(gcode_state="FAILED", **LAST, **error),
        [
            state(gcode_state="FAILED", **{**LAST, "subtask_name": NAME}, **error),
            state(gcode_state="RUNNING", subtask_name=NAME, **error),
        ],
    )
    started = await _start(pg_pool, lan)
    assert started.started_at == T0 + timedelta(seconds=2)


async def test_from_failed_a_new_error_is_this_starts(pg_pool: PgPool) -> None:
    lan = Scripted(
        state(gcode_state="FAILED", **LAST, print_error=0x0500C010),
        [state(gcode_state="FAILED", subtask_name=NAME, print_error=0x07008011)],
    )
    with pytest.raises(ApplicationError, match="0700_8011"):
        await _start(pg_pool, lan)


async def test_a_retry_that_finds_its_name_over_finish_waits_for_the_move(
    pg_pool: PgPool,
) -> None:
    """An earlier attempt's command named the job: no second command, and the move out of
    ``FINISH`` is the start."""
    lan = Scripted(
        state(gcode_state="FINISH", **{**LAST, "subtask_name": NAME}),
        [state(gcode_state="RUNNING", subtask_name=NAME, layer_num=0)],
    )
    started = await _start(pg_pool, lan)
    assert lan.commands == [None]
    assert started.started_at == T0 + timedelta(seconds=1)


async def test_a_retry_whose_print_had_ended_is_that_start(pg_pool: PgPool) -> None:
    """Still ``FINISH`` under its name once the wait is over: it printed, and ended."""
    lan = Scripted(state(gcode_state="FINISH", **{**LAST, "subtask_name": NAME}), [])
    started = await _start(pg_pool, lan)
    assert lan.commands == [None] and started.started_at == T0
