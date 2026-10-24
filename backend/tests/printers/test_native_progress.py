"""A LAN print's progress from its printer's report (#3823, plan 2026-10-16 Task 9,
Review Focus 5), and the run result that names it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import PlannedRun, PrintRunResult, finish_run
from scadbuddy.bambuddy.runs import PrintRun, RecentRuns, newest_failure
from scadbuddy.printers.lan_models import LanStarted
from scadbuddy.printers.progress import MOVED_ON, NOT_HEARD, PAUSED, LanProgress, from_lan_report
from scadbuddy.printers.status import print_error_text

STARTED_AT = datetime(2026, 10, 24, 12, tzinfo=UTC)
OURS = LanStarted(subtask_name="scadbuddy-abcd1234-p1", started_at=STARTED_AT)


def report(seconds: float = 10, **fields: Any) -> tuple[dict[str, Any], datetime]:
    data = {"subtask_name": OURS.subtask_name, "gcode_state": "RUNNING", **fields}
    return data, STARTED_AT + timedelta(seconds=seconds)


def at(**delta: float) -> datetime:
    return STARTED_AT + timedelta(**delta)


@pytest.mark.parametrize(
    ("state", "stage", "settled"),
    [
        ("PREPARE", "running", False),
        ("RUNNING", "running", False),
        ("PAUSE", "running", False),
        ("FINISH", "done", True),
        ("FAILED", "failed", True),
        ("IDLE", "unknown", False),
    ],
)
def test_the_stage_follows_gcode_state(state: str, stage: str, settled: bool) -> None:
    progress = from_lan_report(
        report(gcode_state=state), OURS, printer_name="H2C", now=at(seconds=20)
    )
    assert (progress.route, progress.stage, progress.settled) == ("lan", stage, settled)
    assert progress.copies_completed == (1 if state == "FINISH" else 0)
    assert progress.copies_detail[0].printer_name == "H2C"
    assert progress.bambuddy_url == ""


def test_a_pause_says_so() -> None:
    progress = from_lan_report(report(gcode_state="PAUSE"), OURS, printer_name=None, now=at())
    assert progress.copies_detail[0].waiting_reason == PAUSED


def test_a_failure_carries_the_print_error_code() -> None:
    progress = from_lan_report(
        report(gcode_state="FAILED", print_error=0x0500C010), OURS, printer_name=None, now=at()
    )
    assert progress.stage == "failed" and progress.copies_failed == 1
    assert progress.error_message is not None and "0500_C010" in progress.error_message
    assert print_error_text(0x0500C010) == "0500_C010"


def test_native_follow_settles_when_report_stops() -> None:
    """Review Focus 5: the last report stands until it is `MAX_AGE` old, then the print
    settles as unknown."""
    last = report(seconds=60, gcode_state="RUNNING", mc_percent=40)
    max_age = timedelta(hours=24)
    moving = from_lan_report(last, OURS, printer_name=None, now=at(hours=23), max_age=max_age)
    assert (moving.stage, moving.settled) == ("running", False)
    gone = from_lan_report(last, OURS, printer_name=None, now=at(hours=25), max_age=max_age)
    assert (gone.stage, gone.settled) == ("unknown", True)
    assert gone.copies_detail[0].message == NOT_HEARD


def test_a_report_from_before_the_start_is_not_the_print() -> None:
    old = ({"subtask_name": "someone-else", "gcode_state": "FINISH"}, at(seconds=-30))
    progress = from_lan_report(old, OURS, printer_name=None, now=at(seconds=5))
    assert (progress.stage, progress.settled) == ("unknown", False)


def test_another_print_since_settles_it_as_unknown() -> None:
    other = ({"subtask_name": "someone-else", "gcode_state": "RUNNING"}, at(minutes=30))
    progress = from_lan_report(other, OURS, printer_name=None, now=at(minutes=31))
    assert (progress.stage, progress.settled) == ("unknown", True)
    assert progress.copies_detail[0].message == MOVED_ON


def test_no_report_at_all_waits_then_settles() -> None:
    assert not from_lan_report(None, OURS, printer_name=None, now=at(hours=1)).settled
    assert from_lan_report(None, OURS, printer_name=None, now=at(hours=25)).settled


class _Runs:
    def __init__(self, run: PrintRun | None) -> None:
        self.run = run

    async def latest_for_output(self, output_id: str) -> PrintRun | None:
        return self.run


class _Rows:
    def get(self, printer_id: str) -> object:
        return type("Row", (), {"name": "H2C"})()


class _Reports:
    def __init__(self, value: tuple[dict[str, Any], datetime] | None) -> None:
        self.value = value
        self.asked: list[str] = []

    def get(self, printer_id: str) -> tuple[dict[str, Any], datetime] | None:
        self.asked.append(printer_id)
        return self.value


def _run(result: PrintRunResult | None, status: str = "succeeded") -> PrintRun:
    return PrintRun(
        id="r",
        subject="output:" + "o" * 32,
        status=status,  # type: ignore[arg-type]
        created_at=STARTED_AT,
        result=result,
        printer="p1",
    )


def _lan(run: PrintRun | None, value: tuple[dict[str, Any], datetime] | None) -> LanProgress:
    return LanProgress(
        _Runs(run).latest_for_output,
        _Rows(),  # type: ignore[arg-type]
        _Reports(value),  # type: ignore[arg-type]
        now=lambda: at(minutes=1),
    )


LAN_RESULT = PrintRunResult(library_file_id=41, copies=1, path="native", lan=[OURS])


class _Unreadable:
    def get(self, printer_id: str) -> object:
        raise psycopg.OperationalError("the database is gone")


async def test_an_unreadable_registry_reads_as_no_lan_print() -> None:
    """#4540 review: as the runs' read, a registry read that fails is no LAN print."""
    reports = LanProgress(
        _Runs(_run(LAN_RESULT)).latest_for_output,
        _Rows(),  # type: ignore[arg-type]
        _Unreadable(),  # type: ignore[arg-type]
    )
    assert await reports.read("o" * 32) is None
    rows = LanProgress(
        _Runs(_run(LAN_RESULT)).latest_for_output,
        _Unreadable(),  # type: ignore[arg-type]
        _Reports(report()),  # type: ignore[arg-type]
    )
    assert await rows.read("o" * 32) is None


async def test_unreadable_runs_read_as_no_lan_print() -> None:
    async def latest(run_subject: str) -> PrintRun | None:
        raise psycopg.OperationalError("the database is gone")

    progress = LanProgress(latest, _Rows(), _Reports(report()))  # type: ignore[arg-type]
    assert await progress.read("o" * 32) is None


async def test_a_run_read_already_is_not_read_again() -> None:
    """#4540 review: a progress route reads the newest run once, for its failure and its
    LAN print; `given` reads no run."""
    calls = 0

    async def latest(run_subject: str) -> PrintRun | None:
        nonlocal calls
        calls += 1
        return None

    lan = LanProgress(latest, _Rows(), _Reports(report(gcode_state="FINISH")))  # type: ignore[arg-type]
    progress = await lan.given(_run(LAN_RESULT)).read("o" * 32)
    assert progress is not None and progress.stage == "done" and calls == 0


async def test_a_follows_two_reads_share_one_query() -> None:
    """The follow's newest-failure and LAN reads in one tick share one query of the runs
    (`RecentRuns`); one past its ``ttl`` reads again."""
    store = _CountingRuns(_run(LAN_RESULT))
    clock = [0.0]
    recent = RecentRuns(store, ttl=2.0, clock=lambda: clock[0])  # type: ignore[arg-type]
    lan = LanProgress(recent.latest, _Rows(), _Reports(report()))  # type: ignore[arg-type]
    assert await lan.read("o" * 32) is not None
    assert await newest_failure(store, "o" * 32, latest=recent.latest) is None  # type: ignore[arg-type]
    assert store.calls == 1
    clock[0] = 3.0
    await lan.read("o" * 32)
    assert store.calls == 2


class _CountingRuns(_Runs):
    def __init__(self, run: PrintRun | None) -> None:
        super().__init__(run)
        self.calls = 0

    async def latest_for_output(self, output_id: str) -> PrintRun | None:
        self.calls += 1
        return self.run


async def test_a_lan_run_is_read_from_its_printers_report() -> None:
    result = PrintRunResult(library_file_id=41, copies=1, path="native", lan=[OURS])
    progress = await _lan(_run(result), report(gcode_state="FINISH")).read("o" * 32)
    assert progress is not None and (progress.route, progress.stage) == ("lan", "done")


@pytest.mark.parametrize(
    "run",
    [
        None,
        _run(PrintRunResult(library_file_id=41, copies=1, queue_item_ids=[5])),
        _run(None, status="running"),
    ],
    ids=["no run", "a Bambuddy run", "still running"],
)
async def test_any_other_run_reads_as_bambuddys(run: PrintRun | None) -> None:
    assert await _lan(run, report()).read("o" * 32) is None


def test_a_lan_run_reports_no_bambuddy_link() -> None:
    planned = PlannedRun(
        library_file_id=41,
        printer_id=1,
        nozzle_size="0.4",
        copies=1,
        options=PrintOptions(),
        plates=[],
        path="native",
    )
    outcome = QueueOutcome(sliced_library_file_id=77, printer_id=1, lan=OURS)
    result = finish_run(None, planned, [outcome])
    assert (result.path, result.lan, result.bambuddy_url) == ("native", [OURS], None)
    assert result.queue_item_ids == []


@pytest.mark.parametrize(
    ("plates", "copies", "refused"),
    [([1], None, False), ([1], 1, False), ([1, 2], None, True), ([1], 2, True)],
)
def test_a_lan_run_prints_one_plate_once(
    plates: list[int], copies: int | None, refused: bool
) -> None:
    from scadbuddy.bambuddy.filaments import FilamentPlan
    from scadbuddy.bambuddy.print_run import PrintRunRequest, RunRefusalError
    from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
    from scadbuddy.workflows.print_activities import _one_lan_print

    request = PrintRunRequest(
        filament_plan=FilamentPlan(slots=[]),
        choices=PrintChoices(nozzles=[NozzleChoice(size="0.4")]),
        copies=copies,
    )
    if refused:
        with pytest.raises(RunRefusalError):
            _one_lan_print(plates, request)
    else:
        _one_lan_print(plates, request)
