"""TemplatePipeline over a pipeline's source (spec 2026-09-27 §3.4, §5.2, §5.3)."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.exceptions import TimeoutError as WorkflowTimeoutError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.job_models import PipelineOutput
from scadbuddy.render.projection import workflow_id_for
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows import pipelines
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.ctx import _references
from scadbuddy.workflows.models import (
    OutputRequest,
    PieceRequest,
    PieceResult,
    Projection,
    piece_key,
)
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline
from tests.support.pipelines import (
    FakeWorld,
    OldRenderPiece,
    a_job,
    activity_named,
    run_job,
)
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


async def test_progress_updates_land_in_the_order_they_were_made() -> None:
    world = FakeWorld(
        """\
async def run(ctx, inputs):
    a = await ctx.render("model.scad", w=1)
    ctx.progress("one", done=1, total=2)
    ctx.progress("two", done=2, total=2)
    await ctx.output(plates=await ctx.pack([a]), name="x")
"""
    )
    original = world.project

    async def slow_first(projection: Projection) -> None:
        if projection.steps and projection.steps[0].name == "one":
            await asyncio.sleep(1)  # the first write is the slower one
        await original(projection)

    world.project = activity_named("project", slow_first)  # type: ignore[method-assign]
    async with temporal_client() as client:
        await asyncio.wait_for(run_job(world, a_job(), client=client), timeout=60)
    progress = [
        p.steps[0].name
        for p in world.projections
        if p.state is None and p.steps and p.steps[0].name in ("one", "two")
    ]
    assert progress == ["one", "two"]


async def test_a_default_pipeline_output_refused_is_its_bare_error() -> None:
    world = FakeWorld()

    async def refused(req: OutputRequest) -> PipelineOutput:
        raise ApplicationError("the store is full", type="StoreFull", non_retryable=True)

    world.write_output = activity_named("write_output", refused)  # type: ignore[method-assign]
    async with temporal_client() as client:
        await asyncio.wait_for(run_job(world, a_job(params={"w": 3}), client=client), timeout=60)
    failure = world.final().failure
    assert failure is not None
    assert failure.error == "the store is full"  # no `<default pipeline>:N:`


async def test_a_piece_from_an_older_build_answers_its_waiter() -> None:
    """A `RenderPiece` still draining on the previous build sends its outcome without
    `piece_key`: the one piece being waited on takes it."""
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
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client,
            task_queue=queue,
            workflows=[TemplatePipeline, OldRenderPiece],
            activities=world.activities(),
        ):
            await client.start_workflow("RenderPiece", req, id=f"piece-{key}", task_queue=queue)
            await asyncio.wait_for(
                client.execute_workflow(
                    TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue
                ),
                timeout=30,
            )
    final = world.final()
    assert final.state == "done"
    assert final.log_tail == ["an older build"]
    assert world.pieces == []  # nothing rendered it again


TWO = """\
import asyncio

async def run(ctx, inputs):
    a, b = await asyncio.gather(ctx.render("model.scad", w=1), ctx.render("model.scad", w=2))
    name = f"{a.piece_key}={a.bbox.size[0]:g} {b.piece_key}={b.bbox.size[0]:g}"
    await ctx.output(plates=await ctx.pack([a, b]), name=name)
"""

#: The same two pieces, waited on in a known order: `w=1` first.
TWO_IN_ORDER = """\
import asyncio

async def run(ctx, inputs):
    first = asyncio.ensure_future(ctx.render("model.scad", w=1))
    await asyncio.sleep(1)
    b = await ctx.render("model.scad", w=2)
    a = await first
    name = f"{a.piece_key}={a.bbox.size[0]:g} {b.piece_key}={b.bbox.size[0]:g}"
    await ctx.output(plates=await ctx.pack([a, b]), name=name)
"""


@pytest.mark.parametrize("released", [(2, 1), (1, 2)])
async def test_a_job_waiting_on_two_pieces_gets_each_its_own(released: tuple[int, int]) -> None:
    """Job B waits on both of job A's pieces. Released in either order, each of B's
    Parts is its own piece's: an outcome routed by position fails one of the orders."""
    world = FakeWorld(TWO)
    gates = {1: asyncio.Event(), 2: asyncio.Event()}
    started: set[int] = set()
    original = world.cached_piece

    async def held(req: PieceRequest) -> PieceResult | None:
        w = int(req.params["w"])
        started.add(w)
        await gates[w].wait()
        return await original(req)

    world.cached_piece = activity_named("cached_piece", held)  # type: ignore[method-assign]
    a, b = a_job(), a_job()
    a.model_version = b.model_version = "abc1234"
    keys = {w: piece_key("demo", "abc1234", "model.scad", {"w": w}) for w in (1, 2)}
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client,
            task_queue=queue,
            workflows=[TemplatePipeline, RenderPiece],
            activities=world.activities(),
        ):
            ha = await client.start_workflow(
                TemplatePipeline.run, a, id=workflow_id_for(a.id), task_queue=queue
            )
            async with asyncio.timeout(30):
                while started != {1, 2}:
                    await asyncio.sleep(0.05)
                world.source = TWO_IN_ORDER  # B's pipeline; A has loaded its own
                hb = await client.start_workflow(
                    TemplatePipeline.run, b, id=workflow_id_for(b.id), task_queue=queue
                )
                for w in (1, 2):  # B waits on both of A's pieces
                    piece = client.get_workflow_handle(f"piece-{keys[w]}")
                    while not [
                        e
                        async for e in piece.fetch_history_events()
                        if e.HasField("workflow_execution_signaled_event_attributes")
                    ]:
                        await asyncio.sleep(0.05)
                first, second = released
                gates[first].set()
                await client.get_workflow_handle(f"piece-{keys[first]}").result()
                gates[second].set()
                await asyncio.gather(ha.result(), hb.result())
    names = {o.job_id: o.name for o in world.outputs}
    expected = f"{keys[1]}=1 {keys[2]}=2"
    assert names == {a.id: expected, b.id: expected}
    assert len(world.pieces) == 2  # rendered once each, by A


class _SlowSnapshots:
    """A snapshot store whose download outlasts `SHORT`, then lands the export."""

    def __init__(self, template: Path, paths: DataPaths, seconds: float) -> None:
        self.template, self.paths, self.seconds = template, paths, seconds

    async def materialize(self, slug: str, revision: str) -> bool:
        await asyncio.sleep(self.seconds)
        target = self.paths.model_revision_dir(slug, revision)
        if not target.is_dir():
            shutil.copytree(self.template, target)
        return True


async def test_a_snapshot_download_longer_than_short_still_loads_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_pipeline` is a job's first activity, and on the bambuddy store it brings in the
    revision's snapshot: its budget carries a transfer, as `prepare`'s does (final review I1).
    `SHORT` and `TRANSFER` are scaled down, so the download (4 s) outlasts `SHORT` (2 s)
    but not `SHORT + TRANSFER`; unsandboxed, so the workflow reads the patched values."""
    monkeypatch.setattr(pipelines, "SHORT", timedelta(seconds=2))
    monkeypatch.setattr(pipelines, "TRANSFER", timedelta(seconds=6))
    template = tmp_path / "template"
    template.mkdir()
    (template / "model.scad").write_text("cube();\n", encoding="utf-8")
    (template / "model.json").write_text('{"name": "Demo"}', encoding="utf-8")
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    deps = WorkerDeps(
        config=Config(data_dir=paths.root),
        paths=paths,
        assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs),
        refs=None,  # type: ignore[arg-type]
        projection=None,  # type: ignore[arg-type]
        snapshots=_SlowSnapshots(template, paths, 4.0),  # type: ignore[arg-type]
    )
    real = PipelineActivities(deps)
    world = FakeWorld()
    job = a_job(params={"w": 12})
    job.model_version = "a" * 40
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client,
            task_queue=queue,
            workflows=[TemplatePipeline, RenderPiece],
            activities=[a for a in world.activities() if a != world.load_pipeline]
            + [real.load_pipeline],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            await asyncio.wait_for(
                client.execute_workflow(
                    TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue
                ),
                90,
            )
    final = world.final()
    assert final.state == "done", final.failure
    assert final.pipeline_version == "default"


def test_the_pipeline_activities_budget_the_snapshot_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`migrate_inputs` shares `load_pipeline`'s bound; the migration workflow outlasts
    both attempts of it; `write_output` counts the snapshot among its moves."""
    assert pipelines._load_timeout() == pipelines.SHORT + pipelines.TRANSFER
    assert (
        2 * pipelines._load_timeout() + timedelta(seconds=1) + timedelta(seconds=10)
    ) == pipelines.MIGRATE_EXECUTION_TIMEOUT
    openscad = timedelta(seconds=100)
    monkeypatch.setattr(pipelines, "_openscad_timeout", lambda: openscad)
    req = OutputRequest.model_validate(
        {
            "job_id": "j",
            "index": 0,
            "slug": "demo",
            "layout": {"own": "k"},
            "parts": [],
            "name": None,
            "bom": [],
            "files": {"a.txt": "x", "b.bin": {"key": "act-1", "path": "b.bin"}},
            "record": {
                "revision": None,
                "ui_api": None,
                "pipeline_api": 1,
                "pipeline_version": "default",
                "inputs_v": 0,
                "plate_key": "default",
                "parts": [],
            },
        }
    )
    # The snapshot, one Blob, the publish.
    assert pipelines._output_timeout(req) == openscad + 3 * pipelines.TRANSFER
    # `ctx.activity` adds one transfer per Blob/Part it passes, plus the snapshot's.
    part, blob = {"kind": "part", "piece_key": "k"}, {"kind": "blob", "key": "b", "path": "f"}
    assert _references([[part, blob, 3], {"x": {"y": blob}, "n": "blob"}]) == 3
