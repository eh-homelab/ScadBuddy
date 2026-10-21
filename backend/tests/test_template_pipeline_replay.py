"""``TemplatePipeline`` replays the histories it wrote (#4455).

The in-process worker (``SCADBUDDY_TEMPORAL_WORKER_INPROCESS``) is unversioned, so a
run it started on an older build replays on the new one. A change to the commands
``TemplatePipeline`` issues therefore goes behind ``workflow.patched``, and these
histories are what say so.

``before_lookup`` is a job with a revision recorded on the build before #4438
(3b2dcf930f^, the commit before the job's own ``cached_piece`` lookup): its piece's
child starts where ``piece-lookup-4199``'s marker would now be, so it replays only
while ``TemplatePipeline._cached`` keeps the lookup behind that guard. ``cached`` is
the same job recorded after it: the marker, the local lookup that finds the piece, and
no child. Each was recorded against the dev server with ``FakeWorld``'s activities
(``SCADBUDDY_RECORD_HISTORIES=1`` runs the recording below; ``before_lookup`` only on
a build without ``PIECE_LOOKUP_PATCH``). Never re-record them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from scadbuddy.render.projection import workflow_id_for
from scadbuddy.workflows import pipelines
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.support.pipelines import FakeWorld, a_job, run_job
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "template_pipeline_histories"
NAMES = ["before_lookup", "cached"]
LOOKUP_PATCH = b"piece-lookup-4199"


def _history(name: str) -> WorkflowHistory:
    return WorkflowHistory.from_json(f"render-{name}", (HISTORIES / f"{name}.json").read_text())


def _marker_data(history: WorkflowHistory) -> list[bytes]:
    return [
        payload.data
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
        for details in event.marker_recorded_event_attributes.details.values()
        for payload in details.payloads
    ]


def _children(history: WorkflowHistory) -> int:
    return sum(
        1
        for event in history.events
        if event.HasField("start_child_workflow_execution_initiated_event_attributes")
    )


@pytest.mark.parametrize("name", NAMES)
async def test_template_pipeline_replays_its_recorded_history(name: str) -> None:
    await Replayer(
        workflows=[TemplatePipeline], data_converter=pydantic_data_converter
    ).replay_workflow(_history(name))


def test_the_histories_are_the_two_sides_of_the_lookup_guard() -> None:
    """What makes ``before_lookup`` worth replaying: a child start and no marker."""
    before, cached = _history("before_lookup"), _history("cached")
    assert _children(before) == 1
    assert not any(LOOKUP_PATCH in data for data in _marker_data(before))
    assert _children(cached) == 0
    assert any(LOOKUP_PATCH in data for data in _marker_data(cached))


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
@pytest.mark.parametrize("name", NAMES)
async def test_record(recording_temporal: str, name: str) -> None:
    if (name == "before_lookup") == hasattr(pipelines, "PIECE_LOOKUP_PATCH"):
        when = "before" if name == "before_lookup" else "with"
        pytest.skip(f"{name} is recorded on a build {when} #4438")
    world, job = FakeWorld(), a_job(params={"w": 12})
    job.model_version = "a" * 40
    client = await Client.connect(recording_temporal, data_converter=pydantic_data_converter)
    await run_job(world, job, client=client)
    assert world.final().state == "done"
    history = await client.get_workflow_handle(workflow_id_for(job.id)).fetch_history()
    HISTORIES.mkdir(exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())
