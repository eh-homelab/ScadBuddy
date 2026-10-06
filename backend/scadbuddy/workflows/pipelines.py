"""TemplatePipeline and RenderPiece (spec 2026-09-27 §3.4). Phase 1 runs only the
built-in default pipeline: one piece, one plate layout, one output."""

from __future__ import annotations

import asyncio
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
        input_problem,
        piece_key,
    )

RETRY = RetryPolicy(
    maximum_attempts=3, initial_interval=timedelta(seconds=2), backoff_coefficient=2.0
)
#: What signalling a workflow that is gone raises: one that never ran and one that has
#: closed alike (measured against temporalio 1.33.0's dev server).
EXTERNAL_NOT_FOUND = "ExternalWorkflowExecutionNotFound"
#: `project` is the job row's only writer: a Postgres blip must not fail the job.
PROJECT_RETRY = RetryPolicy(
    maximum_attempts=0,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)
SHORT = timedelta(seconds=60)
#: One piece up to or down from the store: the Bambuddy client's per-request budget
#: (`bambuddy.client.DEFAULT_UPLOAD_TIMEOUT`, 180 s), written out here because a
#: workflow module keeps its imports to the workflow's own models. That client budget
#: bounds each connect, read or write, not a whole transfer; this one assumes a piece
#: moves within 180 s in total. A large piece over a slow link that takes longer is
#: cut off by the activity's start-to-close on every attempt (#691).
TRANSFER = timedelta(seconds=180)
#: Every stage that moves a piece beats while it does (`activities._heartbeating`).
HEARTBEAT = timedelta(seconds=30)
#: `prepare` downloads the revision's snapshot and the template's font families on a
#: worker without the API's volume (phase 3): a transfer, heartbeated, so a stalled
#: download is noticed within `HEARTBEAT` rather than at the budget's end.
PREPARE_TIMEOUT = timedelta(minutes=10)
#: A preview on a worker without the API's volume first brings in what `prepare` does
#: for a piece: the revision's snapshot and the font families it names, one transfer
#: each. (Its default parameters name no upload: `file_assets` skips a file
#: parameter's own default, so there is no assets transfer.)
#: Not in it: the first clone of a library pinned outside the image (a piece has
#: `PREPARE_TIMEOUT` for that). Such a template's first preview on a fresh worker may
#: time out; the clone lands anyway, and the next pass renders the preview.
PREVIEW_TRANSFER = 2 * TRANSFER


def _openscad_timeout() -> timedelta:
    # `activity_timeout` (render_timeout + margin) travels in the workflow memo, set
    # by the submitter, so the workflow stays deterministic across config changes.
    memo = workflow.memo_value("activity_timeout", default=180.0, type_hint=float)
    return timedelta(seconds=memo)


def _retried(start_to_close: timedelta) -> timedelta:
    """One activity's worst case under `RETRY`: every attempt timing out, plus the
    backoff between them."""
    attempts = RETRY.maximum_attempts
    backoff = sum(
        (RETRY.initial_interval * RETRY.backoff_coefficient**n for n in range(attempts - 1)),
        timedelta(),
    )
    return attempts * start_to_close + backoff


def _main_timeout() -> timedelta:
    """`render_main`: the render's uploads brought in (one transfer), the openscad run,
    then its piece published (one transfer)."""
    return _openscad_timeout() + 2 * TRANSFER


def _solids_timeout() -> timedelta:
    """`render_solids`: the piece fetched (one transfer), the render's uploads brought
    in (one transfer), the openscad runs, the piece published (one transfer)."""
    return _openscad_timeout() + 3 * TRANSFER


#: `cached_piece`: an index read and, on a miss, one download.
CACHED_TIMEOUT = SHORT + TRANSFER
#: `finish_piece`: the piece fetched, the short stage, the piece published.
FINISH_TIMEOUT = SHORT + 2 * TRANSFER


def _waiter_recheck() -> timedelta:
    """How long a waiting job trusts a running piece before looking again: the piece's
    worst case with every retry (`cached_piece`, `prepare`, the two openscad
    activities and `finish_piece`, each at its bound), plus one `SHORT` of slack. A
    live piece is then never re-checked, however many retries it needs, so a waiter
    never restarts a render another job is still paying for. Not covered: time a task
    sits queued for a busy worker, which no activity timeout bounds; a re-check then is
    harmless (the re-signal is idempotent)."""
    return (
        _retried(CACHED_TIMEOUT)
        + _retried(PREPARE_TIMEOUT)
        + _retried(_main_timeout())
        + _retried(_solids_timeout())
        + _retried(FINISH_TIMEOUT)
        + SHORT
    )


def _raise_if_cancelled() -> None:
    """Raise the workflow's cancellation if one was requested. temporalio (1.34)
    shields a signal in flight from it: a cancel that lands before the signal
    resolves only cancels the signal command, which is a no-op once it has been
    sent, and is then dropped (`_await_temporal_operation` uncancels the task).
    The signal's own outcome comes back, and the workflow would carry on as if it
    had never been cancelled (#1590)."""
    if workflow.cancellation_reason() is not None:
        raise asyncio.CancelledError


def _target_gone(error: FailureError) -> bool:
    return isinstance(error, ApplicationError) and error.type == EXTERNAL_NOT_FOUND


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
            start_to_close_timeout=CACHED_TIMEOUT,
            heartbeat_timeout=HEARTBEAT,
            retry_policy=RETRY,
        )
        if cached is not None:
            return cached
        prepared: PrepareResult = await workflow.execute_activity(
            "prepare",
            req,
            result_type=PrepareResult,
            start_to_close_timeout=PREPARE_TIMEOUT,
            heartbeat_timeout=HEARTBEAT,
            retry_policy=RETRY,
        )
        main: RenderMainResult = await workflow.execute_activity(
            "render_main",
            args=[req, prepared],
            result_type=RenderMainResult,
            start_to_close_timeout=_main_timeout(),
            heartbeat_timeout=HEARTBEAT,
            retry_policy=RETRY,
        )
        await workflow.execute_activity(
            "render_solids",
            args=[req, prepared, main],
            start_to_close_timeout=_solids_timeout(),
            heartbeat_timeout=HEARTBEAT,
            retry_policy=RETRY,
        )
        result: PieceResult = await workflow.execute_activity(
            "finish_piece",
            args=[req, prepared, main],
            result_type=PieceResult,
            start_to_close_timeout=FINISH_TIMEOUT,
            heartbeat_timeout=HEARTBEAT,
            retry_policy=RETRY,
        )
        return result

    async def _tell_waiting(self, outcome: PieceOutcome) -> None:
        while self._waiting:
            job = workflow.get_external_workflow_handle_for(
                TemplatePipeline.run, self._waiting.pop(0)
            )
            try:
                await job.signal(TemplatePipeline.piece_finished, outcome)
            except FailureError as error:
                # A job that has closed (cancelled) since it asked. Anything else is
                # not expected here, and surfaces.
                if not _target_gone(error):
                    raise


@workflow.defn(name="RenderPreview")
class RenderPreview:
    """A template's default-render preview (`render.previews`), on the worker that has
    the openscad budget. Id ``preview-<slug>``: a second request joins the first."""

    @workflow.run
    async def run(self, slug: str, revision: str | None = None) -> bytes:
        # Schema, render and plate image, each bounded by `render_timeout`, plus the
        # margin: `RenderService.render_preview` sets it. Then the snapshot and fonts
        # it brings in first, on a worker without the volume.
        timeout = workflow.memo_value("preview_timeout", default=3 * 120.0 + 60.0, type_hint=float)
        png: bytes = await workflow.execute_activity(
            "render_preview_png",
            args=[slug, revision],
            result_type=bytes,
            start_to_close_timeout=timedelta(seconds=timeout) + PREVIEW_TRANSFER,
            heartbeat_timeout=HEARTBEAT,
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

        problem = input_problem(job.slug, job.model_version)
        if problem is not None:
            await project(state="failed", failure=Failure(error=problem))
            return
        steps = [StepInfo(name="render", state="running", done=0, total=1)]
        try:
            await project(state="running")
            params = job.inputs.get("params", job.params)
            # Without a revision the source is live and may change before the next job,
            # so the piece is this job's own: its blob directory and workflow (#642).
            scope = f"job:{job.id}" if job.model_version is None else None
            version = job.model_version if job.model_version is not None else scope
            key = piece_key(job.slug, version, "model.scad", params)
            req = PieceRequest(
                slug=job.slug,
                revision=job.model_version,
                scope=scope,
                params=dict(params),
                piece_key=key,
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
        except BaseException as error:
            if is_cancelled_exception(error) and workflow.cancellation_reason() is not None:
                # A superseded/withdrawn job. The API may already have moved the row to
                # cancelled; the projection is idempotent for the case it did not.
                steps[0].state = "cancelled"
                await project(state="cancelled", failure=Failure(error="cancelled"), steps=steps)
                raise
            if not isinstance(error, Exception):
                raise  # the SDK's own (an eviction), or another cancel: not a job outcome
            # Anything else ends in a terminal row too, never one left at `running`.
            steps[0].state = "failed"
            await project(
                state="failed",
                failure=Failure(error=f"{type(error).__name__}: {error}"),
                steps=steps,
            )
            if isinstance(error, FailureError):
                raise
            # A plain exception would fail only the workflow task, which Temporal
            # retries forever with the run open (and its build never drained).
            raise ApplicationError(
                f"{type(error).__name__}: {error}", type=type(error).__name__, non_retryable=True
            ) from error

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
                except FailureError as error:
                    _raise_if_cancelled()
                    if not _target_gone(error):
                        raise
                    continue  # it closed in between; start it again
                _raise_if_cancelled()
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
