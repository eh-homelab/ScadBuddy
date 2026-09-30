"""TemplatePipeline over a pipeline's source (spec 2026-09-27 §3.4, §5.2, §5.3)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import TimeoutError as WorkflowTimeoutError
from temporalio.worker import Worker

from scadbuddy.render.projection import workflow_id_for
from scadbuddy.workflows import pipelines
from scadbuddy.workflows.models import PieceRequest, PieceResult, piece_key
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline
from tests.support.pipelines import FakeWorld, a_job, activity_named, run_job
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_temporal

THREE = """\
INPUTS_VERSION = 1
import asyncio

async def run(ctx, inputs):
    a, b, c = await asyncio.gather(
        ctx.render("model.scad", piece="wall", w=100, d=10),
        ctx.render("model.scad", piece="wall", w=100, d=10),
        ctx.render("parts/roof.scad", w=inputs["span"], d=50),
    )
    ctx.progress("Rendered 3 of 3", done=3, total=3)
    await ctx.output(plates=await ctx.pack([(a, 4), c]), name="house",
                     bom=[{"piece": "wall", "label": "Wall", "count": 4, "part": a.piece_key}],
                     files={"notes.txt": "hello"})
"""


async def test_the_default_pipeline_renders_one_piece_and_writes_it_as_rendered() -> None:
    world, job = FakeWorld(), a_job(params={"w": 12})
    async with temporal_client() as client:
        await run_job(world, job, client=client)
    assert [p.params for p in world.pieces] == [{"w": 12}]
    assert world.outputs[0].layout.own == world.pieces[0].piece_key
    final = world.final()
    assert final.state == "done"
    assert final.pipeline_version == "default"
    assert final.result is not None and final.outputs[0].result == final.result
    assert set(final.blob_keys) == {world.pieces[0].piece_key}
    assert final.log_tail == ["rendered model.scad"]


async def test_a_parameter_named_file_reaches_the_render() -> None:
    world, job = FakeWorld(), a_job(params={"file": "x", "name": "y", "w": 3})
    async with temporal_client() as client:
        await run_job(world, job, client=client)
    assert world.pieces[0].file == "model.scad"
    assert world.pieces[0].params == {"file": "x", "name": "y", "w": 3}
    assert world.final().state == "done"


async def test_identical_renders_share_one_piece_and_files_reach_the_output() -> None:
    world, job = FakeWorld(THREE), a_job(span=120)
    async with temporal_client() as client:
        await run_job(world, job, client=client)
    keys = {p.piece_key for p in world.pieces}
    assert len(keys) == 2 and len(world.pieces) == 2  # the identical walls rendered once
    # No revision: the pieces are the job's own (#642).
    assert piece_key("demo", f"job:{job.id}", "parts/roof.scad", {"w": 120, "d": 50}) in keys
    out = world.outputs[0]
    assert out.name == "house" and out.files == {"notes.txt": "hello"}
    assert sum(len(p.items) for p in out.layout.plates) == 5
    assert out.record.pipeline_version == world.final().pipeline_version != "default"
    assert out.record.inputs_v == 0 and sorted(out.record.parts) == sorted(keys)
    final = world.final()
    assert final.state == "done"
    # The job refs the parts the packed output was built from, not only its own blob.
    assert set(final.blob_keys) == keys | {f"output-{job.id}-0"}
    assert any(
        p.steps and p.steps[0].name == "Rendered 3 of 3"
        for p in world.projections
        if p.state is None
    )


async def test_a_failed_piece_fails_the_job_with_its_log() -> None:
    world, job = FakeWorld(THREE, fail="parts/roof.scad"), a_job(span=120)
    async with temporal_client() as client:
        await run_job(world, job, client=client)
    final = world.final()
    assert final.state == "failed"
    assert final.failure is not None
    assert final.failure.error == "parts/roof.scad: openscad exited with 1"
    assert final.failure.log_tail == ["ERROR: boom"]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "import datetime\n\nasync def run(ctx, inputs):\n    datetime.datetime.now()\n",
            "pipeline/pipeline.py:4: RestrictedWorkflowAccessError: ",
        ),
        (
            "async def run(ctx, inputs):\n    raise ValueError('no rooms')\n",
            "pipeline/pipeline.py:2: ValueError: no rooms",
        ),
        (
            "async def run(ctx, inputs):\n    await ctx.pack([], goal='fewest_swaps')\n",
            "pipeline/pipeline.py:2: ",
        ),
    ],
)
async def test_a_restricted_call_fails_the_job_with_its_line(source: str, message: str) -> None:
    world = FakeWorld(source)
    async with temporal_client() as client:
        await asyncio.wait_for(run_job(world, a_job(), client=client), timeout=60)
    final = world.final()
    assert final.state == "failed"
    assert final.failure is not None and final.failure.error.startswith(message)


async def test_a_pipeline_that_writes_nothing_fails() -> None:
    world = FakeWorld("async def run(ctx, inputs):\n    return None\n")
    async with temporal_client() as client:
        await run_job(world, a_job(), client=client)
    failure = world.final().failure
    assert failure is not None
    assert failure.error == "pipeline/pipeline.py: the pipeline wrote no output"


async def test_an_edit_mid_run_does_not_change_the_running_job() -> None:
    gate = asyncio.Event()
    slow = """\
async def run(ctx, inputs):
    a = await ctx.render("model.scad", w=1)
    b = await ctx.render("model.scad", w=2)
    await ctx.output(plates=await ctx.pack([a, b]), name="first source")
"""
    world, job = FakeWorld(slow), a_job()
    original = world.cached_piece
    started = asyncio.Event()

    # Annotated: temporalio converts the argument by its type hint (a bare `req` is a dict).
    async def held(req: PieceRequest) -> PieceResult | None:
        if req.params == {"w": 2} and not gate.is_set():
            started.set()  # before waiting: `original` records the piece only afterwards
            await gate.wait()
        return await original(req)

    world.cached_piece = activity_named("cached_piece", held)  # type: ignore[method-assign]
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client,
            task_queue=queue,
            workflows=[TemplatePipeline, RenderPiece],
            activities=world.activities(),
            max_cached_workflows=0,
        ):
            handle = await client.start_workflow(
                TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue
            )
            async with asyncio.timeout(30):
                await started.wait()
        # The worker is gone mid-house (its held attempt is cancelled at shutdown).
        # The template's pipeline changes on disk.
        world.source = "async def run(ctx, inputs):\n    raise RuntimeError('the new source ran')\n"
        gate.set()
        async with Worker(
            client,
            task_queue=queue,
            workflows=[TemplatePipeline, RenderPiece],
            activities=world.activities(),
        ):
            await asyncio.wait_for(handle.result(), timeout=60)
    assert world.final().state == "done"
    assert world.outputs[-1].name == "first source"
    assert [p.params for p in world.pieces].count({"w": 1}) == 1


async def test_a_waiter_trusts_a_piece_for_its_whole_retried_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`prepare` owns its transfers plus one schema export; a waiter re-checks only after
    every stage of the piece could have run out its retries."""
    openscad = timedelta(seconds=100)
    monkeypatch.setattr(pipelines, "_openscad_timeout", lambda: openscad)
    assert pipelines._prepare_timeout() == pipelines.PREPARE_TIMEOUT + openscad
    stages = [
        pipelines.CACHED_TIMEOUT,
        pipelines._prepare_timeout(),
        pipelines._main_timeout(),
        pipelines._solids_timeout(),
        pipelines.FINISH_TIMEOUT,
    ]
    # Three attempts each, 2 s then 4 s of backoff between them.
    retried = sum((3 * stage + timedelta(seconds=6) for stage in stages), timedelta())
    assert pipelines._waiter_recheck() == retried + pipelines.SHORT


async def test_ctx_activity_passes_parts_and_returns_json(tmp_path: Path) -> None:
    source = tmp_path / "activities.py"
    source.write_text("def count(parts, n):\n    return {'n': len(parts) * n}\n")
    world = FakeWorld(
        """\
async def run(ctx, inputs):
    a = await ctx.render("model.scad", w=1)
    got = await ctx.activity("count", [a, a], n=3)
    await ctx.output(plates=await ctx.pack([a]), name=str(got["n"]))
""",
        activities_py=source,
    )
    async with temporal_client() as client:
        await run_job(world, a_job(), client=client)
    assert world.outputs[0].name == "6"
    assert world.calls[0].args[0][0]["kind"] == "part"


#: Bounded CPU work that never yields: about a minute in CPython, 30x the SDK's 2 s
#: deadlock detector, so every workflow task fails and only the execution timeout
#: ends the run. Bounded, so no thread spins for the rest of the session. Should a
#: machine ever finish it early, the pipeline raises: the job fails, never `done`.
NEVER_YIELDS = (
    "async def run(ctx, inputs):\n"
    "    total = sum(i * i for i in range(10**9))\n"
    "    raise RuntimeError(f'finished early: {total}')\n"
)


async def test_a_pipeline_that_never_yields_times_out() -> None:
    world, job = FakeWorld(NEVER_YIELDS), a_job()
    timed_out = False
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client,
            task_queue=queue,
            workflows=[TemplatePipeline, RenderPiece],
            activities=world.activities(),
        ):
            try:
                await asyncio.wait_for(
                    client.execute_workflow(
                        TemplatePipeline.run,
                        job,
                        id=workflow_id_for(job.id),
                        task_queue=queue,
                        execution_timeout=timedelta(seconds=8),
                    ),
                    timeout=60,
                )
            except WorkflowFailureError as error:
                timed_out = isinstance(error.cause, WorkflowTimeoutError)
    settled = [p.state for p in world.projections if p.state in ("done", "failed")]
    # Either the timeout ended it (the expected path) or the pipeline failed the job
    # itself; never `done`.
    assert "done" not in settled
    assert timed_out or settled == ["failed"]
