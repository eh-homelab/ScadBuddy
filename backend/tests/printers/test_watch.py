"""The ``printer_watch`` activity on its own (#4186 review I3, M4, M11): when it may
say the printer was heard from, and how it ends on a worker's shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.printers import watch as watch_module
from scadbuddy.printers.lan import LanPrinter
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import PrintEnd, PrinterReportStore, ended_print
from scadbuddy.printers.store import PrinterStore
from scadbuddy.printers.watch import SHOWN, WatchActivities, shown
from scadbuddy.rack.usage import PickedHotend, RackUsageStore
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

    async def reports(self, connected: asyncio.Event | None = None) -> AsyncIterator[LanReport]:
        if not self.subscribes:  # a connect to an address that never answers
            await asyncio.Event().wait()
        if connected is not None:
            connected.set()
        for delta in self.deltas:
            yield LanReport(serial=SERIAL, received_at=datetime.now(UTC), print=delta)
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
        await rack.record_lan_picks(OLD_RUN, OLD_START, None, [hotend], pid)
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


RUNNING_X = {"gcode_state": "RUNNING", "subtask_name": "x"}


@pytest.mark.parametrize(
    ("old", "new", "ended"),
    [
        (RUNNING_X, {"gcode_state": "FINISH", "subtask_name": "x"}, PrintEnd("x", timed=True)),
        (RUNNING_X, {"gcode_state": "FAILED", "subtask_name": "x"}, PrintEnd("x", timed=True)),
        (RUNNING_X, {"gcode_state": "IDLE", "subtask_name": "x"}, PrintEnd("x", timed=False)),
        (RUNNING_X, {"gcode_state": "RUNNING", "subtask_name": "y"}, PrintEnd("x", timed=False)),
        (
            {"gcode_state": "PAUSE", "subtask_name": "x"},
            {"gcode_state": "FINISH", "subtask_name": "x"},
            PrintEnd("x", timed=True),
        ),
        (RUNNING_X, {"gcode_state": "PAUSE", "subtask_name": "x"}, None),
        (RUNNING_X, {"gcode_state": "RUNNING", "subtask_name": "x"}, None),
        # #4935: a new job's name while the printer still says the last one's FINISH.
        (
            {"gcode_state": "FINISH", "subtask_name": "x"},
            {"gcode_state": "FINISH", "subtask_name": "y"},
            None,
        ),
        ({"gcode_state": "IDLE"}, {"gcode_state": "FINISH", "subtask_name": "x"}, None),
        (RUNNING_X, {"mc_percent": 3}, None),
        (None, {"gcode_state": "FINISH", "subtask_name": "x"}, None),
    ],
    ids=[
        "finished",
        "failed",
        "idle",
        "moved on",
        "paused then finished",
        "paused",
        "running",
        "a start under FINISH",
        "never seen running",
        "says too little",
        "no row",
    ],
)
def test_a_write_ends_only_the_print_the_row_showed_running(
    old: dict[str, Any] | None, new: dict[str, Any], ended: PrintEnd | None
) -> None:
    assert ended_print(old, new) == ended


def test_a_write_returns_the_end_it_shows(
    linked: tuple[PrinterStore, PrinterReportStore, str],
) -> None:
    _, reports, pid = linked
    finish = {"gcode_state": "FINISH", "subtask_name": "x"}
    assert reports.put(pid, RUNNING_X, datetime.now(UTC)) is None
    assert reports.put(pid, finish, datetime.now(UTC)) == PrintEnd("x", timed=True)
    assert reports.put(pid, finish, datetime.now(UTC)) is None
