"""``PrinterWatch`` (#3823, plan 2026-10-16, Task 6) on a dev server: the real activity
over a fake LAN connection, writing each LAN printer's latest report to Postgres."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.service import RPCError
from temporalio.worker import Worker

from scadbuddy.core.events import Event, EventBus, PrinterEvent
from scadbuddy.core.tracing import span
from scadbuddy.printers.lan import LanPrinter
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.printers.watch import WatchActivities
from scadbuddy.workflows.follow import follow_queue
from scadbuddy.workflows.printer_watch import (
    PrinterWatch,
    ensure_watch,
    ensure_watches,
    restart_watch,
    stop_watch,
    watch_id,
)
from tests.conftest import PgPool
from tests.printers.conftest import KEK
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

SERIAL = "01P00A000000001"


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


class FakeLan:
    """`LanPrinter.reports` over a list of deltas, merged as the real one merges; it
    then holds the connection open until cancelled."""

    def __init__(self, deltas: list[dict[str, Any]]) -> None:
        self.deltas = deltas
        self.connects = 0
        self.hosts: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        async def reports(
            printer: LanPrinter, connected: asyncio.Event | None = None
        ) -> AsyncIterator[LanReport]:
            fake.connects += 1
            if connected is not None:
                connected.set()
            fake.hosts.append(printer.host)
            merged: dict[str, Any] = {}
            for delta in fake.deltas:
                merged = {**merged, **delta}
                yield LanReport(serial=printer.serial, received_at=datetime.now(UTC), print=merged)
            await asyncio.Event().wait()

        monkeypatch.setattr(LanPrinter, "reports", reports)


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
def printers(pg_pool: PgPool) -> PrinterStore:
    return PrinterStore(pg_pool, KEK)


@pytest.fixture
def reports(pg_pool: PgPool) -> PrinterReportStore:
    return PrinterReportStore(pg_pool)


def linked(printers: PrinterStore) -> str:
    row = printers.create(PrinterCreate(serial=SERIAL, name="H2C"))
    printers.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    return row.id


@asynccontextmanager
async def serving(client: Client, queue: str, watches: WatchActivities) -> AsyncIterator[None]:
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(Worker(client, task_queue=queue, workflows=[PrinterWatch]))
        await stack.enter_async_context(
            Worker(
                client,
                task_queue=follow_queue(queue),
                activities=[watches.printer_watch],
                # A cancel reaches the attempt on its next heartbeat: soon, in a test.
                max_heartbeat_throttle_interval=timedelta(milliseconds=200),
                default_heartbeat_throttle_interval=timedelta(milliseconds=200),
            )
        )
        try:
            yield
        finally:
            await terminate_open_workflows(client, queue)


async def _until(predicate: Any, timeout: float = 15) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


async def test_watch_writes_reports_and_emits_on_change(
    client: Client,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lan = FakeLan(
        [
            {"gcode_state": "RUNNING", "mc_percent": 1},
            {"wifi_signal": "-45dBm"},  # nothing a card shows: written later, not announced
            {"mc_percent": 2},
        ]
    )
    lan.install(monkeypatch)
    pid = linked(printers)
    bus = RecordingBus()
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    watches = WatchActivities(printers, reports, cast(EventBus, bus), write_every=0.0)
    async with serving(client, queue, watches):
        await ensure_watch(client, queue, pid)
        await ensure_watch(client, queue, pid)  # idempotent

        def latest() -> dict[str, Any] | None:
            got = reports.get(pid)
            return None if got is None else got[0]

        await _until(lambda: (latest() or {}).get("mc_percent") == 2)
        assert latest() == {"gcode_state": "RUNNING", "mc_percent": 2, "wifi_signal": "-45dBm"}
        announced = [event for event in bus.events if isinstance(event, PrinterEvent)]
        assert [(e.kind, e.printer_id) for e in announced] == [("printer.report", pid)] * 2
        assert lan.connects == 1 and lan.hosts == ["10.0.0.9"]

        await stop_watch(client, pid)
        handle = client.get_workflow_handle(watch_id(pid))
        assert await handle.result() == "stopped"


async def test_reports_are_throttled_but_changes_are_not(
    client: Client,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lan = FakeLan([{"mc_percent": 1}, {"wifi_signal": "a"}, {"wifi_signal": "b"}])
    lan.install(monkeypatch)
    pid = linked(printers)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    watches = WatchActivities(printers, reports, cast(EventBus, RecordingBus()), write_every=3600.0)
    async with serving(client, queue, watches):
        await ensure_watch(client, queue, pid)
        await _until(lambda: reports.get(pid) is not None)
        await asyncio.sleep(0.5)
        got = reports.get(pid)
        assert got is not None and got[0] == {"mc_percent": 1}


async def test_watch_stops_on_signal(
    client: Client,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeLan([]).install(monkeypatch)
    pid = linked(printers)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    async with serving(client, queue, WatchActivities(printers, reports, None)):
        await ensure_watch(client, queue, pid)
        await stop_watch(client, pid)
        assert await client.get_workflow_handle(watch_id(pid)).result() == "stopped"
        await stop_watch(client, pid)  # an ended watch: nothing to stop


async def test_a_watch_on_an_unlinked_printer_ends(
    client: Client,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lan = FakeLan([{"mc_percent": 1}])
    lan.install(monkeypatch)
    row = printers.create(PrinterCreate(serial=SERIAL, name="H2C"))
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    async with serving(client, queue, WatchActivities(printers, reports, None)):
        await ensure_watch(client, queue, row.id)
        assert await client.get_workflow_handle(watch_id(row.id)).result() == "unlinked"
    assert lan.connects == 0


async def test_a_watch_without_the_key_ends(
    client: Client,
    pg_pool: PgPool,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeLan([{"mc_percent": 1}]).install(monkeypatch)
    pid = linked(printers)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    keyless = PrinterStore(pg_pool, None)
    async with serving(client, queue, WatchActivities(keyless, reports, None)):
        await ensure_watch(client, queue, pid)
        try:
            result = await client.get_workflow_handle(watch_id(pid)).result()
        except WorkflowFailureError:  # pragma: no cover - the answer is a result
            pytest.fail("a watch without the key must end, not fail")
        assert result == "no-key"


async def test_the_sweep_watches_every_lan_printer(
    client: Client,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeLan([]).install(monkeypatch)
    pid = linked(printers)
    other = printers.create(PrinterCreate(serial="01P00A000000002", name="No LAN"))
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    async with serving(client, queue, WatchActivities(printers, reports, None)):
        assert await ensure_watches(printers, client, queue) == [pid]
        described = await client.get_workflow_handle(watch_id(pid)).describe()
        assert described.status is not None and described.status.name == "RUNNING"
        with pytest.raises(RPCError):
            await client.get_workflow_handle(watch_id(other.id)).describe()


async def _running(client: Client, printer_id: str) -> bool:
    described = await client.get_workflow_handle(watch_id(printer_id)).describe()
    return described.status is not None and described.status.name == "RUNNING"


async def test_unlink_then_relink_watches_again(
    client: Client,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#4186 review I4: the re-link's restart reaches the run the unlink's stop is still
    ending, so it keeps watching instead of returning ``stopped``."""
    lan = FakeLan([{"mc_percent": 1}])
    lan.install(monkeypatch)
    pid = linked(printers)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    async with serving(client, queue, WatchActivities(printers, reports, None)):
        await ensure_watch(client, queue, pid)
        await _until(lambda: lan.connects == 1)
        await stop_watch(client, pid)
        await restart_watch(client, queue, pid)
        await _until(lambda: lan.connects == 2)
        await asyncio.sleep(0.5)
        assert await _running(client, pid)


async def test_a_restart_reads_the_new_link(
    client: Client,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed host or code takes effect at once, not after the old connection drops."""
    lan = FakeLan([{"mc_percent": 1}])
    lan.install(monkeypatch)
    pid = linked(printers)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    async with serving(client, queue, WatchActivities(printers, reports, None)):
        await ensure_watch(client, queue, pid)
        await _until(lambda: lan.connects == 1)
        printers.link_lan(
            pid, LanLink(host="10.0.0.10", access_code="87654321"), cert_sha256="cd" * 32
        )
        await restart_watch(client, queue, pid)
        await _until(lambda: lan.hosts == ["10.0.0.9", "10.0.0.10"])
        await ensure_watch(client, queue, pid)  # the sweep's: no restart
        await asyncio.sleep(0.5)
        assert lan.connects == 2


async def test_a_restart_starts_a_watch_that_ended(
    client: Client,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lan = FakeLan([])
    lan.install(monkeypatch)
    row = printers.create(PrinterCreate(serial=SERIAL, name="H2C"))
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    async with serving(client, queue, WatchActivities(printers, reports, None)):
        await ensure_watch(client, queue, row.id)
        assert await client.get_workflow_handle(watch_id(row.id)).result() == "unlinked"
        printers.link_lan(
            row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
        )
        await restart_watch(client, queue, row.id)
        await _until(lambda: lan.connects == 1)
        assert await _running(client, row.id)


async def test_the_watch_continues_as_new(
    client: Client,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M12: past its bound the run continues as new, and the next run watches on."""
    lan = FakeLan([{"mc_percent": 1}])
    lan.install(monkeypatch)
    pid = linked(printers)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    async with serving(client, queue, WatchActivities(printers, reports, None)):
        first = await client.start_workflow(
            PrinterWatch.run, args=[pid, 1.0], id=watch_id(pid), task_queue=queue
        )
        await _until(lambda: lan.connects >= 2, timeout=30)
        described = await client.get_workflow_handle(watch_id(pid)).describe()
        assert described.run_id != first.result_run_id
        assert await _running(client, pid)


async def test_no_span_or_log_record_of_a_watch_carries_the_serial(
    client: Client,
    printers: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
    spans: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#4186 re-review N1: the watch is keyed by the registry id, so neither Temporal's
    spans (``temporalWorkflowID``) nor the SDK's failed-attempt warning name the serial."""
    attempts = 0

    async def reports_once_failing(
        printer: LanPrinter, connected: asyncio.Event | None = None
    ) -> AsyncIterator[LanReport]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("refused")
        if connected is not None:
            connected.set()
        yield LanReport(serial=printer.serial, received_at=datetime.now(UTC), print={"x": 1})
        await asyncio.Event().wait()

    monkeypatch.setattr(LanPrinter, "reports", reports_once_failing)
    config = client.config()
    config["interceptors"] = [TracingInterceptor()]
    traced = Client(**config)
    pid = linked(printers)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    spans.clear()
    with caplog.at_level(logging.DEBUG):
        async with serving(traced, queue, WatchActivities(printers, reports, None)):
            # A parent, as a route's request is: the sampler drops parentless CLIENT spans.
            with span("test.printer_link"):
                await restart_watch(traced, queue, pid)
            await _until(lambda: reports.get(pid) is not None, timeout=30)
            with span("test.printer_unlink"):
                await stop_watch(traced, pid)
            await traced.get_workflow_handle(watch_id(pid)).result()
    assert attempts >= 2
    finished = spans.get_finished_spans()
    assert any(
        (done.attributes or {}).get("temporalWorkflowID") == watch_id(pid) for done in finished
    )
    for done in finished:
        assert SERIAL not in done.name
        assert SERIAL not in repr(dict(done.attributes or {}))
    assert any("failed" in record.getMessage() for record in caplog.records)
    for record in caplog.records:
        assert SERIAL not in record.getMessage()
        assert SERIAL not in repr(record.__dict__)
