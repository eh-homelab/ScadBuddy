"""A LAN print's progress from its printer's report (#3823, plan 2026-10-16 Task 9,
Review Focus 5), and the run result that names it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from scadbuddy.bambuddy.client import BambuddyConfig
from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import PlannedRun, PrintRunResult, finish_run
from scadbuddy.bambuddy.progress import PrintProgress
from scadbuddy.bambuddy.runs import PrintRun, RecentRuns, given_run, newest_failure
from scadbuddy.core.events import PrintEvent, PrintRunEvent
from scadbuddy.printers.lan_models import LanQueued, LanStarted, LanStopped, busy_with
from scadbuddy.printers.progress import (
    AWAITING_CLEAR,
    IDLE_SINCE,
    MOVED_ON,
    NOT_HEARD,
    PAUSED,
    QUEUED_BUSY,
    QUEUED_IDLE,
    QUEUED_PLATE,
    WAITING,
    LanProgress,
    from_lan_report,
    idle_now,
    lan_ended,
    lan_last_print,
    printing_now,
    shows,
)
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
    LAN print; `given_run` reads no run."""
    lan = LanProgress(given_run(_run(LAN_RESULT)), _Rows(), _Reports(report(gcode_state="FINISH")))  # type: ignore[arg-type]
    progress = await lan.read("o" * 32)
    assert progress is not None and progress.stage == "done"


async def test_a_follows_two_reads_share_one_query() -> None:
    """The follow's newest-failure and LAN reads in one tick share one query of the runs
    (`RecentRuns`); one past its ``ttl`` reads again."""
    store = _CountingRuns(_run(LAN_RESULT))
    clock = [0.0]
    recent = RecentRuns(store, ttl=2.0, clock=lambda: clock[0])  # type: ignore[arg-type]
    lan = LanProgress(recent.latest, _Rows(), _Reports(report()))  # type: ignore[arg-type]
    assert await lan.read("o" * 32) is not None
    assert await newest_failure(store, "o" * 32, latest=recent.latest) is None
    assert store.calls == 1
    clock[0] = 3.0
    await lan.read("o" * 32)
    assert store.calls == 2


async def test_a_settled_run_is_read_again_at_once() -> None:
    """#4541: the subject's ``print.run`` event drops its kept run, so a follow's tick
    just after a retry does not read the run before it for up to ``ttl``."""
    store = _CountingRuns(_run(LAN_RESULT))
    recent = RecentRuns(store, ttl=2.0, clock=lambda: 0.0)  # type: ignore[arg-type]
    first = await recent.latest("o" * 32)
    store.run = _run(None)
    recent.heard(PrintRunEvent(output_id="o" * 32, slug="s", run_id="r2"))
    assert await recent.latest("o" * 32) is store.run is not first
    assert store.calls == 2


async def test_another_subjects_event_keeps_the_run() -> None:
    store = _CountingRuns(_run(LAN_RESULT))
    recent = RecentRuns(store, ttl=2.0, clock=lambda: 0.0)  # type: ignore[arg-type]
    await recent.latest("o" * 32)
    recent.heard(PrintRunEvent(output_id="p" * 32, slug="s", run_id="r2"))
    recent.heard(PrintEvent(kind="print.progress", output_id="o" * 32, slug="s"))
    await recent.latest("o" * 32)
    assert store.calls == 1


async def test_a_run_read_across_its_event_is_not_kept() -> None:
    """An event heard while the read is in flight: what the read answers may be the run
    before it, so the next read asks again."""
    store = _CountingRuns(_run(LAN_RESULT))
    recent = RecentRuns(store, ttl=2.0, clock=lambda: 0.0)  # type: ignore[arg-type]
    store.during = lambda: recent.heard(PrintRunEvent(output_id="o" * 32, slug="s", run_id="r2"))
    await recent.latest("o" * 32)
    store.during = None
    await recent.latest("o" * 32)
    assert store.calls == 2


async def test_a_run_read_across_its_event_is_not_kept_however_many_subjects_were_heard() -> None:
    """#4566 review: once ``capacity`` subjects had been heard, `heard` cleared every
    subject's count, so a read that spanned its subject's event could find the count it
    started from and keep the run before it."""
    store = _CountingRuns(_run(LAN_RESULT))
    recent = RecentRuns(store, ttl=2.0, clock=lambda: 0.0, capacity=1)  # type: ignore[arg-type]
    recent.heard(PrintRunEvent(output_id="o" * 32, slug="s", run_id="r1"))

    def events() -> None:
        recent.heard(PrintRunEvent(output_id="p" * 32, slug="s", run_id="r2"))
        recent.heard(PrintRunEvent(output_id="o" * 32, slug="s", run_id="r3"))

    store.during = events
    await recent.latest("o" * 32)
    store.during = None
    await recent.latest("o" * 32)
    assert store.calls == 2


class _CountingRuns(_Runs):
    def __init__(self, run: PrintRun | None) -> None:
        super().__init__(run)
        self.calls = 0
        self.during: Callable[[], None] | None = None

    async def latest_for_output(self, output_id: str) -> PrintRun | None:
        self.calls += 1
        if self.during is not None:
            self.during()
        return self.run


async def test_a_lan_run_is_read_from_its_printers_report() -> None:
    result = PrintRunResult(library_file_id=41, copies=1, path="native", lan=[OURS])
    progress = await _lan(_run(result), report(gcode_state="FINISH")).read("o" * 32)
    assert progress is not None and (progress.route, progress.stage) == ("lan", "done")
    # The run it reads (#4644, #4543): what its last print and rack picks are keyed by.
    assert progress.run_id == "r"


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


def test_a_lan_run_queued_on_a_busy_printer_reports_no_start_yet() -> None:
    """#6871: a run that started nothing is reported over the LAN, with its plan's
    printer and warnings, and no Bambuddy link."""
    planned = PlannedRun(
        library_file_id=41,
        printer_id=1,
        nozzle_size="0.4",
        copies=2,
        options=PrintOptions(),
        plates=[],
        path="native",
        lan_start=True,
    )
    config = BambuddyConfig(base_url="http://bambuddy.test", api_key="k")
    result = finish_run(config, planned, [])
    assert (result.path, result.lan, result.bambuddy_url) == ("native", [], None)
    assert (result.library_file_id, result.printer_id, result.copies) == (41, 1, 2)


def test_a_lan_starts_warnings_are_the_runs() -> None:
    """#4545: the rack pick's advisories reach the run's warnings, each fact once."""
    from scadbuddy.bambuddy.filaments import FilamentWarning

    planned = PlannedRun(
        library_file_id=41,
        printer_id=1,
        nozzle_size="0.4",
        copies=1,
        options=PrintOptions(),
        plates=[],
        path="native",
    )
    left = FilamentWarning(kind="rack-left-to-bambuddy", message="Rack pick left to the printer.")
    started = OURS.model_copy(update={"warnings": [left]})
    outcomes = [
        QueueOutcome(sliced_library_file_id=77, printer_id=1, lan=started),
        QueueOutcome(sliced_library_file_id=78, printer_id=1, lan=started),
    ]
    assert finish_run(None, planned, outcomes).warnings == [left]


SECOND = LanStarted(subtask_name="scadbuddy-abcd1234-p1-c2", started_at=at(hours=2), plate_id=1)


@pytest.mark.parametrize(
    ("data", "stage", "message"),
    [
        ({"gcode_state": "FINISH"}, "done", None),
        ({"gcode_state": "FAILED", "print_error": 0x0500C010}, "failed", "0500_C010"),
        ({"gcode_state": "FAILED"}, "failed", "stopped on the printer"),
        # Another print's end: this one's is not known, and the printer is free.
        ({"gcode_state": "FINISH", "subtask_name": "theirs"}, "unknown", MOVED_ON),
        ({"gcode_state": "IDLE"}, "unknown", IDLE_SINCE),
    ],
)
def test_a_start_ends_when_the_printer_stops_printing(
    data: dict[str, Any], stage: str, message: str | None
) -> None:
    """#4541: the next copy of a run waits for this."""
    ended = lan_ended(report(**data), OURS, seen=True)
    assert ended is not None and ended.ended == stage
    assert (ended.ended_message is None) == (message is None)
    assert message is None or message in (ended.ended_message or "")
    assert ended.subtask_name == OURS.subtask_name


@pytest.mark.parametrize(
    "value",
    [
        None,
        report(gcode_state="RUNNING"),
        report(gcode_state="PAUSE"),
        # Another print started since, still running: the printer is not free.
        report(gcode_state="RUNNING", subtask_name="theirs"),
        # The report the start was seen in, and one from before it.
        report(seconds=0, gcode_state="FINISH"),
        report(seconds=-60, gcode_state="FINISH"),
    ],
    ids=["no report", "running", "paused", "another running", "at the start", "before"],
)
def test_a_start_has_not_ended_while_the_printer_prints(
    value: tuple[dict[str, Any], datetime] | None,
) -> None:
    assert lan_ended(value, OURS, seen=True) is None


@pytest.mark.parametrize(
    "data",
    [
        {"gcode_state": "FINISH", "subtask_name": "theirs"},
        {"gcode_state": "IDLE", "subtask_name": "theirs"},
    ],
    ids=["another's end", "idle under another name"],
)
def test_another_name_is_no_end_until_a_report_named_this_print(data: dict[str, Any]) -> None:
    """Review of #4940: the report's time is the watch's clock, not the print worker's,
    and the watch may not have merged the start yet, so a report from before the start
    can look newer. Read as an end, it would start the next copy over this one."""
    assert lan_ended(report(**data), OURS, seen=False) is None
    ended = lan_ended(report(**data), OURS, seen=True)
    assert ended is not None and ended.ended == "unknown"


def test_its_own_end_needs_no_earlier_report() -> None:
    """A report that names this print is itself the proof."""
    ended = lan_ended(report(gcode_state="FINISH"), OURS, seen=False)
    assert ended is not None and ended.ended == "done"
    assert shows(report(), OURS) and not shows(report(subtask_name="theirs"), OURS)
    assert not shows(None, OURS)


async def test_a_sequenced_run_reads_each_start_and_the_ones_to_come() -> None:
    """#4541: the first copy as the run recorded its end, the second from the report, and
    the third still queued; nothing is settled until the last has printed."""
    done = OURS.model_copy(update={"plate_id": 1, "ended": "done"})
    result = PrintRunResult(
        library_file_id=41, copies=3, path="native", lan=[done, SECOND], lan_waiting=[1]
    )
    value = (
        {"subtask_name": SECOND.subtask_name, "gcode_state": "RUNNING"},
        at(hours=2, seconds=10),
    )
    progress = await _lan(_run(result), value).read("o" * 32)
    assert progress is not None and progress.route == "lan"
    assert (progress.stage, progress.settled) == ("running", False)
    assert (progress.copies, progress.copies_completed, progress.copies_in_progress) == (3, 1, 1)
    assert [copy.stage for copy in progress.copies_detail] == ["done", "running", "queued"]
    assert progress.copies_detail[2].waiting_reason == WAITING
    assert [copy.plate_id for copy in progress.copies_detail] == [1, 1, 1]


async def test_a_sequenced_run_settles_with_its_last_start() -> None:
    done = OURS.model_copy(update={"plate_id": 1, "ended": "done"})
    result = PrintRunResult(library_file_id=41, copies=2, path="native", lan=[done, SECOND])
    value = ({"subtask_name": SECOND.subtask_name, "gcode_state": "FINISH"}, at(hours=3))
    progress = await _lan(_run(result), value).read("o" * 32)
    assert progress is not None
    assert (progress.stage, progress.settled, progress.copies_completed) == ("done", True, 2)
    assert progress.run_id == "r"


@pytest.mark.parametrize(
    ("stopped", "stage"),
    [
        (LanStopped(stage="failed", message="Plate 2 was not started: gone."), "failed"),
        (LanStopped(stage="cancelled", message="The print was cancelled."), "cancelled"),
    ],
)
async def test_a_stopped_run_settles_the_starts_it_never_made(
    stopped: LanStopped, stage: str
) -> None:
    result = PrintRunResult(
        library_file_id=41,
        copies=1,
        path="native",
        lan=[OURS.model_copy(update={"plate_id": 1})],
        lan_waiting=[2],
        lan_stopped=stopped,
    )
    progress = await _lan(_run(result), report(gcode_state="FINISH")).read("o" * 32)
    assert progress is not None
    assert (progress.stage, progress.settled) == (stage, True)
    assert progress.copies_detail[1].message == stopped.message
    assert progress.copies_detail[1].plate_id == 2
    assert progress.error_message == (stopped.message if stage == "failed" else None)


async def test_a_sequenced_run_names_its_controls() -> None:
    """#4942: a run with prints still to start says so, with its id, so the panel and the
    agent can stop the rest; a run that stopped, or has none left, offers nothing."""
    done = OURS.model_copy(update={"plate_id": 1, "ended": "done"})
    result = PrintRunResult(
        library_file_id=41, copies=3, path="native", lan=[done, SECOND], lan_waiting=[1]
    )
    value = (
        {"subtask_name": SECOND.subtask_name, "gcode_state": "RUNNING"},
        at(hours=2, seconds=10),
    )
    progress = await _lan(_run(result), value).read("o" * 32)
    assert progress is not None and progress.lan_run is not None
    assert progress.lan_run.model_dump() == {
        "run_id": "r",
        "waiting": 1,
        "awaiting_clear": False,
        "queued": False,
        "queued_for_print": False,
        "printer_idle": False,
    }
    stopped = result.model_copy(
        update={"lan_stopped": LanStopped(stage="cancelled", message="Stopped.")}
    )
    progress = await _lan(_run(stopped), value).read("o" * 32)
    assert progress is not None and progress.lan_run is None
    single = await _lan(_run(LAN_RESULT), report()).read("o" * 32)
    assert single is not None and single.lan_run is None


async def test_a_run_waiting_for_a_cleared_plate_says_so() -> None:
    """#4939: the next print waits for someone to say the plate is clear, not for the
    printer, and the run's controls say it does."""
    done = OURS.model_copy(update={"plate_id": 1, "ended": "done"})
    result = PrintRunResult(
        library_file_id=41,
        copies=3,
        path="native",
        lan=[done],
        lan_waiting=[1, 1],
        lan_awaiting_clear=True,
    )
    progress = await _lan(_run(result), report(gcode_state="FINISH")).read("o" * 32)
    assert progress is not None and progress.lan_run is not None
    assert progress.lan_run.awaiting_clear and progress.lan_run.waiting == 2
    assert progress.settled is False
    assert [copy.waiting_reason for copy in progress.copies_detail[1:]] == [
        AWAITING_CLEAR.format(printer="H2C"),
        WAITING,
    ]


async def test_a_run_queued_on_a_busy_printer_reads_as_queued_with_its_controls() -> None:
    """#6871: a run that sent nothing while the printer was busy is ``queued``, says what
    it waits for, and names its controls, so the dialog and the Prints page show it and
    can stop it or say the plate is clear."""
    theirs = LanStarted(subtask_name="theirs", started_at=at())
    result = PrintRunResult(
        library_file_id=41,
        copies=1,
        path="native",
        lan_waiting=[1],
        lan_queued=LanQueued(state="RUNNING", job=theirs, place=2),
    )
    value = ({"subtask_name": "theirs", "gcode_state": "RUNNING"}, at(seconds=10))
    progress = await _lan(_run(result), value).read("o" * 32)
    assert progress is not None and progress.route == "lan"
    assert (progress.stage, progress.settled, progress.run_id) == ("queued", False, "r")
    assert [copy.waiting_reason for copy in progress.copies_detail] == [
        QUEUED_BUSY.format(printer="H2C")
    ]
    assert progress.lan_run is not None
    assert progress.lan_run.model_dump() == {
        "run_id": "r",
        "waiting": 1,
        "awaiting_clear": False,
        "queued": True,
        "queued_for_print": True,
        "printer_idle": False,
    }
    # Queued only for an ended print's plate (#6871): no print to wait for.
    plate = result.model_copy(update={"lan_queued": LanQueued(state="FINISH", job=None, place=1)})
    progress = await _lan(_run(plate), value).read("o" * 32)
    assert progress is not None and progress.lan_run is not None
    assert progress.lan_run.queued and not progress.lan_run.queued_for_print
    assert [copy.waiting_reason for copy in progress.copies_detail] == [
        QUEUED_PLATE.format(printer="H2C")
    ]
    # The print it was queued behind has ended (review of #6939): read live, the run now
    # waits for the plate, before its sequence records that.
    # The printer reads idle (a restart): the plate may still hold the print before.
    idle = ({"gcode_state": "IDLE"}, at(seconds=20))
    progress = await _lan(_run(result), idle).read("o" * 32)
    assert progress is not None and progress.lan_run is not None
    assert progress.lan_run.printer_idle and not progress.lan_run.queued_for_print
    assert [copy.waiting_reason for copy in progress.copies_detail] == [
        QUEUED_IDLE.format(printer="H2C")
    ]
    ended = ({"subtask_name": "theirs", "gcode_state": "FINISH"}, at(seconds=20))
    progress = await _lan(_run(result), ended).read("o" * 32)
    assert progress is not None and progress.lan_run is not None
    assert progress.lan_run.queued and not progress.lan_run.queued_for_print
    assert [copy.waiting_reason for copy in progress.copies_detail] == [
        QUEUED_PLATE.format(printer="H2C")
    ]
    clear = result.model_copy(update={"lan_awaiting_clear": True})
    progress = await _lan(_run(clear), value).read("o" * 32)
    assert progress is not None and progress.lan_run is not None
    assert progress.lan_run.awaiting_clear
    assert [copy.waiting_reason for copy in progress.copies_detail] == [
        AWAITING_CLEAR.format(printer="H2C")
    ]


class _LastPrints:
    def __init__(self) -> None:
        self.moved: list[tuple[str, str, str, bool]] = []

    def record_lan_progress(
        self, output_id: str, run_id: str, *, stage: str, settled: bool
    ) -> None:
        self.moved.append((output_id, run_id, stage, settled))


@pytest.mark.parametrize(
    ("progress", "moved"),
    [
        (
            PrintProgress(route="lan", stage="running", bambuddy_url="", run_id="r"),
            [("o" * 32, "r", "running", False)],
        ),
        (
            PrintProgress(route="lan", stage="done", settled=True, bambuddy_url="", run_id="r"),
            [("o" * 32, "r", "done", True)],
        ),
        (PrintProgress(route="lan", stage="running", bambuddy_url=""), []),
        (PrintProgress(route="slice_queue", stage="running", bambuddy_url="x"), []),
    ],
    ids=["running", "settled", "no run", "bambuddy"],
)
async def test_the_follow_moves_a_lan_prints_last_print(
    progress: PrintProgress, moved: list[tuple[str, str, str, bool]]
) -> None:
    """#4644: the output's last print says how its LAN print goes, as the follow reads it."""
    prints = _LastPrints()
    await lan_last_print(prints)("o" * 32, progress)
    assert prints.moved == moved


async def test_a_library_files_lan_print_has_no_last_print_row() -> None:
    prints = _LastPrints()
    progress = PrintProgress(route="lan", stage="done", settled=True, bambuddy_url="", run_id="r")
    await lan_last_print(prints)("library:7", progress)
    assert prints.moved == []


#: The report of a print a queued run waits out (#6871) that names no ``subtask_name``.
UNNAMED_REPORT = {"gcode_state": "RUNNING"}


@pytest.mark.parametrize(
    ("data", "stage"),
    [
        ({"gcode_state": "FINISH", "subtask_name": ""}, "done"),
        ({"gcode_state": "FAILED", "subtask_name": ""}, "failed"),
        # Ended, with no name to tell whose end it is.
        ({"gcode_state": "FINISH"}, "unknown"),
        ({"gcode_state": "IDLE"}, "unknown"),
        ({"gcode_state": "FINISH", "subtask_name": "theirs"}, "unknown"),
    ],
)
def test_a_busy_print_with_no_name_ends_when_the_printer_leaves_busy(
    data: dict[str, Any], stage: str
) -> None:
    """#6871 review: `busy_with` names no job when the report names none, so no report
    can name it. Any end counts: the printer no longer in a busy state."""
    queued = busy_with(UNNAMED_REPORT, STARTED_AT, plate_clear=False)
    assert queued is not None and queued.job is not None
    ended = lan_ended((data, at(seconds=10)), queued.job, seen=False)
    assert ended is not None and ended.ended == stage


@pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", "SLICING"])
def test_a_busy_print_with_no_name_has_not_ended_while_busy(state: str) -> None:
    queued = busy_with(UNNAMED_REPORT, STARTED_AT, plate_clear=False)
    assert queued is not None and queued.job is not None
    assert lan_ended(({"gcode_state": state}, at(seconds=10)), queued.job, seen=False) is None
    # Nor from a report no newer than the check.
    assert lan_ended(({"gcode_state": "FINISH"}, STARTED_AT), queued.job, seen=False) is None


@pytest.mark.parametrize(
    ("state", "printing"),
    [
        ("RUNNING", True),
        ("PAUSE", True),
        ("FINISH", False),
        ("FAILED", False),
        ("IDLE", False),
        ("SOMETHING_NEW", None),
        ("", None),
        (None, None),
    ],
)
def test_only_known_states_say_whether_the_printer_prints(
    state: object, printing: bool | None
) -> None:
    """#6871 (review of #6948): a word the code does not know reads as no state, so a
    queued run's text falls back to the job it recorded."""
    assert printing_now(state) is printing


@pytest.mark.parametrize(
    ("state", "idle"), [("IDLE", True), ("FINISH", False), ("RUNNING", False), (None, False)]
)
def test_only_idle_reads_as_idle(state: object, idle: bool) -> None:
    assert idle_now(state) is idle
