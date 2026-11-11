"""The print watcher's LAN nudge (#4699 phase 3): a LAN job start that no native run
claims, on a printer Bambuddy knows, starts one watcher pass at once."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.print_watch.nudge import Nudger, claimed, nudge_id, start_pass, wanted
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.reports import started_print
from scadbuddy.workflows.housekeeping import AUTO_IMPORT_SWEEP, SWEEPS, Housekeeping
from tests.support.temporal import temporal_client, terminate_open_workflows

ON = StoredSettings(print_auto_import=True)
NOW = datetime(2026, 11, 11, tzinfo=UTC)
LAN_ONLY = PrinterRow(id="p1", serial="01P00A000000001", name="H2C", created_at=NOW, updated_at=NOW)
LINKED = LAN_ONLY.model_copy(update={"bambuddy_printer_id": 7})
#: A job a person started at the printer, or from Bambu Studio.
THEIRS = "benchy"
#: A native run's start (`workflows/printing.py` `_start_lan`).
NATIVE = "scadbuddy-0a1b2c3d-p1"


def skipping(bambuddy_printer_id: int) -> StoredSettings:
    return StoredSettings(
        print_auto_import=True, print_auto_import_skip_printers=[bambuddy_printer_id]
    )


@pytest.mark.parametrize(
    ("subtask", "expected"),
    [
        (NATIVE, True),
        ("scadbuddy-0a1b2c3d-p12-c3", True),
        (THEIRS, False),
        # Not the form `_start_lan` names a start with.
        ("scadbuddy-0a1b2c3d", False),
        ("scadbuddy-0a1b2c3d-p1.3mf", False),
        ("my-scadbuddy-0a1b2c3d-p1", False),
        ("scadbuddy-0A1B2C3D-p1", False),
    ],
)
def test_a_native_run_claims_the_start_it_named(subtask: str, expected: bool) -> None:
    assert claimed(subtask) is expected


@pytest.mark.parametrize(
    ("settings", "row", "subtask", "expected"),
    [
        (ON, LINKED, THEIRS, True),
        (ON, LINKED, NATIVE, False),
        (StoredSettings(), LINKED, THEIRS, False),
        (ON, LAN_ONLY, THEIRS, False),
        (ON, None, THEIRS, False),
        (skipping(7), LINKED, THEIRS, False),
        (skipping(8), LINKED, THEIRS, True),
    ],
    ids=["theirs", "native", "off", "no Bambuddy", "row gone", "skipped", "another skipped"],
)
def test_only_a_start_the_watcher_would_take_is_nudged(
    settings: StoredSettings, row: PrinterRow | None, subtask: str, expected: bool
) -> None:
    assert wanted(settings, row, subtask) is expected


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (None, "x", "x"),
        ("x", "y", "y"),
        ("x", "x", None),
        ("x", None, None),
        (None, None, None),
    ],
    ids=["first start", "the next job", "still running", "ended", "idle"],
)
def test_a_start_is_a_new_running_print(
    before: str | None, after: str | None, expected: str | None
) -> None:
    assert started_print(before, after) == expected


class Recorder:
    def __init__(self, settings: StoredSettings, row: PrinterRow | None) -> None:
        self.settings, self.row = settings, row
        self.reads: list[str] = []
        self.started: list[str] = []

    def load(self) -> StoredSettings:
        self.reads.append("settings")
        return self.settings

    def get(self, printer_id: str) -> PrinterRow | None:
        self.reads.append(printer_id)
        return self.row

    async def start(self, task_queue: str) -> None:
        self.started.append(task_queue)


async def test_a_start_the_watcher_takes_starts_a_pass_on_the_library_queue() -> None:
    fake = Recorder(ON, LINKED)
    nudger = Nudger(fake.load, fake.get, "library", start=fake.start)
    assert await nudger.nudge("p1", THEIRS) is True
    assert fake.started == ["library"]


async def test_a_native_start_reads_nothing_and_starts_nothing() -> None:
    fake = Recorder(ON, LINKED)
    nudger = Nudger(fake.load, fake.get, "library", start=fake.start)
    assert await nudger.nudge("p1", NATIVE) is False
    assert (fake.reads, fake.started) == ([], [])


async def test_with_the_watcher_off_no_pass_starts() -> None:
    fake = Recorder(StoredSettings(), LINKED)
    nudger = Nudger(fake.load, fake.get, "library", start=fake.start)
    assert await nudger.nudge("p1", THEIRS) is False
    assert fake.started == []


class Sweeps:
    def __init__(self) -> None:
        self.ran: list[str] = []

    def all(self) -> list[object]:
        def make(name: str) -> object:
            @activity.defn(name=name)
            async def sweep() -> None:
                self.ran.append(name)

            return sweep

        return [make(name) for name in SWEEPS]


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.mark.requires_temporal
async def test_nudges_while_a_pass_waits_share_it_and_it_runs_the_watcher_only(
    client: Client,
) -> None:
    """Never more than one nudge run in flight: a second start while the first waits out
    its delay joins it. Once it ends, the next nudge starts a run of its own."""
    queue = f"library-{uuid.uuid4().hex[:8]}"
    fake = Sweeps()
    try:
        first = await start_pass(client, queue, delay=timedelta(seconds=1))
        second = await start_pass(client, queue, delay=timedelta(seconds=1))
        assert first.id == second.id == nudge_id(queue)
        assert first.result_run_id == second.result_run_id
        async with Worker(
            client,
            task_queue=queue,
            workflows=[Housekeeping],
            activities=fake.all(),  # type: ignore[arg-type]
        ):
            assert await first.result() == []
            third = await start_pass(client, queue, delay=timedelta(0))
            assert third.result_run_id != first.result_run_id
            await asyncio.wait_for(third.result(), 30)
        assert fake.ran == [AUTO_IMPORT_SWEEP, AUTO_IMPORT_SWEEP]
    finally:
        await terminate_open_workflows(client, queue)
