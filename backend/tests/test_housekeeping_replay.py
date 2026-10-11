"""``Housekeeping`` replays the histories it wrote (#1846).

The ``library`` worker is unversioned, so a run left open across a deploy replays on
the new code (``workflows/housekeeping.py``'s docstring). A change to the commands
``Housekeeping.run`` issues (a sweep added, `SWEEPS` reordered) goes behind
``workflow.patched``; this test is what says so.

``before_reap`` was recorded by the workflow as it was before ``REAP_PATCH``
(327f66551^, six sweeps), started with no list, so it runs `SWEEPS`. ``reap`` was
recorded by the patched workflow, which also runs ``REAP_SWEEP``. ``flow_runs`` was
recorded by the workflow with ``FLOWS_PATCH`` too (#1057), which also runs
``FLOWS_SWEEP`` (``SCADBUDDY_RECORD_HISTORIES=1`` runs the recording below). Never
re-record them: a later change adds a history recorded on the changed code beside them.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowHistory
from temporalio.worker import Replayer, Worker

from scadbuddy.workflows.housekeeping import SWEEPS, Housekeeping
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "housekeeping_histories"


@pytest.mark.parametrize("name", ["before_reap", "reap", "flow_runs"])
async def test_housekeeping_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"housekeeping-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    await Replayer(workflows=[Housekeeping]).replay_workflow(history)


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
async def test_record_flow_runs(recording_temporal: str) -> None:
    """Every sweep a stub, so only the workflow's own commands are recorded."""

    def stub(name: str) -> Any:
        @activity.defn(name=name)
        async def sweep() -> None:
            return None

        return sweep

    client = await Client.connect(recording_temporal)
    queue = f"hk-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[Housekeeping],
        activities=[stub(name) for name in SWEEPS],
    ):
        handle = await client.start_workflow(
            Housekeeping.run, id=f"housekeeping-{queue}", task_queue=queue
        )
        assert await handle.result() == []
        history = await handle.fetch_history()
    names = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert names == list(SWEEPS)
    (HISTORIES / "flow_runs.json").write_text(history.to_json())
