"""TemplatePipeline and RenderPiece (spec 2026-09-27 §3.4): a job runs its template's
pipeline, or the built-in one (§5.3), over `Ctx`; each piece is a child workflow."""

from __future__ import annotations

import asyncio
import contextlib
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
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

with workflow.unsafe.imports_passed_through():
    from scadbuddy.render.job_models import (
        CANCELLED_ERROR,
        SUPERSEDED_ERROR,
        Job,
        OutputRecord,
        PipelineOutput,
        StepInfo,
        StepState,
    )
    from scadbuddy.template import Blob, Part
    from scadbuddy.workflows.models import (
        ACCEPT_ACTIVITY,
        ACCEPT_TRANSIENT,
        ARRANGE_VERSION,
        CLAIMS_ACTIVITY,
        CLOSING,
        LEGACY_PENDING,
        QUEUE_FULL,
        RELEASE_UPDATE,
        RENDER_UNSTARTABLE,
        AcceptRender,
        ArrangeInputs,
        Failure,
        Layout,
        LoadedPipeline,
        LoadRequest,
        MigrateRequest,
        MigrateResult,
        OutputRequest,
        PackRequest,
        PieceOutcome,
        PieceRequest,
        PieceResult,
        PrepareResult,
        Projection,
        ReleaseAnswer,
        ReleaseReason,
        RenderAnswer,
        RenderMainResult,
        RenderStart,
        input_problem,
    )
    from scadbuddy.workflows.print_models import ACCEPTED_UPDATE
    from scadbuddy.workflows.sandbox import load_pipeline_module, pipeline_error, pipeline_error_at

from scadbuddy.workflows.ctx import Ctx, PieceFailedError

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
#: `render_accept`, the run's first step: a bounded number of attempts, so a failure no
#: retry fixes (an unexpected SQL error; an older build's row on the key that outlives
#: them answers still-accepting) answers the request rather than holding its render key
#: with no row (review #1066 (10) 1). A failing attempt is answered within the route's
#: 10 s deadline; a slow one is not cut short (each has `SHORT`), so a loaded database
#: delays a render, as `command-still-accepting`, rather than refusing it.
ACCEPT_RETRY = RetryPolicy(
    maximum_attempts=3, initial_interval=timedelta(seconds=1), backoff_coefficient=2.0
)

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


def _prepare_timeout() -> timedelta:
    """`prepare`: the transfers in `PREPARE_TIMEOUT`, then the schema export that checks
    the piece's parameters, one openscad run (`cached_schema`, under the render's own
    timeout), so the budget adds that run rather than folding it into the transfers."""
    return PREPARE_TIMEOUT + _openscad_timeout()


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
        + _retried(_prepare_timeout())
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


#: The failure types whose first detail is a `Failure` (the job's error, log tail and
#: diagnostics): openscad's own, a refused file or parameter, a template activity's.
FAILURE_TYPES = ("OpenSCADError", "ParameterError", "TemplateActivityError")
#: What each piece activity does, for a failure that is not OpenSCAD's (#952).
_STAGES = {
    "cached_piece": "looking up the finished piece",
    "prepare": "preparing the template's source",
    "render_main": "rendering the model",
    "render_solids": "building the per-colour solids",
    "finish_piece": "writing the 3MF and previews",
}


def _failure_of(error: BaseException) -> Failure:
    """OpenSCAD's own failure where there is one; else the stage that failed and the
    innermost cause, never just the wrapper ("ChildWorkflowError: Child Workflow
    execution failed", #952)."""
    stage: str | None = None
    innermost = error
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type in FAILURE_TYPES and cause.details:
            detail = cause.details[0]
            return detail if isinstance(detail, Failure) else Failure.model_validate(detail)
        if isinstance(cause, ActivityError) and cause.activity_type in _STAGES:
            stage = _STAGES[cause.activity_type]
        innermost = cause
        cause = cause.__cause__
    if stage is None:
        # Not a piece's stage (`load_pipeline` refusing the template, say): the
        # activity's own message.
        cause = error
        while cause is not None:
            if isinstance(cause, ApplicationError):
                return Failure(error=cause.message)
            cause = cause.__cause__
    if isinstance(innermost, ApplicationError) and innermost.type:
        reason = f"{innermost.type}: {innermost.message}"
    else:
        reason = f"{type(innermost).__name__}: {innermost}"
    return Failure(error=f"{stage} failed: {reason}" if stage else reason)


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
            await self._tell_waiting(
                PieceOutcome(failure=_failure_of(error), piece_key=req.piece_key)
            )
            raise
        await self._tell_waiting(PieceOutcome(result=result, piece_key=req.piece_key))
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
            start_to_close_timeout=_prepare_timeout(),
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
            job: workflow.ExternalWorkflowHandle[TemplatePipeline] = (
                workflow.get_external_workflow_handle_for(
                    TemplatePipeline.run, self._waiting.pop(0)
                )
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
            # The run's own priority (`PREVIEW_PRIORITY`), given outright: Temporal
            # 1.31 hands an activity its workflow's priority, 1.32 does not (#603).
            priority=workflow.info().priority,
        )
        return png


@workflow.defn(name="TemplatePipeline")
class TemplatePipeline:
    """One job, started as ``render-<render_key>`` with update-with-start (spec
    2026-10-01 §4.5, #1053): the template's pipeline (or the built-in one, §5.3) run
    over `Ctx`, or an arrange (spec 2026-09-27 §7). Its first step inserts the job's
    row; every identical request that reaches the open execution joins it as one more
    claim, and the last claim released cancels it."""

    def __init__(self) -> None:
        self._outcomes: dict[str, PieceOutcome] = {}
        self._parts: dict[str, Part] = {}
        self._pieces: dict[str, asyncio.Task[PieceOutcome]] = {}
        self._progress: asyncio.Task[None] | None = None
        #: The pieces another job is rendering that this one waits on, by key.
        self._waiting_on: list[str] = []
        self._job: Job | None = None
        self._queue_full: int | None = None
        #: Why the first step failed past its retries: the run completes with no row.
        self._unstartable: str | None = None
        #: An older build's row held the key past the first step's retries: the run
        #: completes with no row, and the request is still accepting.
        self._accept_transient = False
        self._answered = False
        self._claims = 0
        #: The `accepted` Update ids answered: a request sent again keeps its one claim.
        self._requests: set[str] = set()
        #: Why the last claim was released: the error the cancelled job keeps.
        self._released: str | None = None
        #: The release cancelled the job; False when its work settled it first.
        self._cancelled = False
        self._work: asyncio.Task[None] | None = None
        #: One claim projection at a time, each with the count when it runs: concurrent
        #: ones could commit out of order (review #1066 2.1).
        self._claims_lock = asyncio.Lock()
        self._search_attributes = False

    @workflow.signal
    def piece_finished(self, outcome: PieceOutcome) -> None:
        key = outcome.piece_key
        if not key and len(self._waiting_on) == 1:
            # From a build before `piece_key` (draining in a deploy): the one piece
            # waited on is the one that sent it. With several, the re-check restarts.
            key = self._waiting_on[0]
            outcome = outcome.model_copy(update={"piece_key": key})
        self._outcomes[key] = outcome

    def part_of(self, key: str) -> Part:
        part = self._parts.get(key)
        if part is None:
            raise ValueError(f"the layout places piece {key}, which this job never rendered")
        return part

    async def activity_call(self, name: str, arg: Any, *, result_type: type[Any]) -> Any:
        # `write_output` loads meshes, renders every plate's thumbnails (bounded by the
        # render timeout) and writes the 3MF: it gets the openscad bound and heartbeats.
        if name == "write_output":
            return await workflow.execute_activity(
                name,
                arg,
                result_type=result_type,
                start_to_close_timeout=_output_timeout(arg),
                heartbeat_timeout=HEARTBEAT,
                retry_policy=RETRY,
            )
        return await workflow.execute_activity(
            name, arg, result_type=result_type, start_to_close_timeout=SHORT, retry_policy=RETRY
        )

    def _projection(self, fields: dict[str, Any]) -> Projection:
        job = self._job
        if job is None:
            raise RuntimeError("the job has not started")
        return Projection.model_validate({"job_id": job.id, "slug": job.slug, **fields})

    async def _project(self, **fields: Any) -> bool:
        """For ``state="running"``, whether the row is still open (#603); else True."""
        open_row: bool = await workflow.execute_activity(
            "project",
            self._projection(fields),
            result_type=bool,
            start_to_close_timeout=SHORT,
            retry_policy=PROJECT_RETRY,
            # A release that cancels the job mid-projection waits for the write to
            # resolve: its outcome must be in history before the run completes, or
            # Temporal rejects that workflow task and the release with it.
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        if self._released is not None and fields.get("state") in (None, "running"):
            # A write that completed despite the cancel returns normally: the release
            # still stands.
            raise asyncio.CancelledError
        return open_row

    def project_later(self, **fields: Any) -> None:
        """`ctx.progress` is synchronous (§5.2): the write goes out without waiting.
        Each waits for the one before it, so they land in order (`set_steps` is
        last-writer-wins). A late one after the job settles is a no-op."""
        projection = self._projection(fields)
        previous = self._progress

        async def write() -> None:
            if previous is not None:
                await previous
            with contextlib.suppress(ActivityError):
                await workflow.execute_activity(
                    "project",
                    projection,
                    start_to_close_timeout=SHORT,
                    retry_policy=PROJECT_RETRY,
                )

        self._progress = asyncio.create_task(write())

    async def _settle_progress(self) -> None:
        """Let every progress write land before the final one, so it cannot overtake it."""
        if self._progress is not None:
            await self._progress

    def _started(self) -> bool:
        return (
            self._work is not None
            or self._queue_full is not None
            or self._unstartable is not None
            or self._accept_transient
        )

    def _closing(self) -> bool:
        return (
            self._released is not None
            or self._raised()
            or self._unstartable is not None
            or self._accept_transient
        )

    @workflow.update(name=ACCEPTED_UPDATE)
    async def accepted(self) -> RenderAnswer:
        # Once started, a handler claims or releases before it first yields: an
        # `accepted` and a `release` in one activation then see each other's effect
        # (the validator runs in the handler's task, temporalio 1.33).
        if not self._started():
            await workflow.wait_condition(self._started)
        if self._queue_full is not None:
            return RenderAnswer(queue_full=self._queue_full)
        if self._unstartable is not None:
            raise ApplicationError(self._unstartable, type=RENDER_UNSTARTABLE, non_retryable=True)
        if self._closing():
            # Only after the wait above: a release sent by hand took the last claim.
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

    @accepted.validator
    def _accepting(self) -> None:
        """A closing run rejects the request rather than answering it. A rejected
        Update is never written to history ("the Workflow will have no indication that
        it was ever requested", docs.temporal.io/handling-messages), so the same id,
        re-sent once the run has closed, starts a fresh run instead of being answered
        from this one (review #1066 (7) 1)."""
        if self._closing():
            raise ApplicationError("the render is closing", type=CLOSING, non_retryable=True)

    @workflow.update(name=RELEASE_UPDATE)
    async def release(self, reason: ReleaseReason) -> ReleaseAnswer:
        if not self._started():
            await workflow.wait_condition(self._started)
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
        async with self._claims_lock:
            await workflow.execute_local_activity(
                CLAIMS_ACTIVITY,
                args=[self._job.id, self._claims],
                start_to_close_timeout=SHORT,
                retry_policy=PROJECT_RETRY,
            )

    async def _project_released(
        self, job: Job, steps: list[StepInfo], version: str = "default"
    ) -> None:
        """The cancelled job its last release leaves. Local, so it never waits behind
        openscad runs (the piece goes on) for one of the worker's activity slots:
        `release` waits for it."""
        await workflow.execute_local_activity(
            "project",
            Projection(
                job_id=job.id,
                slug=job.slug,
                state="cancelled",
                failure=Failure(error=self._released or "cancelled"),
                steps=steps,
                pipeline_version=version,
            ),
            start_to_close_timeout=SHORT,
            retry_policy=PROJECT_RETRY,
        )
        self._cancelled = True

    def _raised(self) -> bool:
        """The render ended in an error, and the run waits for its handlers to fail."""
        work = self._work
        return (
            work is not None and work.done() and (work.cancelled() or work.exception() is not None)
        )

    def _released_by(self, error: BaseException) -> bool:
        return self._released is not None and is_cancelled_exception(error)

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
            KIND.value_set(start.kind), SUBJECT.value_set(start.slug), STATUS.value_set("pending")
        )
        info = workflow.info()
        try:
            job: Job = await workflow.execute_local_activity(
                ACCEPT_ACTIVITY,
                AcceptRender(start=start, workflow_id=info.workflow_id, run_id=info.run_id),
                result_type=Job,
                start_to_close_timeout=SHORT,
                retry_policy=ACCEPT_RETRY,
            )
        except FailureError as error:
            if is_cancelled_exception(error):
                raise  # the run cancelled by hand: not the step's failure
            # A local activity's failure arrives as its ApplicationError itself
            # (temporalio 1.33), a regular one's as the ActivityError's cause.
            cause = error.cause if isinstance(error, ActivityError) else error
            transient = isinstance(cause, ApplicationError) and cause.type in (
                LEGACY_PENDING,
                ACCEPT_TRANSIENT,
            )
            if transient or isinstance(cause, TemporalTimeoutError):
                # Transient: the older build's workflow settles its row, Postgres comes
                # back, or an attempt hung past `SHORT` (review #1066 (11)). `accepted`
                # answers `closing` and the client sends the request again.
                self._accept_transient = True
                self._upsert(STATUS.value_set("refused"))
                await workflow.wait_condition(workflow.all_handlers_finished)
                return
            if not (isinstance(cause, ApplicationError) and cause.type == QUEUE_FULL):
                # Its retries spent: nothing was written, and the Update answers the
                # failure (500 `render-unstartable`); the run completes, so the next
                # request starts a fresh one.
                self._unstartable = f"the render could not be accepted: {cause or error}"
                self._upsert(STATUS.value_set("refused"))
                await workflow.wait_condition(workflow.all_handlers_finished)
                return
            # Nothing was written: the refusal answers the Update, and the run completes.
            # Back-pressure, so never a failed workflow (review #1066 3.1).
            self._queue_full = int(cause.details[0]) if cause.details else 0
            self._upsert(STATUS.value_set("refused"))
            await workflow.wait_condition(workflow.all_handlers_finished)
            return
        self._job, self._claims = job, 1
        self._work = asyncio.create_task(self._render(job))
        try:
            await self._work
        except asyncio.CancelledError:
            if self._released is None:
                raise
        except Exception:
            # Its `failed` row is written: a request still joining gets the job, not a
            # run that closed under its Update (review #1066 (5) 1.1).
            self._upsert(STATUS.value_set("failed"))
            await workflow.wait_condition(workflow.all_handlers_finished)
            raise
        # `_cancelled`, not `_released`: a release whose cancel lost to the job's `done`
        # clears `_released` only after this resumes (review #1066 (9) 2).
        self._upsert(STATUS.value_set("cancelled" if self._cancelled else "settled"))
        await workflow.wait_condition(workflow.all_handlers_finished)

    async def _render(self, job: Job) -> None:
        problem = input_problem(job.slug, job.model_version)
        if problem is not None:
            try:
                await self._project(state="failed", failure=Failure(error=problem))
            except BaseException as error:
                # Released while the write waited for a worker (review #1066 2.2).
                if not self._released_by(error):
                    raise
                await self._project_released(job, [])
            return
        if job.kind == "arrange":
            await self._arrange(job)
            return
        ctx: Ctx | None = None
        steps = [StepInfo(name="render", state="running", done=0, total=None)]
        version = "default"
        try:
            if not await self._project(state="running"):
                # Settled before this run began: an older build's API commits the row
                # before it starts the run, and a release in between cancels the row
                # with no run to cancel. Nothing to render (#603).
                return
            self._upsert(STATUS.value_set("running"))
            await self._project(steps=steps)
            loaded: LoadedPipeline = await workflow.execute_activity(
                "load_pipeline",
                LoadRequest(slug=job.slug, revision=job.model_version),
                result_type=LoadedPipeline,
                start_to_close_timeout=_load_timeout(),
                heartbeat_timeout=HEARTBEAT,
                retry_policy=RETRY,
            )
            version = loaded.version
            inputs = job.inputs or {"params": job.params, "v": 0}
            ctx = Ctx(self, job, loaded, inputs)
            failure = await self._run_pipeline(ctx, loaded, inputs)
            steps = ctx.steps
            if failure is None and not ctx.outputs:
                failure = Failure(error=f"{loaded.file}: the pipeline wrote no output")
            await self._settle_progress()
            if failure is not None:
                await self._project(
                    state="failed",
                    failure=failure,
                    steps=_settled(steps, "failed"),
                    pipeline_version=version,
                )
                return
            await self._project(
                state="done",
                result=ctx.outputs[0].result,
                outputs=ctx.outputs,
                log_tail=ctx.log_tail,
                steps=_settled(steps, "done"),
                blob_keys=ctx.blob_keys,
                pipeline_version=version,
            )
        except BaseException as error:
            steps = ctx.steps if ctx is not None else steps
            await self._settle_failed(job, error, steps, version)

    async def _settle_failed(
        self,
        job: Job,
        error: BaseException,
        steps: list[StepInfo],
        version: str,
        failure: Failure | None = None,
    ) -> None:
        """The row a job that raised leaves: cancelled when released or cancelled by
        hand, else failed (with ``failure`` when the caller has a better one). Re-raises
        what is not a job outcome."""
        released = self._released_by(error)
        if released or (
            is_cancelled_exception(error) and workflow.cancellation_reason() is not None
        ):
            # Its last claim released (superseded, or by hand), or the run cancelled by
            # hand: the piece goes on (ABANDON), the job is cancelled.
            if released:
                await self._project_released(job, _settled(steps, "cancelled"), version)
                return  # a released job is an outcome: the run completes
            await self._project(
                state="cancelled",
                failure=Failure(error="cancelled"),
                steps=_settled(steps, "cancelled"),
                pipeline_version=version,
            )
            raise error
        if not isinstance(error, Exception):
            raise error  # the SDK's own (an eviction), or another cancel: not a job outcome
        # An activity said no (`load_pipeline` refusing the template): the job's outcome,
        # with its message. Anything else ends in a terminal row too, never one left at
        # `running`.
        said_no = _said_no(error)
        if failure is None:
            failure = (
                _failure_of(error) if said_no else Failure(error=f"{type(error).__name__}: {error}")
            )
        try:
            await self._project(
                state="failed",
                failure=failure,
                steps=_settled(steps, "failed"),
                pipeline_version=version,
            )
        except BaseException as cancel:
            # Released while the write waited for a worker (review #1066 2.2).
            if not self._released_by(cancel):
                raise
            await self._project_released(job, _settled(steps, "cancelled"), version)
            return
        if said_no:
            return
        if isinstance(error, FailureError):
            raise error
        # A plain exception would fail only the workflow task, which Temporal retries
        # forever with the run open (and its build never drained).
        raise ApplicationError(
            f"{type(error).__name__}: {error}", type=type(error).__name__, non_retryable=True
        ) from error

    async def _run_pipeline(
        self, ctx: Ctx, loaded: LoadedPipeline, inputs: dict[str, Any]
    ) -> Failure | None:
        """Run the template's `run(ctx, inputs)`. What it raises becomes the job's error
        with its file and line (§3.4 step 2); cancellation passes through untouched. The
        workflow task never fails on a template's exception: `Exception` includes the
        sandbox's `RestrictedWorkflowAccessError`, and `asyncio.CancelledError` is not
        one."""
        try:
            namespace = load_pipeline_module(loaded.source, loaded.file)
            await namespace["run"](ctx, inputs)
        except PieceFailedError as error:
            if loaded.version == "default":
                return error.failure  # today's behaviour (§5.3): the one piece's own error
            return error.failure.model_copy(update={"error": str(error)})
        except (ActivityError, ChildWorkflowError) as error:
            if is_cancelled_exception(error):
                raise
            failure = _failure_of(error)
            if loaded.version == "default":
                return failure  # no line in a file the template does not have
            return failure.model_copy(
                update={"error": pipeline_error_at(error, loaded.file, failure.error)}
            )
        except Exception as error:
            if is_cancelled_exception(error):
                raise
            if loaded.version == "default":
                return Failure(error=f"{type(error).__name__}: {error}")
            return Failure(error=pipeline_error(error, loaded.file))
        return None

    async def _arrange(self, job: Job) -> None:
        """Objects from saved outputs onto plates for a goal, then one 3MF (spec §7). No
        piece is rendered: the Parts are in the store already."""
        inputs = ArrangeInputs.model_validate(job.inputs)
        steps = [StepInfo(name="arrange", state="running", done=0, total=2)]
        try:
            if not await self._project(state="running", pipeline_version=ARRANGE_VERSION):
                return  # settled before this run began, as in `_render` (#603)
            self._upsert(STATUS.value_set("running"))
            await self._project(steps=steps)
            layout = await workflow.execute_activity(
                "pack",
                # Always plates, never the piece as rendered: only the writer applies
                # the pinned colour order (Review Focus 4).
                PackRequest(
                    items=inputs.items,
                    plate=inputs.plate,
                    goal=inputs.goal,
                    filament_plan=inputs.filament_plan,
                    colours=inputs.colours,
                    allow_own=False,
                ),
                result_type=Layout,
                start_to_close_timeout=SHORT,
                retry_policy=RETRY,
            )
            steps[0].done = 1
            await self._project(steps=steps)
            parts = list({item.part.piece_key: item.part for item in inputs.items}.values())
            keys = [p.piece_key for p in parts]
            req = OutputRequest(
                job_id=job.id,
                index=0,
                slug=job.slug,
                layout=layout,
                parts=parts,
                name=inputs.name,
                bom=[],
                files={},
                plate_model=inputs.plate_model,
                colours=inputs.colours,
                provenance=inputs.provenance,
                record=OutputRecord(
                    revision=None,
                    ui_api=None,
                    pipeline_api=0,
                    pipeline_version=ARRANGE_VERSION,
                    inputs_v=0,
                    plate_key=inputs.plate.key,
                    parts=keys,
                ),
            )
            written = await workflow.execute_activity(
                "write_output",
                req,
                result_type=PipelineOutput,
                # Every Part is fetched from the store: the writer's bound, as a
                # pipeline's `ctx.output` gets it, and it heartbeats.
                start_to_close_timeout=_output_timeout(req),
                heartbeat_timeout=HEARTBEAT,
                retry_policy=RETRY,
            )
            steps[0].state, steps[0].done = "done", 2
            await self._project(
                state="done",
                result=written.result,
                outputs=[written],
                blob_keys=list(dict.fromkeys([*keys, *written.blob_keys])),
                steps=steps,
                pipeline_version=ARRANGE_VERSION,
            )
        except BaseException as error:
            failure: Failure | None = None
            if _said_no(error) and isinstance(error, ActivityError):
                # The packer or the writer said no (an object that does not fit the
                # plate): the job's outcome, with the activity's message.
                cause = error.cause
                message = cause.message if isinstance(cause, ApplicationError) else str(error)
                failure = Failure(error=message)
            await self._settle_failed(job, error, steps, ARRANGE_VERSION, failure)

    async def piece(self, req: PieceRequest) -> PieceOutcome:
        """Identical renders in one pipeline share one task, so one child (§3.4)."""
        if req.piece_key not in self._pieces:
            self._pieces[req.piece_key] = asyncio.create_task(self._piece(req))
        outcome = await self._pieces[req.piece_key]
        if outcome.result is not None:
            self._parts[req.piece_key] = Part.of(req, outcome.result)
        return outcome

    async def _piece(self, req: PieceRequest) -> PieceOutcome:
        """Run the piece as this job's child, or wait on the one another job started.
        Neither a cancelled job nor its closing touches the piece (ABANDON twice)."""
        key = req.piece_key
        piece_id = f"piece-{key}"
        while True:
            if self._outcomes.get(key) is not None:
                return self._outcomes[key]  # arrived between waits
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
                self._waiting_on.append(key)
                try:
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
                            lambda: key in self._outcomes, timeout=_waiter_recheck()
                        )
                    except TimeoutError:
                        continue  # it may have closed without telling us (terminated, timed out)
                finally:
                    self._waiting_on.remove(key)
                return self._outcomes[key]
            try:
                return PieceOutcome(result=await child, piece_key=key)
            except ChildWorkflowError as error:
                if is_cancelled_exception(error) and (
                    workflow.cancellation_reason() is not None or self._released is not None
                ):
                    raise  # this job was cancelled: ABANDON resolves the child as cancelled
                return PieceOutcome(failure=_failure_of(error), piece_key=key)


def _said_no(error: BaseException) -> bool:
    """An activity or piece refused the job (`load_pipeline` refusing the template, the
    packer an object too large): its outcome. A `project` write that failed is not one:
    the run fails, as any error the job's row cannot record."""
    if isinstance(error, ActivityError):
        return error.activity_type != "project"
    return isinstance(error, ChildWorkflowError)


def _settled(steps: list[StepInfo], state: StepState) -> list[StepInfo]:
    return [s.model_copy(update={"state": state}) for s in steps]


def _output_timeout(req: OutputRequest) -> timedelta:
    """`write_output`: the openscad bound (thumbnails, the 3MF), plus one transfer per
    store move: the revision's snapshot (this worker may not have it yet), each piece
    and each `Blob` it fetches, and the output it publishes."""
    moves = 1 + len(req.parts) + sum(isinstance(v, Blob) for v in req.files.values()) + 1
    return _openscad_timeout() + moves * TRANSFER


def _load_timeout() -> timedelta:
    """`load_pipeline` and `migrate_inputs`: the revision's snapshot brought onto this
    worker (one transfer, heartbeated, as `prepare` budgets it), then their own work: a
    file read, or the template's `migrate` (`MIGRATE_SECONDS`, under `SHORT`)."""
    return SHORT + TRANSFER


#: `migrate_inputs`' retries: two attempts, a second after the first.
MIGRATE_RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=1))
#: The migration workflow's bound: both attempts at `_load_timeout()`, the backoff
#: between them, and a margin for scheduling, so it outlasts its activity rather than
#: cutting it off.
MIGRATE_EXECUTION_TIMEOUT = (
    (MIGRATE_RETRY.maximum_attempts or 1) * _load_timeout()
    + (MIGRATE_RETRY.initial_interval or timedelta())
    + timedelta(seconds=10)
)


@workflow.defn(name="MigrateInputs")
class MigrateInputs:
    """Saved inputs brought up to the template's `INPUTS_VERSION` (§8.2), on a worker."""

    @workflow.run
    async def run(self, req: MigrateRequest) -> MigrateResult:
        result: MigrateResult = await workflow.execute_activity(
            "migrate_inputs",
            req,
            result_type=MigrateResult,
            start_to_close_timeout=_load_timeout(),
            heartbeat_timeout=HEARTBEAT,
            retry_policy=MIGRATE_RETRY,
        )
        return result
