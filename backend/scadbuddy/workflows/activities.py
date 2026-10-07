"""The render activities: today's `render_job` stages, each one an activity that
reads and writes the piece's directory in the blob store (spec §3.4)."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics, RenderOutcome
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore, AssetUnavailableError, asset_ids_in
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher, CheckoutGate
from scadbuddy.render.job_models import Job, JobNotFoundError, QueueFullError, now
from scadbuddy.render.jobs import (
    Prepared,
    finish_piece_stage,
    library_lease,
    prepare_source,
    render_main,
    render_solids_stage,
    timed_stage,
)
from scadbuddy.render.previews import PreviewFailedError, render_preview
from scadbuddy.render.projection import JobProjection, LegacyPendingError, legacy_unrun
from scadbuddy.render.runner import OpenSCADError, ProcessOutput
from scadbuddy.store import BlobRefs, BlobStore, PieceStateLostError
from scadbuddy.store.assets import RemoteAssets
from scadbuddy.store.content import BlobScope, template_title
from scadbuddy.store.fonts import FontMirror, model_dir, wanted_families
from scadbuddy.store.snapshots import SnapshotStore, SnapshotUnavailableError
from scadbuddy.workflows.models import (
    ACCEPT_ACTIVITY,
    ACCEPT_TRANSIENT,
    CLAIMS_ACTIVITY,
    LEGACY_PENDING,
    QUEUE_FULL,
    AcceptRender,
    Failure,
    PieceRequest,
    PieceResult,
    PrepareResult,
    Projection,
    RenderMainResult,
)

logger = logging.getLogger(__name__)

#: The finished piece, written last: a blob that has it is never rendered again.
PIECE_NAME = "piece.json"
#: How long `render_accept` asks Temporal about an older build's row holding its key.
LEGACY_DESCRIBE = timedelta(seconds=5)


@dataclass
class WorkerDeps:
    config: Config
    paths: DataPaths
    assets: AssetStore
    blobs: BlobStore
    refs: BlobRefs
    projection: JobProjection
    history: ModelHistory | None = None
    checkouts: CheckoutGate | None = None
    fetcher: CheckoutFetcher | None = None
    thumbnail_executor: Executor | None = None
    metrics: Metrics | None = None
    snapshots: SnapshotStore | None = None
    fonts_mirror: FontMirror | None = None
    remote_assets: RemoteAssets | None = None


def _failure(error: OpenSCADError) -> ApplicationError:
    """Non-retryable: OpenSCAD said no, and it will say no again."""
    return ApplicationError(
        str(error),
        Failure(
            error=str(error),
            log_tail=list(error.log_tail),
            diagnostics=list(error.diagnostics),
            diagnostics_dropped=error.diagnostics_dropped,
            warnings=list(error.warnings),
        ),
        type="OpenSCADError",
        non_retryable=True,
    )


async def _ensure_assets(d: WorkerDeps, params: Mapping[str, object]) -> set[str] | None:
    """Bring the render's uploads in from the store; the ids it could not provide, or
    None without a store (one volume: nothing to bring in). A render that then finds
    one of those missing names it as not in the store."""
    if d.remote_assets is None:
        return None
    wanted = asset_ids_in(params)
    absent = await _heartbeating(asyncio.create_task(d.remote_assets.ensure(d.assets, wanted)))
    return set(absent)


def _unavailable(error: AssetUnavailableError, missing: set[str] | None) -> ApplicationError:
    """Non-retryable: the same parameters name the same missing file on every attempt.
    The one exception is a copy `ensure` provided that went before the render read it
    (the worker's upload sweep): the next attempt brings it in again."""
    if missing is not None and error.asset_id not in missing:
        return ApplicationError(
            f"parameter {error.parameter!r} names uploaded file {error.asset_id}, whose copy"
            " on this worker went before the render read it",
            type="AssetUnavailable",
        )
    message = str(error)
    if missing is not None:
        message = (
            f"parameter {error.parameter!r} names uploaded file {error.asset_id}, which is"
            " not in the blob store"
        )
    return ApplicationError(message, type="AssetUnavailable", non_retryable=True)


async def _heartbeating[T](work: asyncio.Task[T], every: float = 5.0) -> T:
    """Heartbeat while a long openscad run is on. When Temporal cancels the activity,
    cancel `work` and await it so the runner kills the openscad process group before
    the activity returns."""
    try:
        while True:
            done, _ = await asyncio.wait({work}, timeout=every)
            if done:
                return work.result()
            activity.heartbeat()
    finally:
        if not work.done():
            work.cancel()
            await asyncio.wait({work})
            if not work.cancelled():
                work.exception()  # retrieved, so asyncio does not log it as lost


def _prepared(result: PrepareResult) -> Prepared:
    return Prepared(
        Path(result.scad),
        result.version,
        tuple(Path(path) for path in result.library_path),
        Path(result.schema_cache),
        tuple(result.libraries),
    )


def _main_result(output: ProcessOutput) -> RenderMainResult:
    return RenderMainResult(
        returncode=output.returncode,
        duration_s=output.duration_s,
        plates=output.plates,
        log_tail=list(output.log_tail),
        diagnostics=list(output.diagnostics),
        diagnostics_dropped=output.diagnostics_dropped,
        notes=list(output.notes),
        missing_files=list(output.missing_files),
    )


async def _scope(req: PieceRequest, prepared: PrepareResult) -> BlobScope:
    """Where the piece's blob goes: its template's folder, named by `model.json` in
    the template's root (a piece in `parts/` reads the root's, not `parts/`).

    Reading `model.json` is file I/O, so it runs in a thread, off the activity's loop.
    """
    root = model_dir(Path(prepared.scad), req.file)
    title = await asyncio.to_thread(template_title, root, req.slug)
    return BlobScope(slug=req.slug, title=title)


async def _checkout(blobs: BlobStore, key: str) -> str | None:
    """The piece an earlier stage published, for this stage to continue; its sha is the
    publish baseline. Non-retryable when the store lost it: a retry would find nothing
    either, and the next submit renders the piece from the start."""
    try:
        return await _heartbeating(asyncio.create_task(blobs.checkout(key)))
    except PieceStateLostError:
        raise ApplicationError(
            f"piece {key} is no longer in the store; an earlier stage's output was lost",
            type="PieceStateLost",
            non_retryable=True,
        ) from None


def _write_piece(work: Path, piece: PieceResult) -> None:
    staging = work / f".{PIECE_NAME}.{uuid.uuid4().hex}"
    staging.write_text(piece.model_dump_json(), encoding="utf-8")
    os.replace(staging, work / PIECE_NAME)


def _read_piece(path: Path) -> PieceResult | None:
    if not path.is_file():
        return None
    try:
        return PieceResult.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as error:  # a pydantic ValidationError too: an older release wrote it
        logger.warning(
            "unreadable finished piece; rendering it again",
            extra={"path": str(path), "error": str(error)},
        )
        return None


def _process_output(main: RenderMainResult) -> ProcessOutput:
    return ProcessOutput(
        returncode=main.returncode,
        log_tail=list(main.log_tail),
        duration_s=main.duration_s,
        missing_files=tuple(main.missing_files),
        diagnostics=tuple(main.diagnostics),
        diagnostics_dropped=main.diagnostics_dropped,
        notes=tuple(main.notes),
        plates=main.plates,
    )


def _observe_settled(metrics: Metrics, job: Job) -> None:
    """What a settled render records: its outcome, the latency from the submit, and
    the render's own duration (from `mark_started`)."""
    assert job.state in ("done", "failed") and job.finished_at is not None
    outcome: RenderOutcome = job.state
    metrics.render_finished.labels(outcome).inc()
    metrics.job_latency.labels(outcome).observe(
        max(0.0, (job.finished_at - job.created_at).total_seconds())
    )
    started = job.started_at or job.created_at
    metrics.render_duration.labels(outcome).observe(
        max(0.0, (job.finished_at - started).total_seconds())
    )


class RenderActivities:
    def __init__(self, deps: WorkerDeps) -> None:
        self.deps = deps

    def all(self) -> Sequence[Callable[..., Any]]:
        return [
            self.cached_piece,
            self.prepare,
            self.render_main,
            self.render_solids,
            self.finish_piece,
            self.project,
            self.render_preview_png,
            self.render_accept,
            self.render_claims,
        ]

    def _config(self, prepared: PrepareResult) -> Config:
        # What `prepare_source` returned: the one field it sets is the library path.
        return replace(
            self.deps.config, library_path=tuple(Path(path) for path in prepared.library_path)
        )

    @activity.defn(name="cached_piece")
    async def cached_piece(self, req: PieceRequest) -> PieceResult | None:
        """The piece as a finished render left it, so it is never rendered in place again."""
        blobs = self.deps.blobs
        # Phase 1's guard (85b83de0) stays: without a revision the key stands for a
        # live source that can change under it.
        if req.revision is None:
            return None
        if not await _heartbeating(asyncio.create_task(blobs.fetch(req.piece_key))):
            return None
        return await asyncio.to_thread(_read_piece, blobs.dir_for(req.piece_key) / PIECE_NAME)

    async def _materialize(self, slug: str, revision: str | None) -> None:
        """The revision's snapshot, from the store onto this worker's volume."""
        d = self.deps
        if d.snapshots is None or revision is None:
            return
        found = await _heartbeating(asyncio.create_task(d.snapshots.materialize(slug, revision)))
        if not found and (d.history is None or not d.history.available):
            # Nothing to export it from here, and no retry will find it: fail now. The
            # next submit's `pin` (on the API, with git) stores it.
            raise ApplicationError(
                f"the template's source at {revision} is no longer in the store; render again",
                type=SnapshotUnavailableError.__name__,
                non_retryable=True,
            )

    @activity.defn(name="prepare")
    async def prepare(self, req: PieceRequest) -> PrepareResult:
        d = self.deps
        await self._materialize(req.slug, req.revision)
        try:
            with timed_stage(d.metrics)("source"):
                prepared, _ = await _heartbeating(
                    asyncio.create_task(
                        prepare_source(
                            req.slug,
                            req.revision,
                            config=d.config,
                            paths=d.paths,
                            history=d.history,
                            fetcher=d.fetcher,
                        )
                    )
                )
        except OpenSCADError as error:
            raise _failure(error) from None
        if d.fonts_mirror is not None:
            # Only the families this template could name: a fresh worker does not
            # download the whole font library for its first piece.
            source = model_dir(prepared.scad, req.file)
            families = await asyncio.to_thread(wanted_families, source, req.params)
            await _heartbeating(asyncio.create_task(d.fonts_mirror.sync(families)))
        return PrepareResult(
            version=prepared.version,
            scad=str(prepared.scad),
            library_path=[str(path) for path in prepared.library_path],
            schema_cache=str(prepared.schema_cache),
            libraries=list(prepared.libraries),
        )

    @activity.defn(name="render_main")
    async def render_main(self, req: PieceRequest, prepared: PrepareResult) -> RenderMainResult:
        d = self.deps
        # It renders into a directory it never fetched: the compare-and-swap baseline is
        # what the index holds now, and the directory is no hit until this publishes.
        # Heartbeated as `_checkout` is: it waits on the key's lock, which another fetch
        # of the same key in this process may hold for a whole transfer.
        baseline = await _heartbeating(asyncio.create_task(d.blobs.checkout_fresh(req.piece_key)))
        missing = await _ensure_assets(d, req.params)
        work = asyncio.create_task(
            render_main(
                _prepared(prepared),
                req.params,
                d.blobs.dir_for(req.piece_key),
                config=self._config(prepared),
                assets=d.assets,
                checkouts=d.checkouts,
                holder=f"piece:{req.piece_key}",
                stage=timed_stage(d.metrics),
            )
        )
        try:
            output = await _heartbeating(work)
        except OpenSCADError as error:
            raise _failure(error) from None
        except AssetUnavailableError as error:
            raise _unavailable(error, missing) from None
        await _heartbeating(
            asyncio.create_task(
                d.blobs.publish_fresh(
                    req.piece_key, scope=await _scope(req, prepared), expected=baseline
                )
            )
        )
        return _main_result(output)

    @activity.defn(name="render_solids")
    async def render_solids(
        self, req: PieceRequest, prepared: PrepareResult, main: RenderMainResult
    ) -> None:
        d = self.deps
        # The main 3MF may have been rendered on another worker.
        baseline = await _checkout(d.blobs, req.piece_key)
        missing = await _ensure_assets(d, req.params)
        work = asyncio.create_task(
            render_solids_stage(
                _prepared(prepared),
                req.params,
                d.blobs.dir_for(req.piece_key),
                _process_output(main),
                config=self._config(prepared),
                assets=d.assets,
                checkouts=d.checkouts,
                holder=f"piece:{req.piece_key}",
                stage=timed_stage(d.metrics),
            )
        )
        try:
            await _heartbeating(work)
        except OpenSCADError as error:
            raise _failure(error) from None
        except AssetUnavailableError as error:
            raise _unavailable(error, missing) from None
        # Against the sha this stage checked out, so a zombie attempt is refused.
        await _heartbeating(
            asyncio.create_task(
                d.blobs.publish_fresh(
                    req.piece_key, scope=await _scope(req, prepared), expected=baseline
                )
            )
        )

    @activity.defn(name="finish_piece")
    async def finish_piece(
        self, req: PieceRequest, prepared: PrepareResult, main: RenderMainResult
    ) -> PieceResult:
        d = self.deps
        baseline = await _checkout(d.blobs, req.piece_key)
        source = _prepared(prepared)
        work = d.blobs.dir_for(req.piece_key)
        try:
            # The schema is derived here, from the checkouts: hold them. The lease is per
            # activity, as in `render_main` and `render_solids`, because each activity is
            # its own unit of work, possibly on another worker, so no lease can span two.
            # It is safe because every lease re-checks its checkouts (`require_checkouts`),
            # so a removal between two activities fails the next one fast and, once the
            # fetcher restores the pin, its retry renders.
            async with library_lease(d.checkouts, f"piece:{req.piece_key}", source.library_path):
                result = await finish_piece_stage(
                    source,
                    req.params,
                    work,
                    _process_output(main),
                    config=self._config(prepared),
                    paths=d.paths,
                    slug=req.slug,
                    thumbnail_executor=d.thumbnail_executor,
                    stage=timed_stage(d.metrics),
                )
        except OpenSCADError as error:
            raise _failure(error) from None
        piece = PieceResult(result=result, log_tail=main.log_tail)
        # Last, and atomically: from here on the piece is answered by `cached_piece`.
        await asyncio.to_thread(_write_piece, work, piece)
        await _heartbeating(
            asyncio.create_task(
                d.blobs.publish_fresh(
                    req.piece_key, scope=await _scope(req, prepared), expected=baseline
                )
            )
        )
        return piece

    @activity.defn(name="render_preview_png")
    async def render_preview_png(self, slug: str, revision: str | None = None) -> bytes:
        """As `prepare` then the render, for the default parameters: on the bambuddy
        store the worker has no volume, so the source is the pinned revision's
        snapshot and its fonts come from the store (final review C1). The defaults
        name no upload (`file_assets` skips a file parameter's own default)."""
        d = self.deps
        await self._materialize(slug, revision)
        if d.fonts_mirror is not None:
            source = (
                d.paths.model_revision_dir(slug, revision)
                if revision is not None
                else d.paths.model_dir(slug)
            )
            families = await asyncio.to_thread(wanted_families, source, {})
            await _heartbeating(asyncio.create_task(d.fonts_mirror.sync(families)))
        work = asyncio.create_task(
            render_preview(
                slug,
                revision=revision,
                config=d.config,
                paths=d.paths,
                history=d.history,
                assets=d.assets,
                executor=d.thumbnail_executor,
                checkouts=d.checkouts,
                fetcher=d.fetcher,
            )
        )
        try:
            return await _heartbeating(work)
        except OpenSCADError as error:
            raise _failure(error) from None
        except PreviewFailedError as error:
            # Deterministic too (no plate image to keep): not retried, and typed so
            # the scheduler records it against the source (`is_render_error`).
            raise ApplicationError(
                str(error), Failure(error=str(error)), type="PreviewFailedError", non_retryable=True
            ) from None

    @activity.defn(name=ACCEPT_ACTIVITY)
    async def render_accept(self, accept: AcceptRender) -> Job:
        """`TemplatePipeline`'s first step (#1053): the execution's row, or the full
        queue's refusal with nothing written."""
        start = accept.start
        job = Job(
            id=uuid.uuid4().hex,
            slug=start.slug,
            params=dict(start.params),
            inputs=dict(start.inputs),
            model_version=start.model_version,
            kind=start.kind,
            created_at=now(),
            traceparent=start.traceparent,
        )
        orphaned: str | None = None
        try:
            try:
                return await asyncio.to_thread(
                    self.deps.projection.accept,
                    job,
                    start.render_key,
                    workflow_id=accept.workflow_id,
                    run_id=accept.run_id,
                    max_pending=start.max_pending,
                    supersedes=start.supersedes,
                )
            except LegacyPendingError as waiting:
                # An older build's row holds the key: retried until that build runs it,
                # unless no workflow ever will (it would block the key for good). A local
                # activity has a client too when it is `async def` (temporalio 1.33,
                # worker/_activity.py: `client=... if not running_activity.sync`;
                # review #1066 (2) 1.1), and test_submit's legacy_unrun tests run it.
                if not await legacy_unrun(
                    activity.client(), waiting.job, rpc_timeout=LEGACY_DESCRIBE
                ):
                    raise ApplicationError(str(waiting), type=LEGACY_PENDING) from None
                orphaned = waiting.job.id
            return await asyncio.to_thread(
                self.deps.projection.accept,
                job,
                start.render_key,
                workflow_id=accept.workflow_id,
                run_id=accept.run_id,
                max_pending=start.max_pending,
                supersedes=start.supersedes,
                orphaned=orphaned,
            )
        except QueueFullError as error:
            raise ApplicationError(
                str(error), error.depth, type=QUEUE_FULL, non_retryable=True
            ) from None
        except (psycopg.OperationalError, RPCError) as error:
            # Postgres out of reach (a pool timeout is one too), or Temporal unable to
            # say whether the older row's workflow runs: states that pass (review #1066
            # (11)).
            raise ApplicationError(str(error), type=ACCEPT_TRANSIENT) from error

    @activity.defn(name=CLAIMS_ACTIVITY)
    async def render_claims(self, job_id: str, claims: int) -> None:
        await asyncio.to_thread(self.deps.projection.set_claims, job_id, claims)

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> bool:
        """Move the row forward; a no-op when it is already past this state or gone.
        For ``running``, whether the row is still open: an older build's API commits
        the row before it starts the run, so a release may have settled it first, and
        the run then renders nothing (#603). Every other state answers True."""
        p = self.deps.projection
        if projection.state == "running":
            started = await asyncio.to_thread(p.mark_started, projection.job_id)
            # Only the call that moved the row records it: a retry finds it running.
            if started is not None and started.started_at is not None and self.deps.metrics:
                self.deps.metrics.queue_wait.observe(
                    max(0.0, (started.started_at - started.created_at).total_seconds())
                )
            if started is not None:
                return True
            try:
                job = await asyncio.to_thread(p.read, projection.job_id)
            except JobNotFoundError:
                return False
            return job.state == "running"
        if projection.state is None:
            if projection.steps is not None:
                await asyncio.to_thread(p.set_steps, projection.job_id, projection.steps)
            return True
        try:
            job = await asyncio.to_thread(p.read, projection.job_id)
        except JobNotFoundError:
            return True
        # The API cancelled it first: its error says why, the workflow's does not.
        keep_error = projection.state == "cancelled" and job.state == "cancelled"
        job.state = projection.state
        job.pipeline_version = projection.pipeline_version
        if projection.steps is not None:
            job.steps = projection.steps
        if projection.result is not None:
            job.result = projection.result
            job.log_tail = projection.log_tail
            job.warnings = list(projection.result.warnings)
            job.diagnostics = list(projection.result.diagnostics)
            job.diagnostics_dropped = projection.result.diagnostics_dropped
        if projection.failure is not None:
            failure = projection.failure
            if not keep_error:
                job.error = failure.error
            job.log_tail = failure.log_tail
            job.diagnostics = failure.diagnostics
            job.diagnostics_dropped = failure.diagnostics_dropped
            job.warnings = failure.warnings
        if projection.blob_key is not None and projection.state == "done":
            # Before `finish`, so a sweep between the two cannot take the blob.
            await asyncio.to_thread(self.deps.refs.add, projection.blob_key, "job", job.id)
        job.finished_at = job.finished_at or now()
        if not await asyncio.to_thread(p.finish, job):
            logger.debug(
                "job already settled; projection ignored",
                extra={"job_id": job.id, "state": projection.state},
            )
            return True
        if self.deps.metrics is not None and job.state in ("done", "failed"):
            _observe_settled(self.deps.metrics, job)
        return True
