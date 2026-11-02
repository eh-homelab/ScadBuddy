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
a build without ``PIECE_LOOKUP_PATCH``, and neither over its file). Never re-record them.
``waiting`` (below) is recorded the same way, on a build without ``PIECE_TIMEOUT_PATCH``.
The recording tests skip on every build once their files exist: they stay only as the
recipe that recorded them.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from scadbuddy.render.projection import workflow_id_for
from scadbuddy.workflows import pipelines
from scadbuddy.workflows.models import PieceRequest, PieceResult, piece_key
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline
from tests.support.pipelines import (
    FakeWorld,
    a_job,
    activity_named,
    child_starts,
    local_activities,
    patch_ids,
    run_job,
)
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "template_pipeline_histories"
NAMES = ["before_lookup", "cached"]
#: The pre-#4438 history is recorded only on a build without the lookup, the other only
#: on one with it, and neither over a recorded one.
LOOKUP_BUILD = hasattr(pipelines, "PIECE_LOOKUP_PATCH")
_OTHER_BUILD = "before" if LOOKUP_BUILD else "with"
RECORDABLE = [
    pytest.param(
        name,
        marks=[
            pytest.mark.skipif(
                (name == "before_lookup") == LOOKUP_BUILD,
                reason=f"{name} is recorded on a build {_OTHER_BUILD} #4438",
            ),
            pytest.mark.skipif(
                (HISTORIES / f"{name}.json").exists(),
                reason=f"{name} is recorded; never re-record it",
            ),
        ],
    )
    for name in NAMES
]


def _history(name: str) -> WorkflowHistory:
    return WorkflowHistory.from_json(f"render-{name}", (HISTORIES / f"{name}.json").read_text())


@pytest.mark.parametrize("name", NAMES)
async def test_template_pipeline_replays_its_recorded_history(name: str) -> None:
    await Replayer(
        workflows=[TemplatePipeline], data_converter=pydantic_data_converter
    ).replay_workflow(_history(name))


def test_the_histories_are_the_two_sides_of_the_lookup_guard() -> None:
    """What makes ``before_lookup`` worth replaying: a child start and no marker."""
    before, cached = _history("before_lookup"), _history("cached")
    assert len(child_starts(before)) == 1
    # The literal, not the constant: replay pins the marker the recorded histories
    # carry, and a check against the constant would follow a rename that breaks it.
    assert "piece-lookup-4199" not in patch_ids(before)
    assert "cached_piece" not in local_activities(before)
    assert child_starts(cached) == []
    assert "piece-lookup-4199" in patch_ids(cached)
    assert "cached_piece" in local_activities(cached)


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
@pytest.mark.parametrize("name", RECORDABLE)
async def test_record(recording_temporal: str, name: str) -> None:
    world, job = FakeWorld(), a_job(params={"w": 12})
    job.model_version = "a" * 40
    client = await Client.connect(recording_temporal, data_converter=pydantic_data_converter)
    await run_job(world, job, client=client)
    assert world.final().state == "done"
    history = await client.get_workflow_handle(workflow_id_for(job.id)).fetch_history()
    HISTORIES.mkdir(exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json() + "\n")


#: ``waiting`` is a job that joined another's running piece, recorded on main before
#: #5191 (cb40179db, no ``PIECE_TIMEOUT_PATCH``): it signals ``wait_for_me`` and starts its
#: wait's timer at ``_waiter_recheck``'s length, which the piece's outcome then ends.
TIMEOUT_BUILD = hasattr(pipelines, "PIECE_TIMEOUT_PATCH")


def _waiting() -> WorkflowHistory:
    return WorkflowHistory.from_json(
        "render-waiting", (HISTORIES / "waiting.json").read_text(encoding="utf-8")
    )


def _timers(history: WorkflowHistory) -> list[timedelta]:
    return [
        event.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
        for event in history.events
        if event.HasField("timer_started_event_attributes")
    ]


async def test_a_waiter_from_before_the_piece_timeout_keeps_its_old_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#5191 review: a run with no ``piece-timeout-5104`` marker replays through
    ``_waiter_wait``'s unpatched branch, a waiter's timer pending, and waits
    ``_waiter_recheck`` as it did, never ``PIECE_RECHECK``. Unsandboxed, so the spy sees
    the replay's calls; the timer's length is no part of what replay checks."""
    history = _waiting()
    assert "piece-timeout-5104" not in patch_ids(history)
    assert len(child_starts(history)) == 1  # refused: the other job's piece was running
    waits: list[timedelta] = []
    real = pipelines._waiter_wait

    def spied() -> timedelta:
        waits.append(real())
        return waits[-1]

    monkeypatch.setattr(pipelines, "_waiter_wait", spied)
    await Replayer(
        workflows=[TemplatePipeline],
        data_converter=pydantic_data_converter,
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history)
    assert _timers(history) == waits  # one wait, the length the old build waited
    # `_waiter_recheck` at the memo's default, as the recorded job had no memo; read
    # outside a workflow, so the memo read is replaced by its default.
    monkeypatch.setattr(pipelines, "_openscad_timeout", lambda: timedelta(seconds=180))
    assert waits == [pipelines._waiter_recheck()]
    assert waits[0] > pipelines.PIECE_RECHECK


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
@pytest.mark.skipif(TIMEOUT_BUILD, reason="waiting is recorded on a build before #5191")
@pytest.mark.skipif((HISTORIES / "waiting.json").exists(), reason="never re-record it")
async def test_record_waiting(recording_temporal: str) -> None:
    """Skipped on every build now, as `test_record` is: kept only as the recipe that
    recorded ``waiting.json``, run on cb40179db's ``pipelines.py`` with
    ``SCADBUDDY_RECORD_HISTORIES=1``. Never re-record it."""
    world, job = FakeWorld(), a_job(params={"w": 12})
    key = piece_key("demo", f"job:{job.id}", "model.scad", {"w": 12})
    req = PieceRequest(
        slug="demo",
        revision=None,
        scope=f"job:{job.id}",
        file="model.scad",
        params={"w": 12},
        piece_key=key,
    )
    gate = asyncio.Event()
    original = world.cached_piece

    async def held(piece: PieceRequest) -> PieceResult | None:
        await gate.wait()
        return await original(piece)

    world.cached_piece = activity_named("cached_piece", held)  # type: ignore[method-assign]
    client = await Client.connect(recording_temporal, data_converter=pydantic_data_converter)
    queue = f"t-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[TemplatePipeline, RenderPiece],
        activities=world.activities(),
    ):
        piece = await client.start_workflow("RenderPiece", req, id=f"piece-{key}", task_queue=queue)
        handle = await client.start_workflow(
            TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue
        )
        async with asyncio.timeout(30):  # the job waits on the piece
            while not [
                e
                async for e in piece.fetch_history_events()
                if e.HasField("workflow_execution_signaled_event_attributes")
            ]:
                await asyncio.sleep(0.05)
        gate.set()
        await asyncio.wait_for(handle.result(), 30)
    assert world.final().state == "done"
    history = await handle.fetch_history()
    (HISTORIES / "waiting.json").write_text(history.to_json() + "\n", encoding="utf-8")
