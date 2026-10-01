"""TemplatePipeline and RenderPiece (spec 2026-09-27 §3.4): a job runs its template's
pipeline, or the built-in one (§5.3), over `Ctx`; each piece is a child workflow."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

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
    from scadbuddy.render.job_models import Job, OutputRecord, PipelineOutput, StepInfo, StepState
    from scadbuddy.template import Blob, Part
    from scadbuddy.workflows.models import (
        ARRANGE_VERSION,
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
        RenderMainResult,
        input_problem,
    )
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
#: One piece up to or down from the store: the Bambuddy client's per-request budget
#: (`bambuddy.client.DEFAULT_UPLOAD_TIMEOUT`, 180 s), written out here because a
#: workflow module keeps its imports to the workflow's own models.
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


def _target_gone(error: FailureError) -> bool:
    return isinstance(error, ApplicationError) and error.type == EXTERNAL_NOT_FOUND


#: The failure types whose first detail is a `Failure` (the job's error, log tail and
#: diagnostics): openscad's own, a refused file or parameter, a template activity's.
FAILURE_TYPES = ("OpenSCADError", "ParameterError", "TemplateActivityError")


def _failure_of(error: BaseException) -> Failure:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type in FAILURE_TYPES and cause.details:
            detail = cause.details[0]
            return detail if isinstance(detail, Failure) else Failure.model_validate(detail)
        cause = cause.__cause__
    cause = error
    while cause is not None:
        if isinstance(cause, ApplicationError):
            return Failure(error=cause.message)
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
    """One job: the template's pipeline (or the built-in one, §5.3) run over `Ctx`."""

    def __init__(self) -> None:
        self._outcomes: dict[str, PieceOutcome] = {}
        self._parts: dict[str, Part] = {}
        self._pieces: dict[str, asyncio.Task[PieceOutcome]] = {}
        self._progress: asyncio.Task[None] | None = None
        #: The pieces another job is rendering that this one waits on, by key.
        self._waiting_on: list[str] = []
        self._job: Job | None = None

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

    async def _project(self, **fields: Any) -> None:
        await workflow.execute_activity(
            "project",
            self._projection(fields),
            start_to_close_timeout=SHORT,
            retry_policy=PROJECT_RETRY,
        )

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

    @workflow.run
    async def run(self, job: Job) -> None:
        self._job = job
        problem = input_problem(job.slug, job.model_version)
        if problem is not None:
            await self._project(state="failed", failure=Failure(error=problem))
            return
        ctx: Ctx | None = None
        steps = [StepInfo(name="render", state="running", done=0, total=None)]
        version = "default"
        try:
            await self._project(state="running")
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
            if is_cancelled_exception(error) and workflow.cancellation_reason() is not None:
                # A superseded/withdrawn job. The API may already have moved the row to
                # cancelled; the projection is idempotent for the case it did not.
                await self._project(
                    state="cancelled",
                    failure=Failure(error="cancelled"),
                    steps=_settled(steps, "cancelled"),
                    pipeline_version=version,
                )
                raise
            if not isinstance(error, Exception):
                raise  # the SDK's own (an eviction), or another cancel: not a job outcome
            if isinstance(error, ActivityError | ChildWorkflowError):
                # An activity said no (`load_pipeline` refusing the template): the job's
                # outcome, with its message.
                await self._project(
                    state="failed",
                    failure=_failure_of(error),
                    steps=_settled(steps, "failed"),
                    pipeline_version=version,
                )
                return
            # Anything else ends in a terminal row too, never one left at `running`.
            await self._project(
                state="failed",
                failure=Failure(error=f"{type(error).__name__}: {error}"),
                steps=_settled(steps, "failed"),
                pipeline_version=version,
            )
            if isinstance(error, FailureError):
                raise
            # A plain exception would fail only the workflow task, which Temporal
            # retries forever with the run open (and its build never drained).
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
                piece = workflow.get_external_workflow_handle_for(RenderPiece.run, piece_id)
                self._waiting_on.append(key)
                try:
                    try:
                        await piece.signal(RenderPiece.wait_for_me, workflow.info().workflow_id)
                    except FailureError as error:
                        if not _target_gone(error):
                            raise
                        continue  # it closed in between; start it again
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
                if is_cancelled_exception(error) and workflow.cancellation_reason() is not None:
                    raise  # this job was cancelled: ABANDON resolves the child as cancelled
                return PieceOutcome(failure=_failure_of(error), piece_key=key)


def _settled(steps: list[StepInfo], state: StepState) -> list[StepInfo]:
    return [s.model_copy(update={"state": state}) for s in steps]


def _output_timeout(req: OutputRequest) -> timedelta:
    """`write_output`: the openscad bound (thumbnails, the 3MF), plus one transfer per
    store move: the revision's snapshot (this worker may not have it yet), each piece
    and each `Blob` it fetches, and the output it publishes."""
    moves = 1 + len(req.parts) + sum(isinstance(v, Blob) for v in req.files.values()) + 1
    return _openscad_timeout() + moves * TRANSFER


@workflow.defn(name="Arrange")
class Arrange:
    """Objects from saved outputs onto plates for a goal, then one 3MF (spec §7). No
    piece is rendered: the Parts are in the store already."""

    @workflow.run
    async def run(self, job: Job) -> None:
        inputs = ArrangeInputs.model_validate(job.inputs)

        async def project(**fields: object) -> None:
            await workflow.execute_activity(
                "project",
                Projection.model_validate(
                    {
                        "job_id": job.id,
                        "slug": job.slug,
                        "pipeline_version": ARRANGE_VERSION,
                        **fields,
                    }
                ),
                start_to_close_timeout=SHORT,
                retry_policy=PROJECT_RETRY,
            )

        steps = [StepInfo(name="arrange", state="running", done=0, total=2)]
        try:
            await project(state="running", steps=steps)
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
            await project(steps=steps)
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
            await project(
                state="done",
                result=written.result,
                outputs=[written],
                blob_keys=list(dict.fromkeys([*keys, *written.blob_keys])),
                steps=steps,
            )
        except (asyncio.CancelledError, ActivityError) as error:
            if is_cancelled_exception(error) and workflow.cancellation_reason() is not None:
                await project(
                    state="cancelled",
                    failure=Failure(error="cancelled"),
                    steps=_settled(steps, "cancelled"),
                )
                raise
            if not isinstance(error, ActivityError):
                raise
            cause = error.cause
            message = cause.message if isinstance(cause, ApplicationError) else str(error)
            await project(
                state="failed", failure=Failure(error=message), steps=_settled(steps, "failed")
            )


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
