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
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.printers.watch import SHOWN, WatchActivities, shown
from scadbuddy.workflows.printer_watch import PrinterWatchInput
from tests.conftest import PgPool
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"
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
