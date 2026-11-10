"""The printer watch's reports when a write fails (#5298 Task 30 re-review N1, N3, N4):
a report whose write failed is kept and written again, one whose answer was lost under
the same Idempotency-Key (so the API answers the print end that write showed, which the
watch then settles), one that never ran under a new key; ``printer.report`` is announced
only once the write lands; a cancel passes through; and a failure leaves something to
search for in the log, never the report. No database: a scripted store and printer."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.events import Event
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.printers import watch as watch_module
from scadbuddy.printers.lan import LanPrinter
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.reports import PrintEnd, PrinterReportStore
from scadbuddy.printers.watch import WATCH_WRITE_FAILED, WatchActivities
from scadbuddy.rack.usage import LanUsage

PRINTER = "00000000-0000-4000-8000-000000000001"
FINISH = {"gcode_state": "FINISH", "subtask_name": "sub-1"}


class Store:
    """A report store answering each ``put`` from ``script`` (an end, None, or an error to
    raise), and logging every put and touch with its key to ``log``."""

    def __init__(self, log: list[Any], script: list[Any]) -> None:
        self.log = log
        self.script = script
        self.puts: list[tuple[dict[str, Any], str | None]] = []

    def put(
        self,
        printer_id: str,
        report: dict[str, Any],
        seen_at: datetime,
        *,
        idempotency_key: str | None = None,
    ) -> PrintEnd | None:
        self.puts.append((report, idempotency_key))
        self.log.append(("put", idempotency_key))
        answer = self.script.pop(0) if self.script else None
        if isinstance(answer, BaseException):
            raise answer
        return cast(PrintEnd | None, answer)

    def touch(self, printer_id: str, seen_at: datetime) -> None:
        self.log.append(("touch", None))


class Rack:
    def __init__(self, log: list[Any]) -> None:
        self.log = log

    async def settle_lan_ended(
        self, printer: str, subtask_name: str, ended_at: datetime, *, timed: bool = True
    ) -> int:
        self.log.append(("settle", subtask_name))
        return 1


class Bus:
    def __init__(self, log: list[Any]) -> None:
        self.log = log

    def publish(self, event: Event) -> None:
        self.log.append(("event", event.kind))


class Lan:
    """A connection that subscribes, yields ``deltas``, then stays quiet."""

    def __init__(self, deltas: list[dict[str, Any]]) -> None:
        self.deltas = deltas

    async def reports(self, connected: asyncio.Event | None = None) -> AsyncIterator[LanReport]:
        if connected is not None:
            connected.set()
        for delta in self.deltas:
            yield LanReport(serial="01P00A000000001", received_at=datetime.now(UTC), print=delta)
        await asyncio.Event().wait()


def _watch(log: list[Any], script: list[Any]) -> tuple[WatchActivities, Store]:
    store = Store(log, script)
    watches = WatchActivities(
        None,  # type: ignore[arg-type]
        cast(PrinterReportStore, store),
        cast(Any, Bus(log)),
        rack=cast(LanUsage, Rack(log)),
    )
    return watches, store


async def _follow_until(
    watches: WatchActivities, lan: Lan, done: Callable[[], bool], limit: float = 5.0
) -> None:
    task = asyncio.ensure_future(
        ActivityEnvironment().run(watches._follow, PRINTER, cast(LanPrinter, lan))
    )
    try:
        async with asyncio.timeout(limit):
            while not done():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.fixture(autouse=True)
def quick_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch_module, "HEARD_EVERY", 0.05)


async def test_a_lost_answer_is_sent_again_under_its_key_and_its_end_settled() -> None:
    """The first put's answer is lost (it may have landed): the report is kept and sent
    again under the same key, whose replay answers the end; the end is settled and the
    report announced only then."""
    log: list[Any] = []
    lost = CrudUnavailableError("no answer", "transport")
    watches, store = _watch(log, [lost, PrintEnd("sub-1", timed=True)])
    await _follow_until(watches, Lan([FINISH]), lambda: ("event", "printer.report") in log)
    first, second = store.puts[0], store.puts[1]
    assert first == second == (FINISH, first[1]) and first[1] is not None
    assert log[:4] == [
        ("put", first[1]),
        ("put", first[1]),
        ("settle", "sub-1"),
        ("event", "printer.report"),
    ]


async def test_a_write_that_never_ran_is_sent_again_under_a_new_key() -> None:
    log: list[Any] = []
    unreached = CrudUnavailableError("no connection", "unreached")
    watches, store = _watch(log, [unreached, None])
    await _follow_until(watches, Lan([FINISH]), lambda: ("event", "printer.report") in log)
    assert [report for report, _ in store.puts[:2]] == [FINISH, FINISH]
    assert store.puts[0][1] != store.puts[1][1]
    assert log.index(("event", "printer.report")) > 1


async def test_an_owed_report_goes_before_a_newer_one() -> None:
    """A newer report waits while the lost write is sent again under its key: one write
    per turn, the owed one first, then the newer one under a key of its own."""
    log: list[Any] = []
    lost = CrudUnavailableError("no answer", "transport")
    newer = {"gcode_state": "IDLE"}
    watches, store = _watch(log, [lost, PrintEnd("sub-1", timed=True), None])
    await _follow_until(watches, Lan([FINISH, newer]), lambda: len(store.puts) >= 3)
    (r1, k1), (r2, k2), (r3, k3) = store.puts[:3]
    assert (r1, r2, r3) == (FINISH, FINISH, newer)
    assert k1 == k2 != k3
    assert ("settle", "sub-1") in log


async def test_a_cancel_passes_through_a_write_and_a_touch() -> None:
    """The activity's cancel is never counted or swallowed as a failed write."""
    release = threading.Event()

    class Blocked:
        def put(self, *args: Any, **kwargs: Any) -> None:
            release.wait(5)

        def touch(self, *args: Any) -> None:
            release.wait(5)

    watches = WatchActivities(
        None,  # type: ignore[arg-type]
        cast(PrinterReportStore, Blocked()),
        None,
    )
    report = LanReport(serial="s", received_at=datetime.now(UTC), print={})
    try:
        for call in (
            watches._write(PRINTER, report, "k"),
            watches._touch(PRINTER, datetime.now(UTC)),
        ):
            task = asyncio.ensure_future(call)
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        release.set()


async def test_a_failure_is_logged_to_be_found_never_with_the_report(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log: list[Any] = []
    refused = CrudRefusedError(
        422, {"type": "https://x/problem", "reason": "bad", "detail": "TEST-HOTEND-17"}
    )
    watches, _ = _watch(log, [refused, TypeError("a bug")])
    report = LanReport(serial="s", received_at=datetime.now(UTC), print={"secret": "S3CR3T"})
    with caplog.at_level(logging.WARNING):
        assert await watches._write(PRINTER, report, "k1") == "unwritten"
        assert await watches._write(PRINTER, report, "k2") == "unwritten"
    first, second = [r for r in caplog.records if r.getMessage() == WATCH_WRITE_FAILED]
    assert (first.status, first.problem_type, first.reason) == (422, "https://x/problem", "bad")  # type: ignore[attr-defined]
    assert first.exc_info is None
    assert second.exc_info is not None and second.exc_info[0] is TypeError
    assert "S3CR3T" not in caplog.text and "TEST-HOTEND-17" not in caplog.text
