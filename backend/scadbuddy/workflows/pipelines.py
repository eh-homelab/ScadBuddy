"""TemplatePipeline and RenderPiece (spec 2026-09-27 §3.4). Phase 1 runs only the
built-in default pipeline: one piece, one plate layout, one output."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey, SearchAttributeUpdate
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ChildWorkflowError,
    FailureError,
    WorkflowAlreadyStartedError,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from scadbuddy.render.job_models import CANCELLED_ERROR, SUPERSEDED_ERROR, Job, StepInfo
    from scadbuddy.workflows.models import (
        ACCEPT_ACTIVITY,
        CLAIMS_ACTIVITY,
        QUEUE_FULL,
        RELEASE_UPDATE,
        AcceptRender,
        Failure,
        PieceOutcome,
        PieceRequest,
        PieceResult,
        PrepareResult,
        Projection,
        ReleaseAnswer,
        RenderAnswer,
        RenderMainResult,
        RenderStart,
        input_problem,
        piece_key,
    )
    from scadbuddy.workflows.print_models import ACCEPTED_UPDATE, REFUSED

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

KIND = SearchAttributeKey.for_keyword("ScadbuddyKind")
SUBJECT = SearchAttributeKey.for_keyword("ScadbuddySubject")
STATUS = SearchAttributeKey.for_keyword("ScadbuddyStatus")
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
    """One render job, started as ``render-<render_key>`` with update-with-start
    (spec 2026-10-01 §4.5, #1053). Its first step inserts the job's row; every
    identical request that reaches the open execution joins it as one more claim, and
    the last claim released cancels it."""

    def __init__(self) -> None:
        self._outcome: PieceOutcome | None = None
        self._job: Job | None = None
        self._queue_full: int | None = None
        self._answered = False
        self._claims = 0
        #: The `accepted` Update ids answered: a request sent again keeps its one claim.
        self._requests: set[str] = set()
        #: Why the last claim was released: the error the cancelled job keeps.
        self._released: str | None = None
        #: The release cancelled the job; False when its work settled it first.
        self._cancelled = False
        self._work: asyncio.Task[None] | None = None
        self._search_attributes = False

    @workflow.signal
    def piece_finished(self, outcome: PieceOutcome) -> None:
        self._outcome = outcome

    @workflow.update(name=ACCEPTED_UPDATE)
    async def accepted(self) -> RenderAnswer:
        await workflow.wait_condition(
            lambda: self._work is not None or self._queue_full is not None
        )
        if self._queue_full is not None:
            return RenderAnswer(queue_full=self._queue_full)
        if self._released is not None:
            return RenderAnswer(closing=True)
        assert self._job is not None
        info = workflow.current_update_info()
        assert info is not None
        coalesced = self._answered
        self._answered = True
        # The route sends its request's key as the Update id (review #1066 2.1): a
        # re-send after `command-still-accepting` or a lost answer is the same claim.
        claimed = coalesced and info.id not in self._requests
        self._requests.add(info.id)
        if claimed:
            self._claims += 1
            await self._project_claims()
        return RenderAnswer(
            job=self._job.model_copy(update={"claims": self._claims}), coalesced=coalesced
        )

    @workflow.update(name=RELEASE_UPDATE)
    async def release(self, reason: str) -> ReleaseAnswer:
        await workflow.wait_condition(
            lambda: self._work is not None or self._queue_full is not None
        )
        if self._work is None or self._released is not None or self._work.done():
            # Nothing to cancel: never started, already released, or finished while
            # the run waits for its handlers (review #1066 2.3).
            return ReleaseAnswer()
        assert self._job is not None
        self._claims -= 1
        if self._claims > 0:
            await self._project_claims()
            return ReleaseAnswer()
        self._released = SUPERSEDED_ERROR if reason == "superseded" else CANCELLED_ERROR
        work = self._work
        work.cancel()
        await workflow.wait_condition(work.done)
        if not self._cancelled:
            # Its `done` or `failed` write was in flight and landed despite the cancel:
            # the job settled, and the run closes as settled.
            self._released = None
            return ReleaseAnswer()
        return ReleaseAnswer(
            cancelled=self._job.model_copy(
                update={"state": "cancelled", "claims": 0, "error": self._released}
            )
        )

    async def _project_claims(self) -> None:
        assert self._job is not None
        await workflow.execute_local_activity(
            CLAIMS_ACTIVITY,
            args=[self._job.id, self._claims],
            start_to_close_timeout=SHORT,
            retry_policy=PROJECT_RETRY,
        )

    def _upsert(self, *pairs: SearchAttributeUpdate[Any]) -> None:
        """§4.2's attributes: identifiers and states only, never content."""
        if self._search_attributes:
            workflow.upsert_search_attributes(list(pairs))

    @workflow.run
    async def run(self, start: RenderStart | Job) -> None:
        if isinstance(start, Job):
            # An older build's start (`render-<job id>`, its row inserted by its API),
            # reaching this build during a rolling deploy: it renders as it did.
            self._job, self._claims = start, 1
            await self._render(start)
            return
        self._search_attributes = start.search_attributes
        self._upsert(
            KIND.value_set("render"), SUBJECT.value_set(start.slug), STATUS.value_set("pending")
        )
        info = workflow.info()
        try:
            job: Job = await workflow.execute_local_activity(
                ACCEPT_ACTIVITY,
                AcceptRender(start=start, workflow_id=info.workflow_id, run_id=info.run_id),
                result_type=Job,
                start_to_close_timeout=SHORT,
                retry_policy=PROJECT_RETRY,
            )
        except (ActivityError, ApplicationError) as error:
            # A local activity's failure arrives as its ApplicationError itself
            # (temporalio 1.33), a regular one's as the ActivityError's cause.
            cause = error.cause if isinstance(error, ActivityError) else error
            if not (isinstance(cause, ApplicationError) and cause.type == QUEUE_FULL):
                raise
            # Nothing was written: the refusal answers the Update and fails the run.
            self._queue_full = int(cause.details[0]) if cause.details else 0
            self._upsert(STATUS.value_set("refused"))
            await workflow.wait_condition(workflow.all_handlers_finished)
            raise ApplicationError(str(cause), type=REFUSED, non_retryable=True) from None
        self._job, self._claims = job, 1
        self._work = asyncio.create_task(self._render(job))
        try:
            await self._work
        except asyncio.CancelledError:
            if self._released is None:
                raise
        self._upsert(STATUS.value_set("settled" if self._released is None else "cancelled"))
        await workflow.wait_condition(workflow.all_handlers_finished)

    async def _render(self, job: Job) -> None:
        async def project(**fields: object) -> None:
            await workflow.execute_activity(
                "project",
                Projection.model_validate({"job_id": job.id, "slug": job.slug, **fields}),
                start_to_close_timeout=SHORT,
                retry_policy=PROJECT_RETRY,
                # A release that cancels the job mid-projection waits for the write to
                # resolve: its outcome must be in history before the run completes, or
                # Temporal rejects that workflow task and the release with it.
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            if self._released is not None and fields.get("state") in (None, "running"):
                # A write that completed despite the cancel returns normally: the
                # release still stands.
                raise asyncio.CancelledError

        problem = input_problem(job.slug, job.model_version)
        if problem is not None:
            await project(state="failed", failure=Failure(error=problem))
            return
        steps = [StepInfo(name="render", state="running", done=0, total=1)]
        try:
            await project(state="running")
            self._upsert(STATUS.value_set("running"))
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
            released = self._released is not None and is_cancelled_exception(error)
            if released or (
                is_cancelled_exception(error) and workflow.cancellation_reason() is not None
            ):
                # Its last claim released (superseded or withdrawn), or the run cancelled
                # by hand: the piece goes on (ABANDON), the job is cancelled.
                steps[0].state = "cancelled"
                if released:
                    # Local, so it never waits behind openscad runs (the piece goes on)
                    # for one of the worker's activity slots: `release` waits for it.
                    await workflow.execute_local_activity(
                        "project",
                        Projection(
                            job_id=job.id,
                            slug=job.slug,
                            state="cancelled",
                            failure=Failure(error=self._released or "cancelled"),
                            steps=steps,
                        ),
                        start_to_close_timeout=SHORT,
                        retry_policy=PROJECT_RETRY,
                    )
                    self._cancelled = True
                    return  # a released job is an outcome: the run completes
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
                piece: workflow.ExternalWorkflowHandle[RenderPiece] = (
                    workflow.get_external_workflow_handle_for(RenderPiece.run, piece_id)
                )
                try:
                    await piece.signal(RenderPiece.wait_for_me, workflow.info().workflow_id)
                except FailureError as error:
                    if not _target_gone(error):
                        raise
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
                if is_cancelled_exception(error) and (
                    workflow.cancellation_reason() is not None or self._released is not None
                ):
                    raise  # this job was cancelled: ABANDON resolves the child as cancelled
                return PieceOutcome(failure=_failure_of(error))
