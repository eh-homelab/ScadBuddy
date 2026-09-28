"""TemplatePipeline and RenderPiece (spec 2026-09-27 §3.4). Phase 1 runs only the
built-in default pipeline: one piece, one plate layout, one output."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ChildWorkflowError,
    FailureError,
    WorkflowAlreadyStartedError,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from scadbuddy.render.job_models import Job, StepInfo
    from scadbuddy.workflows.models import (
        Failure,
        PieceOutcome,
        PieceRequest,
        PieceResult,
        PrepareResult,
        Projection,
        RenderMainResult,
        piece_key,
    )

RETRY = RetryPolicy(
    maximum_attempts=3, initial_interval=timedelta(seconds=2), backoff_coefficient=2.0
)
#: `project` is the job row's only writer: a Postgres blip must not fail the job.
PROJECT_RETRY = RetryPolicy(
    maximum_attempts=0,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)
SHORT = timedelta(seconds=60)


def _openscad_timeout() -> timedelta:
    # `activity_timeout` (render_timeout + margin) travels in the workflow memo, set
    # by the submitter, so the workflow stays deterministic across config changes.
    memo = workflow.memo_value("activity_timeout", default=180.0, type_hint=float)
    return timedelta(seconds=memo)


def _waiter_recheck() -> timedelta:
    # Three attempts of a piece's worst case (two openscad activities plus two short
    # ones): a live piece is rarely re-checked, and harmlessly (the re-signal is idempotent).
    return 3 * (2 * _openscad_timeout() + 2 * SHORT)


def _failure_of(error: BaseException) -> Failure:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type == "OpenSCADError" and cause.details:
            detail = cause.details[0]
            return detail if isinstance(detail, Failure) else Failure.model_validate(detail)
        cause = cause.__cause__
    return Failure(error=f"{type(error).__name__}: {error}")


@workflow.defn(name="RenderPiece")
class RenderPiece:
    def __init__(self) -> None:
        # Jobs that found this piece already running: a child start has no
        # id-conflict policy (§3.6), so they wait on a signal instead of the child.
        self._waiting: list[str] = []

    @workflow.signal
    def wait_for_me(self, job_workflow_id: str) -> None:
        if job_workflow_id not in self._waiting:
            self._waiting.append(job_workflow_id)

    @workflow.run
    async def run(self, req: PieceRequest) -> PieceResult:
        try:
            result = await self._render(req)
        except ActivityError as error:
            await self._tell_waiting(PieceOutcome(failure=_failure_of(error)))
            raise
        await self._tell_waiting(PieceOutcome(result=result))
        return result

    async def _render(self, req: PieceRequest) -> PieceResult:
        # A piece a closed run already finished: never rendered again in place.
        cached: PieceResult | None = await workflow.execute_activity(
            "cached_piece",
            req,
            result_type=PieceResult,
            start_to_close_timeout=SHORT,
            retry_policy=RETRY,
        )
        if cached is not None:
            return cached
        prepared: PrepareResult = await workflow.execute_activity(
            "prepare",
            req,
            result_type=PrepareResult,
            start_to_close_timeout=SHORT,
            retry_policy=RETRY,
        )
        main: RenderMainResult = await workflow.execute_activity(
            "render_main",
            args=[req, prepared],
            result_type=RenderMainResult,
            start_to_close_timeout=_openscad_timeout(),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RETRY,
        )
        await workflow.execute_activity(
            "render_solids",
            args=[req, prepared, main],
            start_to_close_timeout=_openscad_timeout(),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RETRY,
        )
        result: PieceResult = await workflow.execute_activity(
            "finish_piece",
            args=[req, prepared, main],
            result_type=PieceResult,
            start_to_close_timeout=SHORT,
            retry_policy=RETRY,
        )
        return result

    async def _tell_waiting(self, outcome: PieceOutcome) -> None:
        while self._waiting:
            job = workflow.get_external_workflow_handle_for(
                TemplatePipeline.run, self._waiting.pop(0)
            )
            # A job that has closed (cancelled) since it asked fails the signal.
            with contextlib.suppress(FailureError):
                await job.signal(TemplatePipeline.piece_finished, outcome)


@workflow.defn(name="RenderPreview")
class RenderPreview:
    """A template's default-render preview (`render.previews`), on the worker that has
    the openscad budget. Id ``preview-<slug>``: a second request joins the first."""

    @workflow.run
    async def run(self, slug: str) -> bytes:
        # Schema, render and plate image, each bounded by `render_timeout`, plus the
        # margin: `RenderService.render_preview` sets it.
        timeout = workflow.memo_value("preview_timeout", default=3 * 120.0 + 60.0, type_hint=float)
        png: bytes = await workflow.execute_activity(
            "render_preview_png",
            slug,
            result_type=bytes,
            start_to_close_timeout=timedelta(seconds=timeout),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return png


@workflow.defn(name="TemplatePipeline")
class TemplatePipeline:
    def __init__(self) -> None:
        self._outcome: PieceOutcome | None = None

    @workflow.signal
    def piece_finished(self, outcome: PieceOutcome) -> None:
        self._outcome = outcome

    @workflow.run
    async def run(self, job: Job) -> None:
        async def project(**fields: object) -> None:
            await workflow.execute_activity(
                "project",
                Projection.model_validate({"job_id": job.id, "slug": job.slug, **fields}),
                start_to_close_timeout=SHORT,
                retry_policy=PROJECT_RETRY,
            )

        steps = [StepInfo(name="render", state="running", done=0, total=1)]
        try:
            await project(state="running")
            params = job.inputs.get("params", job.params) if job.inputs else job.params
            key = piece_key(job.slug, job.model_version, "model.scad", params)
            req = PieceRequest(
                slug=job.slug, revision=job.model_version, params=dict(params), piece_key=key
            )
            await project(steps=steps)
            outcome = await self._piece(req)
            if outcome.result is None:
                steps[0].state = "failed"
                await project(state="failed", failure=outcome.failure, steps=steps)
                return
            steps[0].state, steps[0].done = "done", 1
            await project(
                state="done",
                result=outcome.result.result,
                log_tail=outcome.result.log_tail,
                steps=steps,
                blob_key=key,
            )
        except (asyncio.CancelledError, ActivityError, ChildWorkflowError) as error:
            if not (is_cancelled_exception(error) and workflow.cancellation_reason() is not None):
                raise
            # A superseded/withdrawn job. The API may already have moved the row to
            # cancelled; the projection is idempotent for the case it did not.
            await project(state="cancelled", failure=Failure(error="cancelled"), steps=steps)
            raise

    async def _piece(self, req: PieceRequest) -> PieceOutcome:
        """Run the piece as this job's child, or wait on the one another job started.
        Neither a cancelled job nor its closing touches the piece (ABANDON twice)."""
        piece_id = f"piece-{req.piece_key}"
        while True:
            if self._outcome is not None:
                return self._outcome  # arrived between waits
            try:
                child = await workflow.start_child_workflow(
                    RenderPiece.run,
                    req,
                    id=piece_id,
                    memo={"activity_timeout": _openscad_timeout().total_seconds()},
                    cancellation_type=workflow.ChildWorkflowCancellationType.ABANDON,
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
            except WorkflowAlreadyStartedError:
                piece = workflow.get_external_workflow_handle_for(RenderPiece.run, piece_id)
                try:
                    await piece.signal(RenderPiece.wait_for_me, workflow.info().workflow_id)
                except FailureError:
                    continue  # it closed in between; start it again
                try:
                    await workflow.wait_condition(
                        lambda: self._outcome is not None, timeout=_waiter_recheck()
                    )
                except TimeoutError:
                    continue  # it may have closed without telling us (terminated, timed out)
                assert self._outcome is not None
                return self._outcome
            try:
                return PieceOutcome(result=await child)
            except ChildWorkflowError as error:
                if is_cancelled_exception(error) and workflow.cancellation_reason() is not None:
                    raise  # this job was cancelled: ABANDON resolves the child as cancelled
                return PieceOutcome(failure=_failure_of(error))
