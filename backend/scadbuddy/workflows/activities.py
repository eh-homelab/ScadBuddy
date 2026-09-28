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
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics, RenderOutcome
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore, AssetUnavailableError, asset_ids_in
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher, CheckoutGate
from scadbuddy.render.job_models import Job, now
from scadbuddy.render.job_store import JobNotFoundError
from scadbuddy.render.jobs import (
    Prepared,
    finish_piece_stage,
    library_lease,
    prepare_source,
    render_main,
    render_solids_stage,
    timed_stage,
)
from scadbuddy.render.previews import render_preview
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.runner import OpenSCADError, ProcessOutput
from scadbuddy.store import BlobRefs, BlobStore, PieceStateLostError
from scadbuddy.store.assets import RemoteAssets
from scadbuddy.store.content import BlobScope, template_title
from scadbuddy.store.fonts import FontMirror, model_dir, wanted_families
from scadbuddy.store.snapshots import SnapshotStore, SnapshotUnavailableError
from scadbuddy.workflows.models import (
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
    """Bring the render's uploads in from the store; the ids it did not bring in (the
    ones already local among them), or None without a store (one volume: nothing to
    bring in). A render that then finds one missing names it as not in the store."""
    if d.remote_assets is None:
        return None
    wanted = asset_ids_in(params)
    brought = await _heartbeating(asyncio.create_task(d.remote_assets.ensure(d.assets, wanted)))
    return wanted - set(brought)


def _unavailable(error: AssetUnavailableError, missing: set[str] | None) -> ApplicationError:
    """Non-retryable: the same parameters name the same missing file on every attempt."""
    message = str(error)
    if missing is not None and error.asset_id in missing:
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


def _scope(req: PieceRequest, prepared: PrepareResult) -> BlobScope:
    """Where the piece's blob goes: its template's folder, named by `model.json`."""
    return BlobScope(slug=req.slug, title=template_title(Path(prepared.scad).parent, req.slug))


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
    """What `RenderQueue` observes when a render settles: its outcome, the latency
    from the submit, and the render's own duration (here from `mark_started`)."""
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
        )

    @activity.defn(name="render_main")
    async def render_main(self, req: PieceRequest, prepared: PrepareResult) -> RenderMainResult:
        d = self.deps
        # It renders into a directory it never fetched: the compare-and-swap baseline is
        # what the index holds now, and the directory is no hit until this publishes.
        baseline = await d.blobs.checkout_fresh(req.piece_key)
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
                d.blobs.publish_fresh(req.piece_key, scope=_scope(req, prepared), expected=baseline)
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
                d.blobs.publish_fresh(req.piece_key, scope=_scope(req, prepared), expected=baseline)
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
            # The schema is derived here, from the checkouts: hold them.
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
                d.blobs.publish_fresh(req.piece_key, scope=_scope(req, prepared), expected=baseline)
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
        return await _heartbeating(work)

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        """Move the row forward; a no-op when it is already past this state or gone."""
        p = self.deps.projection
        if projection.state == "running":
            await asyncio.to_thread(p.mark_started, projection.job_id)
            return
        if projection.state is None:
            if projection.steps is not None:
                await asyncio.to_thread(p.set_steps, projection.job_id, projection.steps)
            return
        try:
            job = await asyncio.to_thread(p.read, projection.job_id)
        except JobNotFoundError:
            return
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
            return
        if self.deps.metrics is not None and job.state in ("done", "failed"):
            _observe_settled(self.deps.metrics, job)
