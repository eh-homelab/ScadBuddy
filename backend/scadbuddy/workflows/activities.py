"""The render activities: today's `render_job` stages, each one an activity that
reads and writes the piece's directory in the blob store (spec §3.4)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher, CheckoutGate
from scadbuddy.render.job_store import JobNotFoundError
from scadbuddy.render.jobs import (
    Prepared,
    finish_piece_stage,
    library_lease,
    prepare_source,
    render_main,
    render_solids_stage,
)
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.runner import OpenSCADError, ProcessOutput
from scadbuddy.store import BlobRefs, BlobStore
from scadbuddy.workflows.models import (
    Failure,
    PieceRequest,
    PieceResult,
    PrepareResult,
    Projection,
    RenderMainResult,
)

logger = logging.getLogger(__name__)


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


class RenderActivities:
    def __init__(self, deps: WorkerDeps) -> None:
        self.deps = deps

    def all(self) -> Sequence[Callable[..., Any]]:
        return [self.prepare, self.render_main, self.render_solids, self.finish_piece, self.project]

    def _config(self, prepared: PrepareResult) -> Config:
        # What `prepare_source` returned: the one field it sets is the library path.
        return replace(
            self.deps.config, library_path=tuple(Path(path) for path in prepared.library_path)
        )

    @activity.defn(name="prepare")
    async def prepare(self, req: PieceRequest) -> PrepareResult:
        d = self.deps
        try:
            prepared, _ = await prepare_source(
                req.slug,
                req.revision,
                config=d.config,
                paths=d.paths,
                history=d.history,
                fetcher=d.fetcher,
            )
        except OpenSCADError as error:
            raise _failure(error) from None
        return PrepareResult(
            version=prepared.version,
            scad=str(prepared.scad),
            library_path=[str(path) for path in prepared.library_path],
            schema_cache=str(prepared.schema_cache),
        )

    @activity.defn(name="render_main")
    async def render_main(self, req: PieceRequest, prepared: PrepareResult) -> RenderMainResult:
        d = self.deps
        work = asyncio.create_task(
            render_main(
                _prepared(prepared),
                req.params,
                d.blobs.dir_for(req.piece_key),
                config=self._config(prepared),
                assets=d.assets,
                checkouts=d.checkouts,
                holder=f"piece:{req.piece_key}",
            )
        )
        try:
            output = await _heartbeating(work)
        except OpenSCADError as error:
            raise _failure(error) from None
        return _main_result(output)

    @activity.defn(name="render_solids")
    async def render_solids(
        self, req: PieceRequest, prepared: PrepareResult, main: RenderMainResult
    ) -> None:
        d = self.deps
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
            )
        )
        try:
            await _heartbeating(work)
        except OpenSCADError as error:
            raise _failure(error) from None

    @activity.defn(name="finish_piece")
    async def finish_piece(
        self, req: PieceRequest, prepared: PrepareResult, main: RenderMainResult
    ) -> PieceResult:
        d = self.deps
        source = _prepared(prepared)
        try:
            # The schema is derived here, from the checkouts: hold them.
            async with library_lease(d.checkouts, f"piece:{req.piece_key}", source.library_path):
                result = await finish_piece_stage(
                    source,
                    req.params,
                    d.blobs.dir_for(req.piece_key),
                    _process_output(main),
                    config=self._config(prepared),
                    paths=d.paths,
                    slug=req.slug,
                    thumbnail_executor=d.thumbnail_executor,
                )
        except OpenSCADError as error:
            raise _failure(error) from None
        return PieceResult(result=result, log_tail=main.log_tail)

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
            job.error = failure.error
            job.log_tail = failure.log_tail
            job.diagnostics = failure.diagnostics
            job.diagnostics_dropped = failure.diagnostics_dropped
            job.warnings = failure.warnings
        if projection.blob_key is not None and projection.state == "done":
            # Before `finish`, so a sweep between the two cannot take the blob.
            await asyncio.to_thread(self.deps.refs.add, projection.blob_key, "job", job.id)
        if not await asyncio.to_thread(p.finish, job):
            logger.debug(
                "job already settled; projection ignored",
                extra={"job_id": job.id, "state": projection.state},
            )
