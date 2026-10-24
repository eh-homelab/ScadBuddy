"""``TemplatePipeline`` replays the histories an older build wrote (#4199, #4455).

The render worker is PINNED, but the in-process worker
(``SCADBUDDY_TEMPORAL_WORKER_INPROCESS``) is unversioned: a run it started on an older
build replays on the new one after a restart. So a change to the commands
``TemplatePipeline`` issues goes behind ``workflow.patched`` and keeps these replaying.

``before_lookup`` is a cached render from before the job looked its piece up itself
(``piece-lookup-4199``): it starts the piece's ``RenderPiece`` child and has no lookup
marker. Recorded with ``FakeWorld`` on the build before that lookup
(``SCADBUDDY_RECORD_HISTORIES=1`` runs the recording below). Never re-record it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from scadbuddy.render.projection import workflow_id_for
from scadbuddy.workflows.pipelines import PIECE_LOOKUP_PATCH, RenderPiece, TemplatePipeline
from tests.support.pipelines import FakeWorld, a_job, run_job
from tests.support.temporal import temporal_client

HISTORIES = Path(__file__).parent / "fixtures" / "template_pipeline_histories"
NAMES = ["before_lookup"]


@pytest.mark.parametrize("name", NAMES)
async def test_template_pipeline_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(f"render-{name}", (HISTORIES / f"{name}.json").read_text())
    await Replayer(
        workflows=[TemplatePipeline, RenderPiece], data_converter=pydantic_data_converter
    ).replay_workflow(history)


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
async def test_record_before_lookup() -> None:
    world, job = FakeWorld(), a_job(params={"w": 12})
    job.model_version = "a" * 40
    async with temporal_client() as client:
        await run_job(world, job, client=client)
        history = await client.get_workflow_handle(workflow_id_for(job.id)).fetch_history()
    markers = [
        event.marker_recorded_event_attributes.marker_name
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
    ]
    if "core_patch" in markers:
        pytest.fail(
            f"this build already applies {PIECE_LOOKUP_PATCH}: recording now would overwrite "
            "before_lookup.json with a history that carries the patch marker, and "
            "test_template_pipeline_replays_its_recorded_history would stop covering the "
            "unpatched branch. Only ever record this on the commit before that patch landed."
        )
    HISTORIES.mkdir(parents=True, exist_ok=True)
    (HISTORIES / "before_lookup.json").write_text(history.to_json())
