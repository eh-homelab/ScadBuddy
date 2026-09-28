"""TemplatePipeline and RenderPiece against a Temporal dev server, with the openscad
activities replaced by fakes that record their calls."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.worker import Worker

from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import Job, JobResult, PartInfo
from scadbuddy.workflows.models import (
    Failure,
    PieceRequest,
    PieceResult,
    PrepareResult,
    Projection,
    RenderMainResult,
    piece_key,
)
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline
from tests.support.temporal import temporal_client

pytestmark = [pytest.mark.requires_temporal, pytest.mark.asyncio]


class FakeActivities:
    """Same activity names as `RenderActivities`; records calls; `fail_main` makes the
    main render raise the way openscad does."""

    def __init__(
        self,
        *,
        fail_main: bool = False,
        block_main: asyncio.Event | None = None,
        block_solids: asyncio.Event | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.projections: list[Projection] = []
        self.fail_main = fail_main
        self.block_main = block_main
        self.block_solids = block_solids

    @activity.defn(name="cached_piece")
    async def cached_piece(self, req: PieceRequest) -> PieceResult | None:
        return None

    @activity.defn(name="prepare")
    async def prepare(self, req: PieceRequest) -> PrepareResult:
        self.calls.append("prepare")
        return PrepareResult(
            version="v1",
            scad=f"models/{req.slug}/model.scad",
            library_path=[],
            schema_cache="cache/x",
        )

    @activity.defn(name="render_main")
    async def render_main(self, req: PieceRequest, prepared: PrepareResult) -> RenderMainResult:
        self.calls.append("render_main")
        if self.block_main is not None:
            await self.block_main.wait()
        if self.fail_main:
            raise ApplicationError(
                "openscad exited with 1",
                Failure(error="openscad exited with 1", log_tail=["ERROR: boom"]),
                type="OpenSCADError",
                non_retryable=True,
            )
        return RenderMainResult(
            plates=1,
            log_tail=["fine"],
            diagnostics=[],
            diagnostics_dropped=0,
            notes=[],
            missing_files=[],
        )

    @activity.defn(name="render_solids")
    async def render_solids(
        self, req: PieceRequest, prepared: PrepareResult, main: RenderMainResult
    ) -> None:
        self.calls.append("render_solids")
        if self.block_solids is not None:
            await self.block_solids.wait()

    @activity.defn(name="finish_piece")
    async def finish_piece(
        self, req: PieceRequest, prepared: PrepareResult, main: RenderMainResult
    ) -> PieceResult:
        self.calls.append("finish_piece")
        return PieceResult(
            result=JobResult(
                model_3mf=f"blobs/{req.piece_key}/model.3mf",
                preview_glb=f"blobs/{req.piece_key}/preview.glb",
                parts=[PartInfo(name="Color 1", colour="#FF0000", extruder=1, watertight=True)],
                bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
            ),
            log_tail=main.log_tail,
        )

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        self.projections.append(projection)


def _job(**params: int) -> Job:
    return Job(
        id=uuid.uuid4().hex,
        slug="demo",
        params=dict(params),
        inputs={"params": dict(params)},
        created_at=datetime.now(UTC),
    )


def _worker(client: Client, queue: str, acts: FakeActivities) -> Worker:
    return Worker(
        client,
        task_queue=queue,
        workflows=[TemplatePipeline, RenderPiece],
        activities=[
            acts.cached_piece,
            acts.prepare,
            acts.render_main,
            acts.render_solids,
            acts.finish_piece,
            acts.project,
        ],
    )


async def _until_the_piece_is_waited_on(client: Client, width: int) -> None:
    """Until a second job's `wait_for_me` has reached the piece."""
    piece = client.get_workflow_handle(
        f"piece-{piece_key('demo', None, 'model.scad', {'width': width})}"
    )
    while not [
        e
        async for e in piece.fetch_history_events()
        if e.HasField("workflow_execution_signaled_event_attributes")
    ]:
        await asyncio.sleep(0.05)


async def test_a_default_render_runs_the_four_stages_and_projects_done() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities()
        async with _worker(client, queue, acts):
            job = _job(width=1)
            await client.execute_workflow(
                TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue
            )
        assert acts.calls == ["prepare", "render_main", "render_solids", "finish_piece"]
        states = [p.state for p in acts.projections if p.state]
        assert states == ["running", "done"]
        assert acts.projections[-1].result is not None
        assert acts.projections[-1].blob_key == piece_key("demo", None, "model.scad", {"width": 1})


async def test_an_openscad_failure_projects_failed_with_the_log_tail() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities(fail_main=True)
        async with _worker(client, queue, acts):
            job = _job(width=999)
            await client.execute_workflow(
                TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue
            )
        assert acts.projections[0].state == "running"
        last = acts.projections[-1]
        assert last.state == "failed" and last.failure is not None
        assert last.failure.log_tail == ["ERROR: boom"]
        assert acts.calls == ["prepare", "render_main"]


async def test_identical_pieces_render_once_across_two_jobs() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        async with _worker(client, queue, acts):
            a, b = _job(width=2), _job(width=2)
            ha = await client.start_workflow(
                TemplatePipeline.run, a, id=f"render-{a.id}", task_queue=queue
            )
            hb = await client.start_workflow(
                TemplatePipeline.run, b, id=f"render-{b.id}", task_queue=queue
            )
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            gate.set()
            await asyncio.gather(ha.result(), hb.result())
        assert acts.calls.count("render_main") == 1
        assert [p.state for p in acts.projections if p.state == "done"] == ["done", "done"]


async def test_cancelling_one_parent_leaves_a_shared_piece_running() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        async with _worker(client, queue, acts):
            a, b = _job(width=3), _job(width=3)
            ha = await client.start_workflow(
                TemplatePipeline.run, a, id=f"render-{a.id}", task_queue=queue
            )
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            # A owns the piece; B only waits on it.
            hb = await client.start_workflow(
                TemplatePipeline.run, b, id=f"render-{b.id}", task_queue=queue
            )
            await _until_the_piece_is_waited_on(client, 3)
            await ha.cancel()
            with pytest.raises(WorkflowFailureError) as raised:
                await ha.result()
            assert isinstance(raised.value.cause, CancelledError)
            gate.set()
            await hb.result()
        assert acts.calls.count("render_main") == 1
        assert acts.calls.count("finish_piece") == 1
        assert [p.state for p in acts.projections if p.job_id == a.id and p.state][
            -1
        ] == "cancelled"
        assert [p.state for p in acts.projections if p.job_id == b.id and p.state][-1] == "done"


async def test_a_job_waiting_on_a_failing_piece_projects_the_failure() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(fail_main=True, block_main=gate)
        async with _worker(client, queue, acts):
            a, b = _job(width=6), _job(width=6)
            ha = await client.start_workflow(
                TemplatePipeline.run, a, id=f"render-{a.id}", task_queue=queue
            )
            while "render_main" not in acts.calls:
                await asyncio.sleep(0.05)
            hb = await client.start_workflow(
                TemplatePipeline.run, b, id=f"render-{b.id}", task_queue=queue
            )
            await _until_the_piece_is_waited_on(client, 6)
            gate.set()
            await asyncio.gather(ha.result(), hb.result())
        assert acts.calls.count("render_main") == 1
        last_b = [p for p in acts.projections if p.job_id == b.id and p.state][-1]
        assert last_b.state == "failed" and last_b.failure is not None
        assert last_b.failure.log_tail == ["ERROR: boom"]


async def test_cancelling_a_job_that_waits_on_another_jobs_piece_leaves_the_piece_running() -> None:
    """The second job only signalled the piece; the piece's signal back to it after
    the cancel fails and must not fail the piece."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        async with _worker(client, queue, acts):
            a, b = _job(width=5), _job(width=5)
            ha = await client.start_workflow(
                TemplatePipeline.run, a, id=f"render-{a.id}", task_queue=queue
            )
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            hb = await client.start_workflow(
                TemplatePipeline.run, b, id=f"render-{b.id}", task_queue=queue
            )
            await _until_the_piece_is_waited_on(client, 5)
            await hb.cancel()
            with pytest.raises(WorkflowFailureError) as raised:
                await hb.result()
            assert isinstance(raised.value.cause, CancelledError)
            gate.set()
            await ha.result()
        assert acts.calls.count("render_main") == 1
        assert [p.state for p in acts.projections if p.job_id == a.id and p.state][-1] == "done"
        assert [p.state for p in acts.projections if p.job_id == b.id and p.state][
            -1
        ] == "cancelled"


async def test_a_piece_resumes_from_the_activity_it_was_on() -> None:
    """Worker dies after render_main: a new worker finishes without re-running it."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        first = FakeActivities(block_solids=gate)
        job = _job(width=4)
        handle = None
        async with _worker(client, queue, first):
            handle = await client.start_workflow(
                TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue
            )
            while "render_solids" not in first.calls:
                await asyncio.sleep(0.05)
        # first worker gone mid-solids; a second one picks the activity up
        second = FakeActivities()
        async with _worker(client, queue, second):
            assert handle is not None
            await handle.result()
        assert "render_main" not in second.calls
        assert second.calls == ["render_solids", "finish_piece"]


async def test_a_job_with_a_slug_the_api_would_refuse_projects_failed_unrendered() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities()
        async with _worker(client, queue, acts):
            job = _job(width=1).model_copy(update={"slug": "../../etc"})
            await client.execute_workflow(
                TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue
            )
        assert acts.calls == []
        last = acts.projections[-1]
        assert last.state == "failed" and last.failure is not None
        assert "../../etc" in last.failure.error
