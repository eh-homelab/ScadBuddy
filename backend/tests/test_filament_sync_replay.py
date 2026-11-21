"""``FilamentBbSync`` replays the histories it wrote (#6265 Task 8).

On the versioned print worker it is AUTO_UPGRADE (`client.print_deployment`), so a run
left open across a deploy replays on the new code. A change to the commands
``FilamentBbSync.run`` issues goes behind ``workflow.patched``; this test is what says so.

``v1`` was recorded (``SCADBUDDY_RECORD_HISTORIES=1`` runs the recording below) from a run
that a signal started: a pass that left work, the next one at once, then the timer's
full pass, which found another instance configured and ended the run. Never re-record
it: a later change adds a history recorded on the changed code beside it, and records
only that one (``-k <name>``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, Worker

from scadbuddy.filament.bb_sync_models import SYNC_PASS_ACTIVITY, SyncPassInput, SyncPassResult
from scadbuddy.filament.bb_sync_nudge import CHANGED_SIGNAL, SyncStart, workflow_id
from scadbuddy.workflows.client import sandboxed_runner
from scadbuddy.workflows.filament_sync import FilamentBbSync
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "filament_sync_histories"


@pytest.mark.parametrize("name", ["v1"])
async def test_the_history_replays(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"filament-sync-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    await Replayer(
        workflows=[FilamentBbSync], data_converter=pydantic_data_converter
    ).replay_workflow(history)


def test_the_v1_history_holds_three_passes_and_the_timer() -> None:
    history = WorkflowHistory.from_json("filament-sync-v1", (HISTORIES / "v1.json").read_text())
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled == [SYNC_PASS_ACTIVITY] * 3
    timers = [event for event in history.events if event.HasField("timer_started_event_attributes")]
    # The debounce after the signal, and the wait that ran out as the timer.
    assert len(timers) == 2


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
# Only the newest history is recorded: the older ones were recorded by the code of their
# time and are never re-recorded.
@pytest.mark.parametrize("name", ["v1"])
async def test_record(recording_temporal: str, name: str) -> None:
    answers: Iterator[dict[str, object]] = iter(
        [
            {"more": True, "deferred": 1},
            {},
            {"instance_changed": True},
        ]
    )

    @activity.defn(name=SYNC_PASS_ACTIVITY)
    async def sync_pass(start: SyncPassInput) -> SyncPassResult:
        base: dict[str, object] = {
            "written": 0,
            "failed": 0,
            "deferred": 0,
            "more": False,
            "instance_changed": False,
            "interval_s": start.interval_s,
        }
        return SyncPassResult.model_validate(base | next(answers))

    client = await Client.connect(recording_temporal, data_converter=pydantic_data_converter)
    queue = f"filament-sync-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[FilamentBbSync],
        activities=[sync_pass],
        workflow_runner=sandboxed_runner(),
    ):
        handle = await client.start_workflow(
            FilamentBbSync.run,
            SyncStart(instance="0123456789ab", interval_s=1),
            id=f"{workflow_id('0123456789ab')}-{queue}",
            task_queue=queue,
            start_signal=CHANGED_SIGNAL,
            start_signal_args=[False],
        )
        await handle.result()
        history = await handle.fetch_history()
    HISTORIES.mkdir(exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())
