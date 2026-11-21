"""``ProfileFetch`` replays the history it wrote (#6625 R-F8).

The profile-fetch worker is not versioned, and a run's workflow can stay open for up to
`workflow.EXECUTION_TIMEOUT` (24 h), across a deploy. A change to the commands
``ProfileFetch.run`` issues goes behind ``workflow.patched``; this test is what says so: a
non-deterministic edit fails it.

``imported`` was recorded with the export and the import faked by name, on the happy path
(``SCADBUDDY_RECORD_HISTORIES=1`` runs the recording below). Never re-record it: a later
change adds a history recorded on the changed code beside it.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, Worker

from scadbuddy.profile_fetch.wire import (
    EXPORT_ACTIVITY,
    FAILED_ACTIVITY,
    IMPORT_ACTIVITY,
    ExportFailure,
    ExportReport,
    ExportTarget,
    FetchAsk,
    ImportOutcome,
)
from scadbuddy.profile_fetch.workflow import ProfileFetch
from tests.support.temporal import temporal_client

HISTORIES = Path(__file__).parents[1] / "fixtures" / "profile_fetch_histories"

REPORT = ExportReport(
    exported=3, failures=[ExportFailure(printer_model="H2C", nozzle="0.2", reason="no 0.2")]
)
OUTCOME = ImportOutcome(status="partial", presets=3, k_values=1, missing=[], notes=[])


@pytest.mark.parametrize("name", ["imported"])
async def test_profile_fetch_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"profile-fetch-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    replayer = Replayer(workflows=[ProfileFetch], data_converter=pydantic_data_converter)
    await replayer.replay_workflow(history)


def test_the_imported_history_exports_then_imports() -> None:
    history = WorkflowHistory.from_json(
        "profile-fetch-imported", (HISTORIES / "imported.json").read_text()
    )
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled == [EXPORT_ACTIVITY, IMPORT_ACTIVITY]


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
@pytest.mark.parametrize("name", ["imported"])
async def test_record(name: str) -> None:
    @activity.defn(name=EXPORT_ACTIVITY)
    async def export(ask: FetchAsk) -> ExportReport:
        return REPORT

    @activity.defn(name=IMPORT_ACTIVITY)
    async def import_(run_id: str, report: ExportReport) -> ImportOutcome:
        return OUTCOME

    @activity.defn(name=FAILED_ACTIVITY)
    async def failed(run_id: str, reason: str) -> None:
        raise AssertionError("the happy path posts no failed")

    ask = FetchAsk(
        run_id=str(uuid.uuid4()),
        type_id=str(uuid.uuid4()),
        tdfp_id=30173,
        targets=[ExportTarget(printer_model="H2C", nozzles=["0.2", "0.4", "0.6", "0.8"])],
    )
    queue = f"profile-fetch-{uuid.uuid4().hex[:8]}"
    async with (
        temporal_client() as client,
        Worker(
            client,
            task_queue=queue,
            workflows=[ProfileFetch],
            activities=[export, import_, failed],
        ),
    ):
        handle = await client.start_workflow(
            ProfileFetch.run,
            ask,
            id=f"profile-fetch:{ask.run_id}",
            task_queue=queue,
        )
        assert await handle.result() == OUTCOME
        history = await handle.fetch_history()
    HISTORIES.mkdir(exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())
