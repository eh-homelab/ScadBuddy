"""``SpoolRefresh`` replays the histories it wrote (#3818 tier A, plan 2026-10-18 Task 6b).

On the versioned ``library`` worker it is AUTO_UPGRADE (`VersionedSpoolRefresh`), so a
run left open across a deploy replays on the new code. A change to the commands
``SpoolRefresh.run`` issues goes behind ``workflow.patched``; this test is what says so.

``refreshed`` was recorded with a refresh that answered, ``failed`` with one that raised
(``SCADBUDDY_RECORD_HISTORIES=1`` runs the recording below). Never re-record them: a
later change adds a history recorded on the changed code beside them.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.worker import Replayer, Worker

from scadbuddy.workflows.spool_refresh import REFRESH_ACTIVITY, SpoolRefresh
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "spool_refresh_histories"


@pytest.mark.parametrize("name", ["refreshed", "failed"])
async def test_spool_refresh_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"spool-refresh-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    await Replayer(workflows=[SpoolRefresh]).replay_workflow(history)


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
@pytest.mark.parametrize("name", ["refreshed", "failed"])
async def test_record(recording_temporal: str, name: str) -> None:
    @activity.defn(name=REFRESH_ACTIVITY)
    async def refresh() -> dict[str, str]:
        if name == "failed":
            raise RuntimeError("the store is down")
        return {"bambuddy": "refreshed"}

    client = await Client.connect(recording_temporal)
    queue = f"spools-{uuid.uuid4().hex[:8]}"
    async with Worker(client, task_queue=queue, workflows=[SpoolRefresh], activities=[refresh]):
        handle = await client.start_workflow(
            SpoolRefresh.run, id=f"spool-providers-{queue}", task_queue=queue
        )
        if name == "failed":
            with pytest.raises(WorkflowFailureError):
                await handle.result()
        else:
            assert await handle.result() == {"bambuddy": "refreshed"}
        history = await handle.fetch_history()
    HISTORIES.mkdir(exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())
