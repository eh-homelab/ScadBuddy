"""The ``printer_watch`` activity on its own (#4186 review I3, M4, M11): when it may
say the printer was heard from, and how it ends on a worker's shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.printers import watch as watch_module
from scadbuddy.printers.lan import LanPrinter
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import (
    Advance,
    JobStart,
    PrintEnd,
    PrinterReportStore,
    advance_print,
)
from scadbuddy.printers.store import PrinterStore
from scadbuddy.printers.watch import SHOWN, WatchActivities, shown
from scadbuddy.rack.usage import LanUsage, PickedHotend, RackUsageStore
from scadbuddy.workflows.printer_watch import PrinterWatchInput
from tests.conftest import PgPool
from tests.printers.conftest import KEK
from tests.rack.helpers import serial

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"
HOTEND = serial(51)
LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


class Lan:
    """A connection that subscribes (or never does), yields ``deltas``, then stays
    quiet until cancelled."""

    def __init__(self, *, subscribes: bool, deltas: list[dict[str, Any]] | None = None) -> None:
        self.subscribes = subscribes
        self.deltas = deltas or []
        #: Each report yielded, so a test can name the one that ended a print.
        self.sent: list[LanReport] = []

    async def reports(self, connected: asyncio.Event | None = None) -> AsyncIterator[LanReport]:
        if not self.subscribes:  # a connect to an address that never answers
            await asyncio.Event().wait()
        if connected is not None:
            connected.set()
        for delta in self.deltas:
            report = LanReport(serial=SERIAL, received_at=datetime.now(UTC), print=delta)
            self.sent.append(report)
            yield report
        await asyncio.Event().wait()


@pytest.fixture
def linked(pg_pool: PgPool) -> tuple[PrinterStore, PrinterReportStore, str]:
    printers = PrinterStore(pg_pool, KEK)
    row = printers.create(PrinterCreate(serial=SERIAL, name="H2C"))
    printers.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    reports = PrinterReportStore(pg_pool)
    reports.put(row.id, {"gcode_state": "IDLE"}, LONG_AGO)
    return printers, reports, row.id


async def _run_for(env: ActivityEnvironment, watches: WatchActivities, pid: str, s: float) -> None:
    task = asyncio.ensure_future(env.run(watches.printer_watch, PrinterWatchInput(printer_id=pid)))
    await asyncio.sleep(s)
    env.cancel()
    with pytest.raises(BaseException):  # noqa: B017 - cancelled, however the env says it
        await task


async def test_no_touch_while_the_printer_never_answers(
    linked: tuple[PrinterStore, PrinterReportStore, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I3 scenario A: a connect that hangs is no evidence; ``seen_at`` stays old, so the
    card goes offline at `STALE_AFTER`."""
    printers, reports, pid = linked
    monkeypatch.setattr(watch_module, "HEARD_EVERY", 0.05)
    lan = Lan(subscribes=False)
    watches = WatchActivities(printers, reports, None, connect=lambda *a: cast(LanPrinter, lan))
    await _run_for(ActivityEnvironment(), watches, pid, 0.4)
    got = reports.get(pid)
    assert got is not None and got[1] == LONG_AGO


async def test_a_quiet_connection_is_touched(
    linked: tuple[PrinterStore, PrinterReportStore, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    printers, reports, pid = linked
    monkeypatch.setattr(watch_module, "HEARD_EVERY", 0.05)
    lan = Lan(subscribes=True)
    watches = WatchActivities(printers, reports, None, connect=lambda *a: cast(LanPrinter, lan))
    await _run_for(ActivityEnvironment(), watches, pid, 0.4)
    got = reports.get(pid)
    assert got is not None and datetime.now(UTC) - got[1] < timedelta(seconds=5)


async def test_the_watch_ends_on_worker_shutdown(
    linked: tuple[PrinterStore, PrinterReportStore, str],
) -> None:
    """M4: a print worker's stop does not wait out its graceful timeout per watch."""
    printers, reports, pid = linked
    lan = Lan(subscribes=True)
    watches = WatchActivities(printers, reports, None, connect=lambda *a: cast(LanPrinter, lan))
    env = ActivityEnvironment()
    task = asyncio.ensure_future(env.run(watches.printer_watch, PrinterWatchInput(printer_id=pid)))
    await asyncio.sleep(0.2)
    env.worker_shutdown()
    with pytest.raises(ApplicationError, match="shutting down"):
        async with asyncio.timeout(5):
            await task


def test_a_new_hms_error_or_time_left_is_announced() -> None:
    """M11."""
    assert {"hms", "mc_remaining_time"} <= set(SHOWN)
    assert shown({"hms": []}) != shown({"hms": [{"attr": 1, "code": 2}]})
    assert shown({"mc_remaining_time": 10}) != shown({"mc_remaining_time": 9})


async def test_a_touch_claims_only_what_keepalive_proves(
    linked: tuple[PrinterStore, PrinterReportStore, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4186 re-review N3: paho drops a link with no answer within `LINK_PROOF` (two
    keepalives) of its last inbound packet, so a touch stamps ``now - LINK_PROOF``, never
    ``now``: the card goes offline 90 s after the printer was last heard, as the rule says."""
    printers, reports, pid = linked
    monkeypatch.setattr(watch_module, "HEARD_EVERY", 0.05)
    monkeypatch.setattr(watch_module, "LINK_PROOF", 0.5)
    lan = Lan(subscribes=True)
    watches = WatchActivities(printers, reports, None, connect=lambda *a: cast(LanPrinter, lan))
    started = datetime.now(UTC)
    await _run_for(ActivityEnvironment(), watches, pid, 1.0)
    got = reports.get(pid)
    assert got is not None
    lag = datetime.now(UTC) - got[1]
    assert timedelta(seconds=0.45) <= lag < timedelta(seconds=1.5)
    assert got[1] >= started


def test_the_proof_window_is_two_keepalives() -> None:
    from scadbuddy.printers import lan

    assert watch_module.LINK_PROOF == 2 * lan.KEEPALIVE
    assert lan.KEEPALIVE <= 10


OLD_RUN, OLD_START = "run-old", "scadbuddy-run-old-p1"


async def test_the_watch_settles_a_start_its_printer_ends_whatever_run_follows(
    linked: tuple[PrinterStore, PrinterReportStore, str], pg_conninfo: str
) -> None:
    """#5138: a newer run of the output started since, so no follow reads the older
    run's last copy. The printer's own watch still settles that start's rack pick once
    its report ends it."""
    printers, reports, pid = linked
    rack = RackUsageStore(pg_conninfo)
    try:
        hotend = PickedHotend(group_id=0, position=2, serial=HOTEND)
        await rack.record_lan_picks(OLD_RUN, OLD_START, pid, [hotend])
        assert (await rack.usage([HOTEND]))[HOTEND].pending == 1
        lan = Lan(
            subscribes=True,
            deltas=[
                {"gcode_state": "RUNNING", "subtask_name": OLD_START},
                {"gcode_state": "FINISH", "subtask_name": OLD_START},
            ],
        )
        watches = WatchActivities(
            printers, reports, None, connect=lambda *a: cast(LanPrinter, lan), rack=rack
        )
        await _run_for(ActivityEnvironment(), watches, pid, 0.4)

        usage = (await rack.usage([HOTEND]))[HOTEND]
        assert (usage.prints, usage.pending) == (1, 0)
    finally:
        rack.close()


async def test_a_start_the_printer_moved_on_from_settles_untimed(
    linked: tuple[PrinterStore, PrinterReportStore, str], pg_conninfo: str
) -> None:
    """A report naming another print says the start ended, not when or how: its pick
    settles as one print with no time, through the watch's own write."""
    printers, reports, pid = linked
    rack = RackUsageStore(pg_conninfo)
    try:
        hotend = PickedHotend(group_id=0, position=2, serial=HOTEND)
        await rack.record_lan_picks(OLD_RUN, OLD_START, pid, [hotend])
        lan = Lan(
            subscribes=True,
            deltas=[
                {"gcode_state": "RUNNING", "subtask_name": OLD_START},
                {"gcode_state": "RUNNING", "subtask_name": "someone-elses-print"},
            ],
        )
        # No card field changes, so the report waits for `write_every`: none here.
        watches = WatchActivities(
            printers,
            reports,
            None,
            connect=lambda *a: cast(LanPrinter, lan),
            rack=rack,
            write_every=0,
        )
        await _run_for(ActivityEnvironment(), watches, pid, 0.4)

        usage = (await rack.usage([HOTEND]))[HOTEND]
        assert (usage.prints, usage.pending, usage.print_seconds) == (1, 0, 0)
        with psycopg.connect(pg_conninfo) as conn:
            row = conn.execute(
                "SELECT print_seconds FROM rack_nozzle_prints WHERE lan_subtask = %s",
                (OLD_START,),
            ).fetchone()
        assert row == (None,)
    finally:
        rack.close()


class Settles:
    """A rack that records each settle the watch asks for."""

    def __init__(self) -> None:
        self.ended: list[tuple[str, str, datetime, bool]] = []

    async def settle_lan_ended(
        self, printer: str, subtask_name: str, ended_at: datetime, *, timed: bool = True
    ) -> int:
        self.ended.append((printer, subtask_name, ended_at, timed))
        return 1


async def test_a_failed_print_settles_timed_at_the_report_that_says_so(
    linked: tuple[PrinterStore, PrinterReportStore, str],
) -> None:
    """#5261: a ``FAILED`` end is an end the printer timed, as ``FINISH`` is."""
    printers, reports, pid = linked
    lan = Lan(
        subscribes=True,
        deltas=[
            {"gcode_state": "RUNNING", "subtask_name": OLD_START},
            {"gcode_state": "FAILED", "subtask_name": OLD_START},
        ],
    )
    rack = Settles()
    watches = WatchActivities(
        printers,
        reports,
        None,
        connect=lambda *a: cast(LanPrinter, lan),
        rack=cast(LanUsage, rack),
    )
    await _run_for(ActivityEnvironment(), watches, pid, 0.4)

    assert rack.ended == [(pid, OLD_START, lan.sent[1].received_at, True)]


async def test_an_end_held_back_settles_when_the_quiet_watch_writes_it(
    linked: tuple[PrinterStore, PrinterReportStore, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5261: a report that changes no card field waits for `write_every`. When the
    printer then goes quiet, the `HEARD_EVERY` timeout writes it, and the end it shows
    settles at that report's time, not at the flush."""
    printers, reports, pid = linked
    monkeypatch.setattr(watch_module, "HEARD_EVERY", 0.05)
    lan = Lan(
        subscribes=True,
        deltas=[
            {"gcode_state": "RUNNING", "subtask_name": OLD_START},
            {"gcode_state": "RUNNING", "subtask_name": "someone-elses-print"},
        ],
    )
    rack = Settles()
    watches = WatchActivities(
        printers,
        reports,
        None,
        connect=lambda *a: cast(LanPrinter, lan),
        rack=cast(LanUsage, rack),
        write_every=3600,
    )
    await _run_for(ActivityEnvironment(), watches, pid, 0.4)

    assert rack.ended == [(pid, OLD_START, lan.sent[1].received_at, False)]
    got = reports.get(pid)
    assert got is not None and got[0]["subtask_name"] == "someone-elses-print"


RUNNING_X = {"gcode_state": "RUNNING", "subtask_name": "x"}
FINISH_X = {"gcode_state": "FINISH", "subtask_name": "x"}


@pytest.mark.parametrize(
    ("running", "new", "after", "ended"),
    [
        ("x", FINISH_X, None, PrintEnd("x", timed=True)),
        ("x", {"gcode_state": "FAILED", "subtask_name": "x"}, None, PrintEnd("x", timed=True)),
        ("x", {"gcode_state": "IDLE", "subtask_name": "x"}, None, PrintEnd("x", timed=False)),
        ("x", {"gcode_state": "RUNNING", "subtask_name": "y"}, "y", PrintEnd("x", timed=False)),
        ("x", {"gcode_state": "FINISH", "subtask_name": "y"}, None, PrintEnd("x", timed=False)),
        ("x", {"gcode_state": "PAUSE", "subtask_name": "x"}, "x", None),
        ("x", RUNNING_X, "x", None),
        (None, RUNNING_X, "x", None),
        # #4935: a new job's name while the printer still says the last one's FINISH.
        (None, {"gcode_state": "FINISH", "subtask_name": "y"}, None, None),
        (None, FINISH_X, None, None),
        # A delta before the reconnect's pushall answer keeps the running print.
        ("x", {"mc_percent": 3}, "x", None),
        ("x", {"gcode_state": "FINISH"}, "x", None),
        (None, {"subtask_name": "x"}, None, None),
    ],
    ids=[
        "finished",
        "failed",
        "idle",
        "moved on",
        "moved on under FINISH",
        "paused",
        "running",
        "a start",
        "a start under FINISH",
        "never seen running",
        "says too little",
        "names no print",
        "names no state",
    ],
)
def test_a_report_ends_only_the_print_running_before_it(
    running: str | None, new: dict[str, Any], after: str | None, ended: PrintEnd | None
) -> None:
    assert advance_print(running, new) == (after, ended)


def test_a_write_returns_the_end_it_shows_after_a_report_that_says_too_little(
    linked: tuple[PrinterStore, PrinterReportStore, str],
) -> None:
    """#5138 review: a reconnect's first delta may name no print; the row keeps the one
    running, so the next whole report still ends it."""
    _, reports, pid = linked
    assert reports.put(pid, RUNNING_X, datetime.now(UTC)) is None
    assert reports.put(pid, {"mc_percent": 3}, datetime.now(UTC)) is None
    assert reports.put(pid, FINISH_X, datetime.now(UTC)) == PrintEnd("x", timed=True)
    assert reports.put(pid, FINISH_X, datetime.now(UTC)) is None


def test_a_write_returns_the_start_it_shows_once_per_job(
    linked: tuple[PrinterStore, PrinterReportStore, str],
) -> None:
    """#4699 phase 3: one job is one start, whatever reports repeat it."""
    _, reports, pid = linked
    now = datetime.now(UTC)
    assert reports.advance(pid, RUNNING_X, now) == Advance(started="x")
    assert reports.advance(pid, {"mc_percent": 3}, now) == Advance()
    assert reports.advance(pid, RUNNING_X, now) == Advance()
    y = {"gcode_state": "PREPARE", "subtask_name": "y"}
    assert reports.advance(pid, y, now) == Advance(ended=PrintEnd("x", timed=False), started="y")


class Nudges:
    """A nudge that records each start the watch hands it, or fails."""

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.seen: list[tuple[str, str]] = []

    async def nudge(self, printer_id: str, start: JobStart) -> bool:
        self.seen.append((printer_id, start.subtask_name))
        if self.fails:
            raise RuntimeError("Temporal did not answer")
        return True


async def test_the_watch_nudges_the_print_watcher_once_per_job_start(
    linked: tuple[PrinterStore, PrinterReportStore, str],
) -> None:
    """#4699 phase 3: each job start, and only a start, is handed to the nudge."""
    printers, reports, pid = linked
    lan = Lan(
        subscribes=True,
        deltas=[
            RUNNING_X,
            {**RUNNING_X, "mc_percent": 5},
            FINISH_X,
            {"gcode_state": "RUNNING", "subtask_name": "y"},
        ],
    )
    nudges = Nudges()
    watches = WatchActivities(
        printers,
        reports,
        None,
        connect=lambda *a: cast(LanPrinter, lan),
        write_every=0,
        nudge=nudges.nudge,
    )
    await _run_for(ActivityEnvironment(), watches, pid, 0.4)

    assert nudges.seen == [(pid, "x"), (pid, "y")]


async def test_a_nudge_that_fails_never_ends_the_watch(
    linked: tuple[PrinterStore, PrinterReportStore, str],
) -> None:
    printers, reports, pid = linked
    lan = Lan(
        subscribes=True,
        deltas=[RUNNING_X, {"gcode_state": "RUNNING", "subtask_name": "y", "mc_percent": 9}],
    )
    nudges = Nudges(fails=True)
    watches = WatchActivities(
        printers,
        reports,
        None,
        connect=lambda *a: cast(LanPrinter, lan),
        write_every=0,
        nudge=nudges.nudge,
    )
    await _run_for(ActivityEnvironment(), watches, pid, 0.4)

    assert nudges.seen == [(pid, "x"), (pid, "y")]
    got = reports.get(pid)
    assert got is not None and got[0]["subtask_name"] == "y"
