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
from datetime import timedelta
from typing import Any

from fastapi import status
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.config import ACTIVITY_TIMEOUT_MARGIN, Config
from scadbuddy.core.metrics import Metrics, RenderOutcome
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.render.inputs import InputsError, inputs_key, legacy_inputs
from scadbuddy.render.job_models import Job, QueueFullError, now, render_key
from scadbuddy.render.jobs import INITIAL_RENDER_ESTIMATE, prune_revision_exports
from scadbuddy.render.projection import JobProjection, workflow_id_for
from scadbuddy.render.schema import ParamValue
from scadbuddy.store.snapshots import SnapshotStore
from scadbuddy.workflows.models import MigrateRequest, MigrateResult
from scadbuddy.workflows.pipelines import (
    MIGRATE_EXECUTION_TIMEOUT,
    PREVIEW_TRANSFER,
    MigrateInputs,
    RenderPreview,
    TemplatePipeline,
)

logger = logging.getLogger(__name__)

#: How long a request waits on one Temporal call before leaving it to the reconciler
#: (a start) or giving up (a cancel): the SDK's own retry budget is ~10 s per call.
RPC_TIMEOUT = timedelta(seconds=5)

#: The largest `Job` a submit sends as a workflow input. Temporal refuses a payload
#: over 2 MiB outright and warns past 512 KiB; a start it refuses would never succeed.
MAX_WORKFLOW_INPUT_BYTES = 1024 * 1024
#: Start errors that no retry can fix. INVALID_ARGUMENT is the input itself: a
#: workflow argument over the payload limit gets it (verified with 3 MiB on the dev
#: server). NOT_FOUND is a configuration error, a namespace or task queue that does
#: not exist, which no retry fixes. Not FAILED_PRECONDITION: Temporal answers that for
#: a namespace that is not active (yet), which passes, so the reconciler retries it.
UNSTARTABLE = frozenset({RPCStatusCode.INVALID_ARGUMENT, RPCStatusCode.NOT_FOUND})


def _unstartable(error: Exception) -> bool:
    return isinstance(error, RPCError) and error.status in UNSTARTABLE


class RenderService:
    def __init__(
        self,
        *,
        projection: JobProjection,
        client: Client,
        task_queue: str,
        config: Config,
        paths: DataPaths,
        metrics: Metrics,
        reconcile_after: float = 5.0,
        reconcile_interval: float = 5.0,
        prune_interval: float = 300.0,
    ) -> None:
        self.store = projection
        self.client = client
        #: Set in the lifespan once the store is built (Task 8), as `client` is.
        self.snapshots: SnapshotStore | None = None
        self.task_queue = task_queue
        self.config = config
        self.paths = paths
        self.metrics = metrics
        self.reconcile_after = reconcile_after
        self.reconcile_interval = reconcile_interval
        #: How often the reconciler also prunes: settled rows hold their blobs' refs
        #: until they go.
        self.prune_interval = prune_interval
        self._reconciler: asyncio.Task[None] | None = None
        self._listened_before = False
        metrics.store_info.labels(projection.backend).set(1)
        metrics.queue_depth_slo.set(config.render_queue_depth_slo)
        metrics.queue_max.set(config.render_queue_max)
        metrics.latency_slo.set(config.render_latency_slo)

    async def start(self) -> None:
        self.store.listener(on_state=self._listener_state)
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
        return {
            "activity_timeout": self.config.activity_timeout,
            "template_activity_max_timeout": self.config.template_activity_max_timeout,
        }

    async def submit(
        self,
        slug: str,
        params: Mapping[str, ParamValue],
        *,
        model_version: str | None = None,
        supersedes: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        whole_inputs: bool = False,
    ) -> Job:
        """Record the job (or join the waiting one it matches) and start its workflow.
        ``whole_inputs``: a pipeline template's job, keyed on all of its inputs (§3.4)."""
        if self.snapshots is not None:
            # The bambuddy store (spec §6.1): workers read the source from the store,
            # so every job names a revision whose snapshot exists before it starts.
            model_version = await self.snapshots.pin(slug, model_version)
        job = Job(
            id=uuid.uuid4().hex,
            slug=slug,
            params=dict(params),
            inputs=dict(inputs) if inputs is not None else legacy_inputs(params),
            model_version=model_version,
            created_at=now(),
        )
        size = len(pydantic_data_converter.payload_converter.to_payload(job).data)
        if size > MAX_WORKFLOW_INPUT_BYTES:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"these parameters make a render request of {size} bytes; the most a render"
                f" can carry is {MAX_WORKFLOW_INPUT_BYTES}",
            )
        key = (
            inputs_key(slug, job.inputs, model_version)
            if whole_inputs
            else render_key(slug, params, model_version)
        )
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
        if submitted.coalesced:
            self.metrics.render_coalesced.inc()
            return submitted.job
        self.metrics.render_submitted.inc()
        try:
            await self._start(submitted.job)
        except Exception as error:
            self.metrics.store_errors.labels("start_workflow").inc()
            if _unstartable(error):
                await self._fail_unstartable(submitted.job, error)
            else:
                # The row is committed: the reconciler starts it.
                logger.exception(
                    "could not start a render's workflow; the reconciler will",
                    extra={"job_id": submitted.job.id},
                )
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
            except Exception as error:
                # One row that cannot start must not hold back the rows behind it.
                self.metrics.store_errors.labels("start_workflow").inc()
                if _unstartable(error):
                    await self._fail_unstartable(job, error)
                else:
                    logger.exception(
                        "could not start a pending render's workflow", extra={"job_id": job.id}
                    )
                continue
            started.append(job.id)
        if started:
            logger.warning(
                "started the workflows of pending renders that had none running",
                extra={"count": len(started), "job_ids": started},
            )
        return len(started)

    async def prune(self) -> None:
        """Settled jobs past `job_ttl` (and their blob refs), and revision exports."""
        ttl = self.config.job_ttl
        await asyncio.to_thread(self.store.prune, ttl)
        await asyncio.to_thread(prune_revision_exports, self.paths, ttl)

    async def render_preview(self, slug: str, timeout: float) -> bytes:
        """``slug``'s default-render preview, rendered on the worker. A second request
        for the slug joins the first run. Past ``timeout`` this caller stops waiting;
        the run is shared, so it is not cancelled (another caller may still be waiting
        on it, with time left) and bounds itself instead: its memo'd `preview_timeout`
        is ``timeout`` plus the margin. The timeout counts from the start, so it
        includes any wait for a free worker. On the bambuddy store the worker renders
        the snapshot of the slug's last commit, which it may first have to bring in:
        the caller then waits `PREVIEW_TRANSFER` longer."""
        revision: str | None = None
        wait = timeout
        if self.snapshots is not None:
            # The bambuddy store: the worker has no volume, so it renders the snapshot
            # of the last commit (as `submit`), and may first bring it and its fonts in.
            revision = await self.snapshots.pin(slug, None)
            wait += PREVIEW_TRANSFER.total_seconds()
        preview_timeout = timeout + ACTIVITY_TIMEOUT_MARGIN
        handle = await self.client.start_workflow(
            RenderPreview.run,
            args=[slug, revision],
            id=f"preview-{slug}",
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            memo={**self._memo(), "preview_timeout": preview_timeout},
            rpc_timeout=RPC_TIMEOUT,
        )
        png: bytes = await asyncio.wait_for(handle.result(), wait)
        return png

    async def migrate_inputs(
        self, slug: str, inputs: Mapping[str, Any], *, version: str | None
    ) -> MigrateResult:
        """``inputs`` brought up to the template's `INPUTS_VERSION` by its `migrate`, run
        on a worker (§8.2, §9). The template's refusal is an `InputsError` with its
        message; a request too large to carry is a 413, the service unreachable a 503,
        and a migration that ran out of time a 504. On the bambuddy store the worker has
        no volume, so every revision is pinned as a snapshot first, as for a render
        (`pin` takes None as the last commit)."""
        # Measured before the pin, so an oversized request uploads no snapshot: with a
        # full-length revision, the longest `pin` can return.
        probe = MigrateRequest(slug=slug, revision="0" * 40, inputs=dict(inputs))
        size = len(pydantic_data_converter.payload_converter.to_payload(probe).data)
        if size > MAX_WORKFLOW_INPUT_BYTES:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"these inputs make a migration request of {size} bytes; the most one"
                f" can carry is {MAX_WORKFLOW_INPUT_BYTES}",
            )
        revision = version
        if self.snapshots is not None:
            revision = await self.snapshots.pin(slug, version)
        req = MigrateRequest(slug=slug, revision=revision, inputs=dict(inputs))
        try:
            result: MigrateResult = await self.client.execute_workflow(
                MigrateInputs.run,
                req,
                id=f"migrate-{uuid.uuid4().hex}",
                task_queue=self.task_queue,
                execution_timeout=MIGRATE_EXECUTION_TIMEOUT,
                rpc_timeout=RPC_TIMEOUT,
            )
        except RPCError as error:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"the render service is unavailable: {error.message}",
            ) from None
        except WorkflowFailureError as error:
            cause: BaseException | None = error.cause
            timed_out = False
            while cause is not None and not isinstance(cause, ApplicationError):
                timed_out = timed_out or isinstance(cause, TemporalTimeoutError)
                cause = cause.__cause__
            if isinstance(cause, ApplicationError):
                raise InputsError(cause.message) from None
            if timed_out:
                raise ApiError(
                    status.HTTP_504_GATEWAY_TIMEOUT,
                    "migrating the inputs timed out after"
                    f" {MIGRATE_EXECUTION_TIMEOUT.total_seconds():g}s",
                ) from None
            raise InputsError(str(error)) from None
        return result

    def retry_after(self) -> int:
        return max(1, math.ceil(INITIAL_RENDER_ESTIMATE))

    def refresh_metrics(self) -> None:
        """The queue gauges, read from the projection."""
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
        await self.client.start_workflow(
            TemplatePipeline.run,
            job,
            id=workflow_id_for(job.id),
            task_queue=self.task_queue,
            id_conflict_policy=conflict,
            memo=self._memo(),
            # Bounds a pipeline that never yields; `settle_timed_out` fails its row.
            execution_timeout=timedelta(seconds=self.config.pipeline_timeout),
            rpc_timeout=RPC_TIMEOUT,
        )

    async def _fail_unstartable(self, job: Job, error: Exception) -> None:
        """Settle a row whose workflow Temporal will never start, so it neither waits
        forever nor holds its render key for every identical request."""
        logger.error(
            "Temporal refused a render's workflow for good; failing the job",
            extra={"job_id": job.id, "error": str(error)},
        )
        job.state = "failed"
        job.finished_at = now()
        job.error = f"the render could not be started: {error}"
        if await asyncio.to_thread(self.store.finish, job):
            self._settled(job, "failed")

    async def _cancel_workflow(self, job: Job) -> None:
        try:
            await self.client.get_workflow_handle(workflow_id_for(job.id)).cancel(
                rpc_timeout=RPC_TIMEOUT
            )
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

    async def settle_timed_out(self) -> int:
        """Fail every running row whose workflow ended without settling it: timed out
        (a pipeline that never yields), terminated, or failed. Returns how many."""
        timeout = self.config.pipeline_timeout
        stale = await asyncio.to_thread(self.store.stale_running, timeout)
        settled = 0
        for job in stale:
            try:
                handle = self.client.get_workflow_handle(workflow_id_for(job.id))
                status = (await handle.describe(rpc_timeout=RPC_TIMEOUT)).status
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    # The server could not say (unavailable, a deadline): nothing is
                    # known about the job, so it waits for the next pass.
                    logger.warning(
                        "could not describe a stale job's workflow",
                        extra={"job_id": job.id},
                        exc_info=True,
                    )
                    continue
                status = None  # gone from the server's retention
            if status == WorkflowExecutionStatus.RUNNING:
                continue
            job.state = "failed"
            job.finished_at = now()
            job.error = (
                f"the pipeline did not finish within {timeout:g}s"
                if status == WorkflowExecutionStatus.TIMED_OUT
                else "the job's workflow ended without settling it"
            )
            if await asyncio.to_thread(self.store.finish, job):
                self._settled(job, "failed")
                settled += 1
        return settled

    async def _reconcile_forever(self) -> None:
        loop = asyncio.get_running_loop()
        pruned = loop.time()
        while True:
            await asyncio.sleep(self.reconcile_interval)
            try:
                await self.reconcile_once()
                await self.settle_timed_out()
            except Exception:
                logger.exception("the render reconciler's pass failed")
            if loop.time() - pruned >= self.prune_interval:
                pruned = loop.time()
                try:
                    await self.prune()
                except Exception:
                    logger.exception("could not prune settled render jobs")

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
