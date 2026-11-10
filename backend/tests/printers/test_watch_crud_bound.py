"""The printer watch's CRUD calls on the print worker (#5298 Task 30 review I2): a stalled
API makes a report's write, its touch and its rack settle give up within
`WATCH_CALL_BUDGET` plus one try's `WATCH_CALL_TIMEOUT`, well inside the watch's 30 s
heartbeat timeout, and the watch goes on: the failure is logged and counted, never the
attempt's. No database: the API is a mock transport whose every try runs its whole
timeout on a fake clock and never answers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from prometheus_client import CollectorRegistry, Counter

from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.crud.client import CrudClient, CrudSyncClient
from scadbuddy.crud.guard import CRUD_HEADERS
from scadbuddy.crud.print import (
    WATCH_CALL_BUDGET,
    WATCH_CALL_TIMEOUT,
    RemoteRackUsage,
    RemoteReports,
)
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.watch import OWED, WRITTEN, WatchActivities
from scadbuddy.workflows.printer_watch import WATCH_HEARTBEAT
from tests.crud.crud_support import FixedToken

PRINTER = "00000000-0000-4000-8000-000000000001"
#: The most one bounded call may take: its budget, then a try begun at its very end.
ONE_CALL = WATCH_CALL_BUDGET + WATCH_CALL_TIMEOUT


class Api:
    """An API that never answers a stalled path: each try runs its whole read timeout on
    the clock, then times out. Every other path answers ``answers``."""

    def __init__(self, stalled: str, answers: dict[str, Any] | None = None) -> None:
        self.stalled = stalled
        self.answers = answers or {}
        self.now = 0.0
        self.timeouts: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    async def asleep(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(self.stalled):
            timeout = request.extensions["timeout"]["read"]
            self.timeouts.append(timeout)
            self.now += timeout
            raise httpx.ReadTimeout("no answer", request=request)
        return httpx.Response(200, json=self.answers, headers=CRUD_HEADERS)


def _clients(api: Api) -> tuple[CrudSyncClient, CrudClient]:
    auth = ServiceAuth(FixedToken())
    blocking = CrudSyncClient(
        "http://api",
        "print",
        auth,
        transport=httpx.MockTransport(api),
        sleep=api.sleep,
        clock=api.clock,
    )
    crud = CrudClient(
        "http://api",
        "print",
        auth,
        transport=httpx.MockTransport(api),
        sleep=api.asleep,
        clock=api.clock,
    )
    return blocking, crud


def _counter(name: str) -> Counter:
    return Counter(name, name, registry=CollectorRegistry())


def _report() -> LanReport:
    return LanReport(serial="01P00A000000001", received_at=datetime.now(UTC), print={})


def test_the_bound_fits_well_inside_the_heartbeat_timeout() -> None:
    # A report's write and its settle, between two heartbeats.
    assert WATCH_HEARTBEAT.total_seconds() / 3 >= 2 * ONE_CALL


async def test_a_stalled_api_makes_a_reports_write_give_up_within_the_bound() -> None:
    api = Api(stalled=f"/reports/{PRINTER}")
    blocking, crud = _clients(api)
    failed = _counter("write_failed")
    try:
        watches = WatchActivities(
            None,  # type: ignore[arg-type]
            RemoteReports(blocking),
            None,
            write_failed=failed,
        )
        assert await watches._write(PRINTER, _report(), "k1") == OWED
    finally:
        blocking.close()
        await crud.aclose()
    assert failed._value.get() == 1
    assert len(api.timeouts) >= 1
    assert set(api.timeouts) == {WATCH_CALL_TIMEOUT}
    assert api.now <= ONE_CALL


async def test_a_stalled_api_makes_a_touch_give_up_within_the_bound() -> None:
    api = Api(stalled="/touch")
    blocking, crud = _clients(api)
    failed = _counter("touch_failed")
    try:
        watches = WatchActivities(
            None,  # type: ignore[arg-type]
            RemoteReports(blocking),
            None,
            write_failed=failed,
        )
        await watches._touch(PRINTER, datetime.now(UTC))
    finally:
        blocking.close()
        await crud.aclose()
    assert failed._value.get() == 1
    assert set(api.timeouts) == {WATCH_CALL_TIMEOUT}
    assert api.now <= ONE_CALL


async def test_a_stalled_settle_gives_up_within_the_bound_and_the_write_stands() -> None:
    """The report is written and shows a print ended; its rack settle never answers. The
    settle is counted as failed and the write is still the watch's success."""
    api = Api(
        stalled="/rack/settle-lan-ended",
        answers={"ended": {"subtask_name": "scadbuddy-run-1-p1", "timed": True}},
    )
    blocking, crud = _clients(api)
    settle_failed = _counter("settle_failed")
    write_failed = _counter("write_failed")
    try:
        watches = WatchActivities(
            None,  # type: ignore[arg-type]
            RemoteReports(blocking),
            None,
            rack=RemoteRackUsage(crud),
            settle_failed=settle_failed,
            write_failed=write_failed,
        )
        assert await watches._write(PRINTER, _report(), "k1") == WRITTEN
    finally:
        blocking.close()
        await crud.aclose()
    assert settle_failed._value.get() == 1
    assert write_failed._value.get() == 0
    assert set(api.timeouts) == {WATCH_CALL_TIMEOUT}
    assert api.now <= ONE_CALL
