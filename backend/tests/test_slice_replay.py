"""``SliceWorkflow`` replays the histories it wrote (plan
2026-10-16-native-slicer-rust-worker Task 11).

On the versioned ``library`` worker (#4090) a slice runs as ``VersionedSliceWorkflow``,
AUTO_UPGRADE: an open slice moves to the new build at its next workflow task and replays
there. So a change to the commands ``SliceWorkflow.run`` issues (an activity added,
reordered, or its options changed) goes behind ``workflow.patched`` and adds a history
recorded on the changed code beside these; this test is what says so.

``completed`` is a slice that prepares, slices and records; ``refused`` one whose
``slice_plate`` fails as a validation exit (192); ``prepared_again`` one whose first
``slice_plate`` refuses its URLs (``TransferRefused``), so it prepares once more and
slices; ``cancelled`` one cancelled while ``slice_plate`` runs, which records the row
and ends cancelled (review M4). Each was recorded with stub activities
(``SCADBUDDY_RECORD_HISTORIES=1`` runs the recording below). Never re-record them.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.slicing.models import (
    SLICER_ERROR,
    TRANSFER_REFUSED,
    VALIDATION_EXIT,
    SlicePlateInput,
    SlicePlateOutput,
    TransferUrl,
)
from scadbuddy.slicing.transfer import PendingPut
from scadbuddy.workflows.slicing import SliceWorkflow
from scadbuddy.workflows.slicing_models import (
    PREPARE_ACTIVITY,
    RECORD_ACTIVITY,
    SLICE_PLATE,
    SlicePrepared,
    SliceRecordInput,
    SliceWorkflowInput,
    SliceWorkflowResult,
)
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "slice_workflow_histories"
NAMES = ["completed", "refused", "prepared_again", "cancelled"]


@pytest.mark.parametrize("name", NAMES)
async def test_slice_workflow_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(f"slice-{name}", (HISTORIES / f"{name}.json").read_text())
    await Replayer(
        workflows=[SliceWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(history)


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


def _url(name: str) -> TransferUrl:
    return TransferUrl(
        url=f"http://scadbuddy/api/v1/internal/blobs/{name}",
        headers={"X-ScadBuddy-Transfer": "token"},
        expires_at=datetime(2026, 10, 18, 20, tzinfo=UTC),
    )


def _prepared() -> SlicePrepared:
    put = PendingPut(url=_url("pending-0"), pending_key="pending-0")
    return SlicePrepared(
        plate_input=SlicePlateInput(
            input_3mf=_url("slice-in"),
            plate=1,
            machine_preset=_url("slice-m"),
            process_preset=_url("slice-p"),
            filament_presets=[_url("slice-f")],
            sliced_3mf=put.url,
        ),
        put=put,
        reused_blob_key=None,
    )


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
@pytest.mark.parametrize("name", NAMES)
async def test_record(recording_temporal: str, name: str) -> None:
    calls = 0

    @activity.defn(name=PREPARE_ACTIVITY)
    async def prepare(input: SliceWorkflowInput) -> SlicePrepared:
        return _prepared()

    @activity.defn(name=SLICE_PLATE)
    async def slice_plate(input: SlicePlateInput) -> SlicePlateOutput:
        nonlocal calls
        calls += 1
        if name == "cancelled":
            await asyncio.sleep(3600)
        if name == "refused":
            raise ApplicationError(
                "validation failed",
                {"exit_code": VALIDATION_EXIT, "stderr_tail": "plate 99 is empty"},
                type=SLICER_ERROR,
                non_retryable=True,
            )
        if name == "prepared_again" and calls == 1:
            raise ApplicationError("expired", type=TRANSFER_REFUSED, non_retryable=True)
        return SlicePlateOutput(
            sliced_sha256="ab" * 32, sliced_size=10, exit_code=0, elapsed_seconds=1.0
        )

    @activity.defn(name=RECORD_ACTIVITY)
    async def record(input: SliceRecordInput) -> SliceWorkflowResult:
        return SliceWorkflowResult(
            status="cancelled" if input.cancelled else "failed" if input.failure else "completed",
            sliced_blob_key=None if input.failure else "slice-out",
            failure=input.failure,
            warnings=[],
        )

    client = await Client.connect(recording_temporal, data_converter=pydantic_data_converter)
    queue = f"slice-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[SliceWorkflow],
        activities=[prepare, slice_plate, record],
    ):
        handle = await client.start_workflow(
            SliceWorkflow.run,
            SliceWorkflowInput(
                slice_id=1,
                library_file_id=41,
                plan=SlicePlan(
                    printer_preset=PresetRef(source="standard", id="p"),
                    process_preset=PresetRef(source="standard", id="q"),
                    filament_presets=[PresetRef(source="standard", id="f")],
                    filament_colours=["#FF0000"],
                    bed_type="Textured PEI Plate",
                ),
                plate_id=1,
                preset_key="k",
                slicer_queue=queue,
            ),
            id=f"slice-{name}-{queue}",
            task_queue=queue,
            execution_timeout=timedelta(minutes=2),
        )
        if name == "cancelled":
            await asyncio.sleep(1)
            await handle.cancel()
            with pytest.raises(WorkflowFailureError):
                await handle.result()
        else:
            result: SliceWorkflowResult = await handle.result()
            assert result.status == ("failed" if name == "refused" else "completed")
        history = await handle.fetch_history()
    (HISTORIES / f"{name}.json").write_text(history.to_json())
