"""``PrinterWatch`` replays the histories it wrote (#4186 review M12).

The watch moves to each new print-worker build (AUTO_UPGRADE), so a run open across a
deploy replays on the new code: a change to ``PrinterWatch.run``'s commands goes behind
``workflow.patched``, and this test is what says so.

Recorded by the workflow as #4186's review round left it (``SCADBUDDY_RECORD_HISTORIES=1``
runs the recordings below): ``stopped`` (started, then the unlink's ``stop``),
``restarted`` (started, then a re-link's ``restart`` by signal-with-start, which ends the
attempt and starts the next, then ``stop``) and ``continued`` (a run past its bound,
which waits for its attempt's cancel and continues as new). Never re-record them: a later
change adds a history recorded on the changed code beside them.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import PendingActivityState
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, Worker

from scadbuddy.workflows.follow import follow_queue
from scadbuddy.workflows.printer_watch import (
    PRINTER_WATCH_ACTIVITY,
    PrinterWatch,
    PrinterWatchInput,
    restart_watch,
    stop_watch,
    watch_id,
)
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "printer_watch_histories"
PRINTER = "6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b"


@pytest.mark.parametrize("name", ["stopped", "restarted", "continued"])
async def test_printer_watch_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"printer-watch-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    await Replayer(
        workflows=[PrinterWatch], data_converter=pydantic_data_converter
    ).replay_workflow(history)


# ── recording ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


@activity.defn(name=PRINTER_WATCH_ACTIVITY)
async def _holds(input: PrinterWatchInput) -> str:
    """The real activity's shape: heartbeats until cancelled."""
    while True:
        activity.heartbeat()
        await asyncio.sleep(0.1)


@asynccontextmanager
async def _serving(address: str) -> AsyncIterator[tuple[Client, str]]:
    client = await Client.connect(address, data_converter=pydantic_data_converter)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(Worker(client, task_queue=queue, workflows=[PrinterWatch]))
        await stack.enter_async_context(
            Worker(
                client,
                task_queue=follow_queue(queue),
                activities=[_holds],
                max_heartbeat_throttle_interval=timedelta(milliseconds=200),
                default_heartbeat_throttle_interval=timedelta(milliseconds=200),
            )
        )
        yield client, queue


async def _attempts(client: Client) -> int:
    """The attempt number of the running activity, 0 while none has started. (A
    retried activity's started event is written only when it closes.)"""
    described = await client.get_workflow_handle(watch_id(PRINTER)).describe()
    pending = described.raw_description.pending_activities
    if not pending or pending[0].state != PendingActivityState.PENDING_ACTIVITY_STATE_STARTED:
        return 0
    return len(
        [
            e
            for e in (await client.get_workflow_handle(watch_id(PRINTER)).fetch_history()).events
            if e.HasField("activity_task_scheduled_event_attributes")
        ]
    )


async def _started(client: Client, scheduled: int = 1) -> None:
    """Until the ``scheduled``-th attempt is running."""
    async with asyncio.timeout(15):
        while await _attempts(client) < scheduled:
            await asyncio.sleep(0.1)


recording = pytest.mark.skipif(
    os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history"
)


@pytest.mark.requires_temporal
@recording
async def test_record_stopped(recording_temporal: str) -> None:
    async with _serving(recording_temporal) as (client, queue):
        handle = await client.start_workflow(
            PrinterWatch.run, args=[PRINTER, None], id=watch_id(PRINTER), task_queue=queue
        )
        await _started(client)
        await stop_watch(client, PRINTER)
        assert await handle.result() == "stopped"
        (HISTORIES / "stopped.json").write_text((await handle.fetch_history()).to_json())


@pytest.mark.requires_temporal
@recording
async def test_record_restarted(recording_temporal: str) -> None:
    async with _serving(recording_temporal) as (client, queue):
        handle = await client.start_workflow(
            PrinterWatch.run, args=[PRINTER, None], id=watch_id(PRINTER), task_queue=queue
        )
        await _started(client)
        await restart_watch(client, queue, PRINTER)
        await _started(client, scheduled=2)
        await stop_watch(client, PRINTER)
        assert await handle.result() == "stopped"
        (HISTORIES / "restarted.json").write_text((await handle.fetch_history()).to_json())


@pytest.mark.requires_temporal
@recording
async def test_record_continued(recording_temporal: str) -> None:
    async with _serving(recording_temporal) as (client, queue):
        handle = await client.start_workflow(
            PrinterWatch.run, args=[PRINTER, 2.0], id=watch_id(PRINTER), task_queue=queue
        )
        first = client.get_workflow_handle(watch_id(PRINTER), run_id=handle.result_run_id)
        async with asyncio.timeout(30):
            while (await first.describe()).status != WorkflowExecutionStatus.CONTINUED_AS_NEW:
                await asyncio.sleep(0.2)
        (HISTORIES / "continued.json").write_text((await first.fetch_history()).to_json())
        await stop_watch(client, PRINTER)
