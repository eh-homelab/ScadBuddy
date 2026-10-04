"""TemplatePipeline and RenderPiece against a Temporal dev server, with the openscad
activities replaced by fakes that record their calls."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ApplicationError, CancelledError, FailureError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import (
    CANCELLED_ERROR,
    SUPERSEDED_ERROR,
    Job,
    JobResult,
    PartInfo,
)
from scadbuddy.workflows.models import (
    ACCEPT_ACTIVITY,
    CLAIMS_ACTIVITY,
    QUEUE_FULL,
    RELEASE_UPDATE,
    AcceptRender,
    Failure,
    PieceRequest,
    PieceResult,
    PrepareResult,
    Projection,
    ReleaseAnswer,
    RenderAnswer,
    RenderMainResult,
    piece_key,
)
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline, _target_gone
from scadbuddy.workflows.print_models import ACCEPTED_UPDATE
from tests.support.renders import start_of, start_render
from tests.support.temporal import temporal_client

pytestmark = [pytest.mark.requires_temporal, pytest.mark.asyncio]

#: Jobs share a piece only at a revision: a live source is each job's own (#642).
REVISION = "abc1234"


class FakeActivities:
    """Same activity names as `RenderActivities`; records calls; `fail_main` makes the
    main render raise the way openscad does."""

    def __init__(
        self,
        *,
        fail_main: bool = False,
        block_main: asyncio.Event | None = None,
        block_solids: asyncio.Event | None = None,
        queue_full: int | None = None,
        block_cancelled: asyncio.Event | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.projections: list[Projection] = []
        self.claims: list[int] = []
        self.accepts = 0
        self.queue_full = queue_full
        self.block_cancelled = block_cancelled
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
        if projection.state == "cancelled" and self.block_cancelled is not None:
            await self.block_cancelled.wait()

    @activity.defn(name=ACCEPT_ACTIVITY)
    async def render_accept(self, accept: AcceptRender) -> Job:
        """The row, as the real one inserts it; the job id is the workflow id's tail,
        so a test that names its workflow `render-<job id>` knows the job's id."""
        self.accepts += 1
        if self.queue_full is not None:
            raise ApplicationError(
                "the render queue is full", self.queue_full, type=QUEUE_FULL, non_retryable=True
            )
        start = accept.start
        return Job(
            id=accept.workflow_id.removeprefix("render-"),
            slug=start.slug,
            params=start.params,
            inputs=start.inputs,
            model_version=start.model_version,
            created_at=datetime.now(UTC),
        )

    @activity.defn(name=CLAIMS_ACTIVITY)
    async def render_claims(self, job_id: str, claims: int) -> None:
        self.claims.append(claims)


def _job(revision: str | None = REVISION, **params: int) -> Job:
    return Job(
        id=uuid.uuid4().hex,
        slug="demo",
        params=dict(params),
        inputs={"params": dict(params)},
        model_version=revision,
        created_at=datetime.now(UTC),
    )


def _worker(
    client: Client, queue: str, acts: FakeActivities, *, max_concurrent_activities: int = 100
) -> Worker:
    return Worker(
        client,
        task_queue=queue,
        max_concurrent_activities=max_concurrent_activities,
        workflows=[TemplatePipeline, RenderPiece],
        activities=[
            acts.cached_piece,
            acts.prepare,
            acts.render_main,
            acts.render_solids,
            acts.finish_piece,
            acts.project,
            acts.render_accept,
            acts.render_claims,
        ],
    )


async def _until_the_piece_is_waited_on(client: Client, width: int) -> None:
    """Until a second job's `wait_for_me` has reached the piece."""
    piece = client.get_workflow_handle(
        f"piece-{piece_key('demo', REVISION, 'model.scad', {'width': width})}"
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
                TemplatePipeline.run, start_of(job), id=f"render-{job.id}", task_queue=queue
            )
        assert acts.calls == ["prepare", "render_main", "render_solids", "finish_piece"]
        states = [p.state for p in acts.projections if p.state]
        assert states == ["running", "done"]
        assert acts.projections[-1].result is not None
        assert acts.projections[-1].blob_key == piece_key(
            "demo", REVISION, "model.scad", {"width": 1}
        )


async def test_an_openscad_failure_projects_failed_with_the_log_tail() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities(fail_main=True)
        async with _worker(client, queue, acts):
            job = _job(width=999)
            await client.execute_workflow(
                TemplatePipeline.run, start_of(job), id=f"render-{job.id}", task_queue=queue
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
                TemplatePipeline.run, start_of(a), id=f"render-{a.id}", task_queue=queue
            )
            hb = await client.start_workflow(
                TemplatePipeline.run, start_of(b), id=f"render-{b.id}", task_queue=queue
            )
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            gate.set()
            await asyncio.gather(ha.result(), hb.result())
        assert acts.calls.count("render_main") == 1
        assert [p.state for p in acts.projections if p.state == "done"] == ["done", "done"]


async def test_two_revision_less_jobs_never_share_a_piece() -> None:
    """#642: a live source can change between them, so each renders its own."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities()
        async with _worker(client, queue, acts):
            a, b = _job(revision=None, width=6), _job(revision=None, width=6)
            await asyncio.gather(
                client.execute_workflow(
                    TemplatePipeline.run, start_of(a), id=f"render-{a.id}", task_queue=queue
                ),
                client.execute_workflow(
                    TemplatePipeline.run, start_of(b), id=f"render-{b.id}", task_queue=queue
                ),
            )
        assert acts.calls.count("render_main") == 2
        keys = {p.job_id: p.blob_key for p in acts.projections if p.state == "done"}
        assert keys == {
            job.id: piece_key("demo", f"job:{job.id}", "model.scad", {"width": 6}) for job in (a, b)
        }


async def test_cancelling_one_parent_leaves_a_shared_piece_running() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        async with _worker(client, queue, acts):
            a, b = _job(width=3), _job(width=3)
            ha = await client.start_workflow(
                TemplatePipeline.run, start_of(a), id=f"render-{a.id}", task_queue=queue
            )
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            # A owns the piece; B only waits on it.
            hb = await client.start_workflow(
                TemplatePipeline.run, start_of(b), id=f"render-{b.id}", task_queue=queue
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
        cancelled = [p for p in acts.projections if p.job_id == a.id and p.state][-1]
        assert cancelled.state == "cancelled"
        # The job's own step says so too, not the "running" it was projected with.
        assert cancelled.steps is not None and cancelled.steps[0].state == "cancelled"
        assert [p.state for p in acts.projections if p.job_id == b.id and p.state][-1] == "done"


async def test_a_piece_cancelled_by_hand_fails_the_job_that_owns_it() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        async with _worker(client, queue, acts):
            job = _job(width=21)
            handle = await client.start_workflow(
                TemplatePipeline.run, start_of(job), id=f"render-{job.id}", task_queue=queue
            )
            try:
                while "render_solids" not in acts.calls:
                    await asyncio.sleep(0.05)
                # An operator cancels the piece itself, not the job that owns it.
                piece_id = f"piece-{piece_key('demo', REVISION, 'model.scad', {'width': 21})}"
                await client.get_workflow_handle(piece_id).cancel()
                # The job was not cancelled: it settles and completes, it does not re-raise.
                await asyncio.wait_for(handle.result(), timeout=30)
            finally:
                gate.set()
        states = [p.state for p in acts.projections if p.job_id == job.id and p.state]
        assert states[-1] == "failed"
        assert "cancelled" not in states


async def test_a_job_waiting_on_a_failing_piece_projects_the_failure() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(fail_main=True, block_main=gate)
        async with _worker(client, queue, acts):
            a, b = _job(width=6), _job(width=6)
            ha = await client.start_workflow(
                TemplatePipeline.run, start_of(a), id=f"render-{a.id}", task_queue=queue
            )
            while "render_main" not in acts.calls:
                await asyncio.sleep(0.05)
            hb = await client.start_workflow(
                TemplatePipeline.run, start_of(b), id=f"render-{b.id}", task_queue=queue
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
                TemplatePipeline.run, start_of(a), id=f"render-{a.id}", task_queue=queue
            )
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            hb = await client.start_workflow(
                TemplatePipeline.run, start_of(b), id=f"render-{b.id}", task_queue=queue
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
                TemplatePipeline.run, start_of(job), id=f"render-{job.id}", task_queue=queue
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


async def test_an_unexpected_error_in_the_pipeline_projects_failed_and_closes_the_run() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities()
        async with _worker(client, queue, acts):
            job = _job(width=31)
            # Hand-authored inputs (spec §4.3): `params` present but not a mapping.
            job.inputs = {"params": None}
            handle = await client.start_workflow(
                TemplatePipeline.run, start_of(job), id=f"render-{job.id}", task_queue=queue
            )
            # Closed, not retried as a workflow task forever with the row at `running`.
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), timeout=30)
        last = [p for p in acts.projections if p.job_id == job.id and p.state][-1]
        assert last.state == "failed"
        assert last.failure is not None and last.failure.error.startswith("TypeError: ")
        assert last.steps is not None and last.steps[0].state == "failed"
        assert acts.calls == []


async def test_a_job_with_a_slug_the_api_would_refuse_projects_failed_unrendered() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities()
        async with _worker(client, queue, acts):
            job = _job(width=1).model_copy(update={"slug": "../../etc"})
            await client.execute_workflow(
                TemplatePipeline.run, start_of(job), id=f"render-{job.id}", task_queue=queue
            )
        assert acts.calls == []
        last = acts.projections[-1]
        assert last.state == "failed" and last.failure is not None
        assert "../../etc" in last.failure.error


@workflow.defn(name="ReturnsAtOnce")
class _ReturnsAtOnce:
    @workflow.run
    async def run(self) -> None:
        return None


@workflow.defn(name="SignalsAnother")
class _SignalsAnother:
    @workflow.run
    async def run(self, target: str) -> bool:
        try:
            await workflow.get_external_workflow_handle(target).signal("anything")
        except FailureError as error:
            return _target_gone(error)
        return False


async def test_signalling_a_closed_or_unknown_workflow_is_recognised_as_gone() -> None:
    """#857: `_target_gone` names an SDK error type; a Temporal bump that renames it
    would turn a waiter's retry on a closed piece into a failed job."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client,
            task_queue=queue,
            workflows=[_ReturnsAtOnce, _SignalsAnother],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            closed = f"closed-{uuid.uuid4().hex}"
            await client.execute_workflow(_ReturnsAtOnce.run, id=closed, task_queue=queue)
            for target in (closed, f"never-{uuid.uuid4().hex}"):
                assert await client.execute_workflow(
                    _SignalsAnother.run, target, id=f"signals-{uuid.uuid4().hex}", task_queue=queue
                )


async def test_a_second_accepted_coalesces_with_one_more_claim() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_main=gate)
        async with _worker(client, queue, acts):
            job = _job(width=50)
            wid = f"render-{job.id}"
            first = await start_render(client, queue, start_of(job), id=wid)
            second = await start_render(client, queue, start_of(job), id=wid)
            gate.set()
            await client.get_workflow_handle(wid).result()
        assert first.job is not None and not first.coalesced and first.job.claims == 1
        assert second.job is not None and second.coalesced and second.job.id == first.job.id
        assert second.job.claims == 2
        assert acts.accepts == 1 and acts.claims == [2]


async def test_release_of_one_of_two_claims_keeps_rendering() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_main=gate)
        async with _worker(client, queue, acts):
            job = _job(width=51)
            wid = f"render-{job.id}"
            await start_render(client, queue, start_of(job), id=wid)
            await start_render(client, queue, start_of(job), id=wid)
            handle = client.get_workflow_handle(wid)
            answer = await handle.execute_update(
                RELEASE_UPDATE, "superseded", result_type=ReleaseAnswer
            )
            gate.set()
            await handle.result()
        assert answer.cancelled is None
        assert acts.claims == [2, 1]
        assert [p.state for p in acts.projections if p.state][-1] == "done"


async def test_the_last_release_cancels_and_projects_cancelled_with_its_reason() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        async with _worker(client, queue, acts):
            job = _job(width=52)
            wid = f"render-{job.id}"
            await start_render(client, queue, start_of(job), id=wid)
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            handle = client.get_workflow_handle(wid)
            answer = await handle.execute_update(
                RELEASE_UPDATE, "superseded", result_type=ReleaseAnswer
            )
            # The run completes: a released job is an outcome, not a failure.
            await asyncio.wait_for(handle.result(), timeout=30)
            piece = client.get_workflow_handle(
                f"piece-{piece_key('demo', REVISION, 'model.scad', {'width': 52})}"
            )
            assert (await piece.describe()).status == WorkflowExecutionStatus.RUNNING
            gate.set()
            await piece.result()
        assert answer.cancelled is not None and answer.cancelled.state == "cancelled"
        last = [p for p in acts.projections if p.state][-1]
        assert last.state == "cancelled"
        assert last.failure is not None and last.failure.error == SUPERSEDED_ERROR


async def test_accepted_after_the_last_release_answers_closing() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate, projecting = asyncio.Event(), asyncio.Event()
        acts = FakeActivities(block_solids=gate, block_cancelled=projecting)
        async with _worker(client, queue, acts):
            job = _job(width=53)
            wid = f"render-{job.id}"
            await start_render(client, queue, start_of(job), id=wid)
            handle = client.get_workflow_handle(wid)
            # The release waits for the cancelled render to project (held here); an
            # `accepted` sent meanwhile reaches the same, closing, execution.
            release = asyncio.create_task(
                handle.execute_update(RELEASE_UPDATE, "cancelled", result_type=ReleaseAnswer)
            )
            while not [p for p in acts.projections if p.state == "cancelled"]:
                await asyncio.sleep(0.01)
            late = await handle.execute_update(ACCEPTED_UPDATE, result_type=RenderAnswer)
            projecting.set()
            await release
            await handle.result()
            gate.set()
        assert late.closing and late.job is None
        last = [p for p in acts.projections if p.state][-1]
        assert last.failure is not None and last.failure.error == CANCELLED_ERROR


async def test_a_full_queue_answers_queue_full_and_fails_the_execution() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities(queue_full=3)
        async with _worker(client, queue, acts):
            job = _job(width=54)
            wid = f"render-{job.id}"
            answer = await start_render(client, queue, start_of(job), id=wid)
            with pytest.raises(WorkflowFailureError):
                await client.get_workflow_handle(wid).result()
        assert answer.queue_full == 3 and answer.job is None
        assert acts.projections == [] and acts.calls == []


async def test_a_release_projects_cancelled_while_every_activity_slot_is_busy() -> None:
    """The worker's one activity slot holds the piece's openscad run (ABANDONed, so it
    goes on): the cancelled projection must not wait behind it (review I1)."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        async with _worker(client, queue, acts, max_concurrent_activities=1):
            job = _job(width=uuid.uuid4().int % 10**9)
            wid = f"render-{job.id}"
            await start_render(client, queue, start_of(job), id=wid)
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            handle = client.get_workflow_handle(wid)
            try:
                answer = await asyncio.wait_for(
                    handle.execute_update(RELEASE_UPDATE, "superseded", result_type=ReleaseAnswer),
                    timeout=10,
                )
                await asyncio.wait_for(handle.result(), timeout=10)
            finally:
                gate.set()
        assert answer.cancelled is not None
        assert [p for p in acts.projections if p.state][-1].state == "cancelled"


async def test_a_job_input_from_an_older_build_still_renders() -> None:
    """An old API pod's start (a `Job`, its row already inserted) reaching the new
    build during a rolling deploy (review I2)."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities()
        async with _worker(client, queue, acts):
            job = _job(width=uuid.uuid4().int % 10**9)
            await client.execute_workflow(
                TemplatePipeline.run,
                job,
                id=f"render-{job.id}",
                task_queue=queue,
            )
        assert acts.accepts == 0
        last = [p for p in acts.projections if p.state][-1]
        assert last.state == "done" and last.job_id == job.id


async def test_a_release_right_after_the_start_cancels_the_job() -> None:
    """The release lands while the job's first projection is in flight: that write
    completing anyway must not swallow the release."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_main=gate)
        async with _worker(client, queue, acts):
            job = _job(width=uuid.uuid4().int % 10**9)
            wid = f"render-{job.id}"
            await start_render(client, queue, start_of(job), id=wid)
            handle = client.get_workflow_handle(wid)
            try:
                answer = await asyncio.wait_for(
                    handle.execute_update(RELEASE_UPDATE, "cancelled", result_type=ReleaseAnswer),
                    timeout=10,
                )
                await asyncio.wait_for(handle.result(), timeout=10)
            finally:
                gate.set()
        assert answer.cancelled is not None
        last = [p for p in acts.projections if p.state][-1]
        assert last.state == "cancelled" and last.failure is not None
        assert last.failure.error == CANCELLED_ERROR
