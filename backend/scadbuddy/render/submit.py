"""RenderService: renders on Temporal (spec 2026-09-27 §3.3), behind
``SCADBUDDY_TEMPORAL_ADDRESS``.

A submit inserts the `render_jobs` row (the projection) and then starts the workflow the
row names. The row is committed first, so a start that fails is not lost: the
reconciler starts every pending row whose workflow has not picked it up. A second
start of a workflow already running is a no-op (`USE_EXISTING` on submit; `FAIL` in
the reconciler, which then knows it started nothing). The projection publishes
every ``job.*`` event in its own transaction; nothing is announced here.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.config import ACTIVITY_TIMEOUT_MARGIN, Config
from scadbuddy.core.metrics import Metrics, RenderOutcome
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.job_models import Job, now
from scadbuddy.render.job_store import QueueFullError, render_key
from scadbuddy.render.jobs import INITIAL_RENDER_ESTIMATE
from scadbuddy.render.projection import JobProjection, workflow_id_for
from scadbuddy.render.render_cache import cached_render
from scadbuddy.render.schema import ParamValue
from scadbuddy.workflows.pipelines import RenderPreview, TemplatePipeline

logger = logging.getLogger(__name__)


class RenderService:
    def __init__(
        self,
        *,
        projection: JobProjection,
        client: Client | None,
        task_queue: str,
        config: Config,
        paths: DataPaths,
        metrics: Metrics,
        reconcile_after: float = 5.0,
        reconcile_interval: float = 5.0,
    ) -> None:
        self.store = projection
        self.client = client
        self.task_queue = task_queue
        self.config = config
        self.paths = paths
        self.metrics = metrics
        self.reconcile_after = reconcile_after
        self.reconcile_interval = reconcile_interval
        self._reconciler: asyncio.Task[None] | None = None
        self._listened_before = False
        metrics.store_info.labels(projection.backend).set(1)
        metrics.queue_depth_slo.set(config.render_queue_depth_slo)
        metrics.queue_max.set(config.render_queue_max)
        metrics.latency_slo.set(config.render_latency_slo)

    async def start(self) -> None:
        self.store.listener(
            on_state=self._listener_state,
            check_interval=self.config.render_fallback_poll_interval,
        )
        # A failed first pass must not stop the boot: the loop tries again.
        try:
            await self.reconcile_once()
        except Exception:
            logger.exception("the render reconciler's first pass failed")
        self._reconciler = asyncio.create_task(self._reconcile_forever())

    async def aclose(self) -> None:
        if self._reconciler is not None:
            self._reconciler.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconciler
            self._reconciler = None

    def _memo(self) -> dict[str, Any]:
        return {"activity_timeout": self.config.activity_timeout}

    async def submit(
        self,
        slug: str,
        params: Mapping[str, ParamValue],
        *,
        model_version: str | None = None,
        supersedes: str | None = None,
    ) -> Job:
        """As `RenderQueue.submit`, with the workflow start in place of the wake-up."""
        job = Job(
            id=uuid.uuid4().hex,
            slug=slug,
            params=dict(params),
            inputs={"params": dict(params)},
            model_version=model_version,
            created_at=now(),
        )
        key = render_key(slug, params, model_version)
        kept = (
            await asyncio.to_thread(cached_render, self.paths, slug, key)
            if model_version is not None
            else None
        )
        if kept is not None:
            job.state = "done"
            job.started_at = job.finished_at = job.created_at
            job.result = kept.result
            job.log_tail = kept.log_tail
            job.diagnostics = kept.result.diagnostics
            job.diagnostics_dropped = kept.result.diagnostics_dropped
        try:
            submitted = await asyncio.to_thread(
                self.store.submit,
                job,
                key,
                supersedes=supersedes,
                max_pending=self.config.render_queue_max,
            )
        except QueueFullError as error:
            self.metrics.render_rejected.inc()
            raise QueueFullError(error.depth, self.retry_after()) from None
        if submitted.superseded is not None:
            self._settled(submitted.superseded, "superseded")
            await self._cancel_workflow(submitted.superseded)
        if submitted.cached:
            self.metrics.render_cached.inc()
            self._settled(submitted.job, "done")
            return submitted.job
        if submitted.coalesced:
            self.metrics.render_coalesced.inc()
            return submitted.job
        self.metrics.render_submitted.inc()
        try:
            await self._start(submitted.job)
        except Exception:
            # The row is committed: the reconciler starts it.
            logger.exception(
                "could not start a render's workflow; the reconciler will",
                extra={"job_id": submitted.job.id},
            )
            self.metrics.store_errors.labels("start_workflow").inc()
        return submitted.job

    async def cancel(self, job_id: str, *, slug: str) -> Job | None:
        """Withdraw one request for the job; the last one cancels it and its workflow."""
        job = await asyncio.to_thread(self.store.release_claim, job_id, slug=slug)
        if job is not None:
            await self._cancel_workflow(job)
        return job

    async def reconcile_once(self) -> int:
        """Start the workflow of every pending row nothing has picked up for
        `reconcile_after` seconds. Returns how many it started: a row whose workflow
        is running and has not reached its first activity yet (a busy worker) is
        left alone."""
        stale = await asyncio.to_thread(self.store.stale_pending, self.reconcile_after)
        started: list[str] = []
        for job in stale:
            try:
                await self._start(job, WorkflowIDConflictPolicy.FAIL)
            except WorkflowAlreadyStartedError:
                continue
            except Exception:
                # One row that cannot start must not hold back the rows behind it.
                logger.exception(
                    "could not start a pending render's workflow", extra={"job_id": job.id}
                )
                self.metrics.store_errors.labels("start_workflow").inc()
                continue
            started.append(job.id)
        if started:
            logger.warning(
                "started the workflows of pending renders that had none running",
                extra={"count": len(started), "job_ids": started},
            )
        return len(started)

    async def render_preview(self, slug: str, timeout: float) -> bytes:
        """``slug``'s default-render preview, rendered on the worker. A second request
        for the slug joins the first run. Past ``timeout`` this caller stops waiting;
        the run is shared, so it is not cancelled (another caller may still be waiting
        on it, with time left) and bounds itself instead: its memo'd `preview_timeout`
        is ``timeout`` plus the margin. The timeout counts from the start, so it
        includes any wait for a free worker."""
        assert self.client is not None
        preview_timeout = timeout + ACTIVITY_TIMEOUT_MARGIN
        handle = await self.client.start_workflow(
            RenderPreview.run,
            slug,
            id=f"preview-{slug}",
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            memo={**self._memo(), "preview_timeout": preview_timeout},
        )
        png: bytes = await asyncio.wait_for(handle.result(), timeout)
        return png

    def retry_after(self) -> int:
        return max(1, math.ceil(INITIAL_RENDER_ESTIMATE))

    def refresh_metrics(self) -> None:
        """As `RenderQueue.refresh_metrics`, over the projection."""
        try:
            counts = self.store.counts()
        except Exception:
            logger.exception("could not read the render jobs from the projection")
            self.metrics.store_up.set(0)
            self.metrics.store_errors.labels("read").inc()
            return
        self.metrics.store_up.set(1)
        self.metrics.queue_depth.set(counts.pending)
        self.metrics.running.set(counts.running)
        oldest = counts.oldest_pending
        self.metrics.oldest_pending.set(
            max(0.0, (now() - oldest).total_seconds()) if oldest is not None else 0.0
        )

    async def _start(
        self,
        job: Job,
        conflict: WorkflowIDConflictPolicy = WorkflowIDConflictPolicy.USE_EXISTING,
    ) -> None:
        assert self.client is not None
        await self.client.start_workflow(
            TemplatePipeline.run,
            job,
            id=workflow_id_for(job.id),
            task_queue=self.task_queue,
            id_conflict_policy=conflict,
            memo=self._memo(),
        )

    async def _cancel_workflow(self, job: Job) -> None:
        assert self.client is not None
        try:
            await self.client.get_workflow_handle(workflow_id_for(job.id)).cancel()
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                # Never started (the reconciler had not got to it). A closed one
                # accepts the cancel without an error.
                logger.debug("no workflow to cancel", extra={"job_id": job.id})
                return
            # The row is cancelled either way; the workflow may run on until its next
            # `project` finds that. The submit that superseded it still succeeds.
            logger.warning(
                "could not cancel a render's workflow",
                extra={"job_id": job.id, "status": error.status.name},
            )
            self.metrics.store_errors.labels("cancel_workflow").inc()
        except Exception as error:
            # Not an RPC status (a client that cannot connect, say): the row is
            # cancelled all the same, so this never fails the submit that superseded it.
            logger.warning(
                "could not cancel a render's workflow",
                extra={"job_id": job.id, "error_type": type(error).__name__},
                exc_info=True,
            )
            self.metrics.store_errors.labels("cancel_workflow").inc()

    async def _reconcile_forever(self) -> None:
        while True:
            await asyncio.sleep(self.reconcile_interval)
            try:
                await self.reconcile_once()
            except Exception:
                logger.exception("the render reconciler's pass failed")

    def _settled(self, job: Job, outcome: RenderOutcome) -> None:
        self.metrics.render_finished.labels(outcome).inc()
        self.metrics.job_latency.labels(outcome).observe(
            max(0.0, ((job.finished_at or now()) - job.created_at).total_seconds())
        )

    def _listener_state(self, connected: bool) -> None:
        if connected and self._listened_before:
            self.metrics.listener_reconnects.inc()
        self._listened_before = self._listened_before or connected
        self.metrics.listener_connected.set(1 if connected else 0)
