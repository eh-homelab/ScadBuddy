"""TemplatePipeline and RenderPiece against a Temporal dev server, with the openscad
activities replaced by fakes that record their calls."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from temporalio import activity, workflow
from temporalio.bridge.proto.workflow_activation import WorkflowActivation
from temporalio.bridge.proto.workflow_completion import WorkflowActivationCompletion
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowUpdateFailedError,
)
from temporalio.exceptions import ApplicationError, CancelledError, FailureError
from temporalio.worker import (
    UnsandboxedWorkflowRunner,
    Worker,
    WorkflowInstance,
    WorkflowInstanceDetails,
    WorkflowRunner,
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import (
    CANCELLED_ERROR,
    SUPERSEDED_ERROR,
    Job,
    JobResult,
    PartInfo,
    PipelineOutput,
)
from scadbuddy.workflows.models import (
    ACCEPT_ACTIVITY,
    CLAIMS_ACTIVITY,
    CLOSING,
    QUEUE_FULL,
    RELEASE_UPDATE,
    AcceptRender,
    Failure,
    Layout,
    LoadedPipeline,
    LoadRequest,
    OutputRequest,
    PackRequest,
    PieceRequest,
    PieceResult,
    PrepareResult,
    Projection,
    ReleaseAnswer,
    RenderAnswer,
    RenderMainResult,
    piece_key,
)
from scadbuddy.workflows.pipeline_activities import pack_layout
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline, _target_gone
from scadbuddy.workflows.print_models import ACCEPTED_UPDATE
from tests.support.pipelines import FakeWorld, fake_output
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
        block_claims: asyncio.Event | None = None,
        fail_running: asyncio.Event | None = None,
        fail_solids: bool = False,
        settled_rows: frozenset[str] = frozenset(),
    ) -> None:
        self.calls: list[str] = []
        #: Rows the API settled before the run's first step (#603).
        self.settled_rows = settled_rows
        self.projections: list[Projection] = []
        self.claims: list[int] = []
        self.accepts = 0
        self.queue_full = queue_full
        self.block_cancelled = block_cancelled
        self.block_claims = block_claims
        self.fail_running = fail_running
        self.claiming = 0
        self.fail_main = fail_main
        self.block_main = block_main
        self.block_solids = block_solids
        self.fail_solids = fail_solids

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
        if self.fail_solids:
            # What a BadZipFile looks like once its retries are spent (#952).
            raise ApplicationError("File is not a zip file", type="BadZipFile", non_retryable=True)

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
    async def project(self, projection: Projection) -> bool:
        self.projections.append(projection)
        if projection.state == "running" and self.fail_running is not None:
            await self.fail_running.wait()
            raise ApplicationError("the database went away", non_retryable=True)
        if projection.state == "cancelled" and self.block_cancelled is not None:
            await self.block_cancelled.wait()
        return projection.job_id not in self.settled_rows

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
        self.claiming += 1
        if self.block_claims is not None:
            await self.block_claims.wait()
        self.claims.append(claims)

    # The built-in pipeline's own activities (spec §5.3), as `FakeWorld` has them.
    @activity.defn(name="load_pipeline")
    async def load_pipeline(self, req: LoadRequest) -> LoadedPipeline:
        return await FakeWorld().load_pipeline(req)

    @activity.defn(name="pack")
    async def pack(self, req: PackRequest) -> Layout:
        return pack_layout(req)

    @activity.defn(name="write_output")
    async def write_output(self, req: OutputRequest) -> PipelineOutput:
        return fake_output(req)


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
    client: Client,
    queue: str,
    acts: FakeActivities,
    runner: WorkflowRunner | None = None,
    *,
    max_concurrent_activities: int = 100,
) -> Worker:
    worker = Worker(
        client,
        task_queue=queue,
        max_concurrent_activities=max_concurrent_activities,
        workflows=[TemplatePipeline, RenderPiece],
        workflow_runner=runner or SandboxedWorkflowRunner(),
        activities=[
            acts.cached_piece,
            acts.prepare,
            acts.render_main,
            acts.render_solids,
            acts.finish_piece,
            acts.project,
            acts.load_pipeline,
            acts.pack,
            acts.write_output,
            acts.render_accept,
            acts.render_claims,
        ],
    )
    if runner is not None:
        # A held activation (`_HoldsItsSignal`) is not a deadlock. `debug_mode`
        # would also turn the detector off, but it runs activations on the event
        # loop, which a held one would then block, test and all.
        assert worker._workflow_worker is not None
        worker._workflow_worker._deadlock_timeout_seconds = None
    return worker


class _HoldsItsSignal(WorkflowRunner):
    """The default runner, except that ``workflow_id``'s first activation that signals
    another workflow hands its completion back to the server, signal command and all,
    only once ``release`` is set. What the test does meanwhile reaches the workflow
    while that signal is in flight (#1590)."""

    def __init__(self, workflow_id: str) -> None:
        self.inner = SandboxedWorkflowRunner()
        self.workflow_id = workflow_id
        self.signalling = threading.Event()
        self.release = threading.Event()

    def prepare_workflow(self, defn: Any) -> None:
        self.inner.prepare_workflow(defn)

    def set_worker_level_failure_exception_types(self, types: Any) -> None:
        self.inner.set_worker_level_failure_exception_types(types)

    def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        instance = self.inner.create_instance(det)
        if det.info.workflow_id != self.workflow_id:
            return instance
        runner = self

        class Held:
            def activate(self, act: WorkflowActivation) -> WorkflowActivationCompletion:
                completion = instance.activate(act)
                commands = completion.successful.commands
                if not runner.signalling.is_set() and any(
                    c.HasField("signal_external_workflow_execution") for c in commands
                ):
                    runner.signalling.set()
                    runner.release.wait(30)
                return completion

            def __getattr__(self, name: str) -> Any:
                return getattr(instance, name)

        return cast(WorkflowInstance, Held())


async def _until_its_signal_is_answered(handle: WorkflowHandle[Any, Any]) -> None:
    """Until ``handle``'s workflow has run a task after its signal to another one
    completed: the signal's `await` has returned, so the job waits on the piece."""
    while True:
        names = [
            e.WhichOneof("attributes")
            async for e in handle.fetch_history_events()
            if e.WhichOneof("attributes") is not None
        ]
        done = "external_workflow_execution_signaled_event_attributes"
        if (
            done in names
            and "workflow_task_completed_event_attributes" in names[names.index(done) :]
        ):
            return
        await asyncio.sleep(0.05)


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
        assert acts.projections[-1].blob_keys == [
            piece_key("demo", REVISION, "model.scad", {"width": 1})
        ]


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
        # The built-in pipeline's one piece: its error as it is, no file in front (§5.3).
        assert last.failure.error == "openscad exited with 1"
        assert last.failure.log_tail == ["ERROR: boom"]
        assert acts.calls == ["prepare", "render_main"]


async def test_a_failure_outside_openscad_names_its_stage_and_cause() -> None:
    """#952: not "ChildWorkflowError: Child Workflow execution failed"."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities(fail_solids=True)
        async with _worker(client, queue, acts):
            job = _job(width=7)
            await client.execute_workflow(
                TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue
            )
        last = acts.projections[-1]
        assert last.state == "failed" and last.failure is not None
        assert last.failure.error == (
            "building the per-colour solids failed: BadZipFile: File is not a zip file"
        )
        assert last.failure.log_tail == []


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
        keys = {p.job_id: p.blob_keys for p in acts.projections if p.state == "done"}
        assert keys == {
            job.id: [piece_key("demo", f"job:{job.id}", "model.scad", {"width": 6})]
            for job in (a, b)
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
            await _until_its_signal_is_answered(hb)
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


async def test_a_job_cancelled_while_its_signal_to_the_piece_is_in_flight_is_cancelled() -> None:
    """The SDK shields a signal in flight from the workflow's cancellation, and
    (temporalio 1.34) drops a cancel that lands before the signal resolves: the job
    then waited on the piece, and ended with its outcome as if never cancelled
    (#1590). The cancel lands in that window here every time."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        a, b = _job(width=6), _job(width=6)
        runner = _HoldsItsSignal(f"render-{b.id}")
        async with _worker(client, queue, acts, runner):
            ha = await client.start_workflow(
                TemplatePipeline.run, a, id=f"render-{a.id}", task_queue=queue
            )
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            hb = await client.start_workflow(
                TemplatePipeline.run, b, id=f"render-{b.id}", task_queue=queue
            )
            try:
                assert await asyncio.to_thread(runner.signalling.wait, 30)
                await hb.cancel()  # recorded before the server sees the signal
            finally:
                runner.release.set()
            await _until_the_piece_is_waited_on(client, 6)
            with pytest.raises(WorkflowFailureError) as raised:
                await asyncio.wait_for(hb.result(), timeout=30)
            assert isinstance(raised.value.cause, CancelledError)
            gate.set()
            await ha.result()
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


async def _decided_after_an_activity(handle: WorkflowHandle[Any, Any]) -> bool:
    """Whether a workflow task completed after an activity did."""
    completed = False
    async for event in handle.fetch_history_events():
        if event.HasField("activity_task_completed_event_attributes"):
            completed = True
        elif completed and event.HasField("workflow_task_completed_event_attributes"):
            return True
    return False


async def test_an_unexpected_error_in_the_pipeline_projects_failed_and_closes_the_run() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities()
        async with _worker(client, queue, acts):
            job = _job(width=31)
            # Hand-authored inputs (spec §4.3): a `v` that is not a number fails the host,
            # before any template code runs.
            job.inputs = {"params": {"width": 31}, "v": "x"}
            handle = await client.start_workflow(
                TemplatePipeline.run, start_of(job), id=f"render-{job.id}", task_queue=queue
            )
            # Closed, not retried as a workflow task forever with the row at `running`.
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), timeout=30)
        last = [p for p in acts.projections if p.job_id == job.id and p.state][-1]
        assert last.state == "failed"
        assert last.failure is not None and last.failure.error.startswith("ValueError: ")
        assert last.steps is not None and last.steps[0].state == "failed"
        assert acts.calls == []


async def test_params_that_are_not_a_mapping_fail_the_default_pipeline_without_a_location() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities()
        async with _worker(client, queue, acts):
            job = _job(width=32)
            # `params` present but not a mapping: the pipeline's own error, so the run
            # completes with the job failed.
            job.inputs = {"params": None}
            await asyncio.wait_for(
                client.execute_workflow(
                    TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue
                ),
                timeout=30,
            )
        last = [p for p in acts.projections if p.job_id == job.id and p.state][-1]
        assert last.state == "failed"
        assert last.failure is not None
        assert last.failure.error.startswith("TypeError: ")  # no line in code nobody wrote
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


async def test_a_request_coalesced_into_a_render_that_raises_gets_its_job() -> None:
    """The run waits for the coalesced request's `accepted` (in its claims write)
    before it fails, so the request is answered rather than refused as
    still-accepting and re-sent into a new run (review #1066 1.1)."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        claims, fail = asyncio.Event(), asyncio.Event()
        acts = FakeActivities(block_claims=claims, fail_running=fail)
        async with _worker(client, queue, acts):
            job = _job(width=uuid.uuid4().int % 10**9)
            wid = f"render-{job.id}"
            await start_render(client, queue, start_of(job), id=wid)
            second = asyncio.create_task(start_render(client, queue, start_of(job), id=wid))
            while not acts.claiming:
                await asyncio.sleep(0.05)
            fail.set()
            handle = client.get_workflow_handle(wid)
            # The `failed` write landed and the run has taken its turn on it.
            while not await _decided_after_an_activity(handle):
                await asyncio.sleep(0.05)
            claims.set()
            answer = await asyncio.wait_for(second, timeout=30)
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), timeout=30)
        assert answer.job is not None and answer.coalesced and answer.job.claims == 2


async def test_accepted_after_the_render_raised_is_rejected_as_closing() -> None:
    """A request that reaches the run once its render has failed, while it waits for
    its handlers, is rejected, so it starts a fresh render rather than joining the
    failure (review #1066 (6) 1); rejected, it leaves no trace in the run's history, so
    its id is free for the next run (review #1066 (7) 1)."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        claims, fail = asyncio.Event(), asyncio.Event()
        acts = FakeActivities(block_claims=claims, fail_running=fail)
        async with _worker(client, queue, acts):
            job = _job(width=uuid.uuid4().int % 10**9)
            wid = f"render-{job.id}"
            await start_render(client, queue, start_of(job), id=wid)
            # A coalesced request in its claims write holds the failed run open.
            second = asyncio.create_task(start_render(client, queue, start_of(job), id=wid))
            while not acts.claiming:
                await asyncio.sleep(0.05)
            fail.set()
            handle = client.get_workflow_handle(wid)
            while not await _decided_after_an_activity(handle):
                await asyncio.sleep(0.05)
            late = uuid.uuid4().hex
            with pytest.raises(WorkflowUpdateFailedError) as rejected:
                await asyncio.wait_for(
                    handle.execute_update(ACCEPTED_UPDATE, id=late, result_type=RenderAnswer),
                    timeout=30,
                )
            claims.set()
            await asyncio.wait_for(second, timeout=30)
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), timeout=30)
            ids = [
                e.workflow_execution_update_accepted_event_attributes.accepted_request.meta.update_id
                async for e in handle.fetch_history_events()
                if e.HasField("workflow_execution_update_accepted_event_attributes")
            ]
        assert isinstance(rejected.value.cause, ApplicationError)
        assert rejected.value.cause.type == CLOSING
        assert late not in ids
        assert acts.claims == [2]


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


async def test_accepted_after_the_last_release_is_rejected_as_closing() -> None:
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
            with pytest.raises(WorkflowUpdateFailedError) as rejected:
                await handle.execute_update(ACCEPTED_UPDATE, result_type=RenderAnswer)
            projecting.set()
            await release
            await handle.result()
            gate.set()
        assert isinstance(rejected.value.cause, ApplicationError)
        assert rejected.value.cause.type == CLOSING
        last = [p for p in acts.projections if p.state][-1]
        assert last.failure is not None and last.failure.error == CANCELLED_ERROR


async def test_a_full_queue_answers_queue_full_and_completes_the_execution() -> None:
    """Back-pressure, not a defect: the refused run closes completed, so it never
    counts as a failed workflow (review #1066 3.1)."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities(queue_full=3)
        async with _worker(client, queue, acts):
            job = _job(width=54)
            wid = f"render-{job.id}"
            answer = await start_render(client, queue, start_of(job), id=wid)
            handle = client.get_workflow_handle(wid)
            await handle.result()
            described = await handle.describe()
        assert described.status == WorkflowExecutionStatus.COMPLETED
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


async def test_an_older_builds_start_for_a_row_already_cancelled_renders_nothing() -> None:
    """#603: an older build's API commits the row, then starts `render-<job id>`. A
    release on a newer replica in between cancels the row and finds no workflow to
    cancel, so the run starts for a cancelled row: it stops at its first step instead
    of rendering in full."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        job = _job(width=uuid.uuid4().int % 10**9)
        acts = FakeActivities(settled_rows=frozenset({job.id}))
        async with _worker(client, queue, acts):
            await client.execute_workflow(
                TemplatePipeline.run,
                job,
                id=f"render-{job.id}",
                task_queue=queue,
            )
        assert acts.calls == []
        assert [p.state for p in acts.projections] == ["running"]


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
