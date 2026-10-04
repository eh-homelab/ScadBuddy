"""RenderService: renders on Temporal (spec 2026-09-27 §3.3), in the command shape
of spec 2026-10-01 §4.5 (#1053).

A submit starts `TemplatePipeline` as ``render-<render_key>`` with update-with-start:
the workflow's first activity inserts the `render_jobs` row and its `accepted`
Update answers it, so a row exists only once its execution does and nothing needs
reconciling. An identical request that reaches the open execution joins it as one more
claim (workflow state); a supersede or a withdrawal sends the job's execution its
`release` Update, and the last one cancels it. The projection publishes every ``job.*``
event in its own transaction; nothing is announced here.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from contextlib import suppress
from datetime import timedelta
from typing import Any

from fastapi import status
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.config import ACTIVITY_TIMEOUT_MARGIN, Config
from scadbuddy.core.metrics import Metrics, RenderOutcome
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.previews import source_key
from scadbuddy.render.inputs import legacy_inputs
from scadbuddy.render.job_models import (
    CANCELLED_ERROR,
    SUPERSEDED_ERROR,
    Job,
    JobNotFoundError,
    QueueFullError,
    now,
    render_key,
)
from scadbuddy.render.jobs import (
    INITIAL_RENDER_ESTIMATE,
    SnapshotUnavailableError,
    prune_revision_exports,
)
from scadbuddy.render.projection import (
    LEGACY_UNSTARTED_ERROR,
    JobProjection,
    workflow_id_for,
    workflow_id_for_key,
)
from scadbuddy.render.schema import ParamValue
from scadbuddy.store.snapshots import SnapshotStore
from scadbuddy.workflows.commands import CommandStillAcceptingError, start_command
from scadbuddy.workflows.models import RELEASE_UPDATE, ReleaseAnswer, RenderAnswer, RenderStart
from scadbuddy.workflows.pipelines import PREVIEW_TRANSFER, RenderPreview
from scadbuddy.workflows.print_models import ACCEPTED_UPDATE

logger = logging.getLogger(__name__)

#: How long a request waits on one Temporal call it makes besides the start (a release,
#: a describe): the SDK's own retry budget is ~10 s per call.
RPC_TIMEOUT = timedelta(seconds=5)
#: How old a legacy pending row with no workflow must be before `settle_legacy` fails
#: it: a few of the older API's own start timeouts.
LEGACY_GRACE = 6 * RPC_TIMEOUT
#: How long a submit that reached an execution closing on its last release waits for it
#: to close before it starts again (ruling 10 of the phase 2b plan).
CLOSING_WAIT = 5.0

#: The largest request a submit sends as a workflow input. Temporal refuses a payload
#: over 2 MiB outright and warns past 512 KiB; a start it refuses would never succeed.
MAX_WORKFLOW_INPUT_BYTES = 1024 * 1024


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
        prune_interval: float = 300.0,
        search_attributes: bool = False,
    ) -> None:
        self.store = projection
        self.client = client
        #: Set in the lifespan once the store is built (Task 8), as `client` is.
        self.snapshots: SnapshotStore | None = None
        self.task_queue = task_queue
        self.config = config
        self.paths = paths
        self.metrics = metrics
        #: How often settled rows are pruned: they hold their blobs' refs until they go.
        self.prune_interval = prune_interval
        #: Upsert §4.2's Search Attributes (registered on the cluster first).
        self.search_attributes = search_attributes
        self._pruner: asyncio.Task[None] | None = None
        self._listened_before = False
        metrics.store_info.labels(projection.backend).set(1)
        self._publish_limits()

    def reconfigure(self, config: Config) -> None:
        """A live settings change (#322): the next submit and preview read ``config``,
        and the exported limits say what is in effect."""
        self.config = config
        self._publish_limits()

    def _publish_limits(self) -> None:
        self.metrics.queue_depth_slo.set(self.config.render_queue_depth_slo)
        self.metrics.queue_max.set(self.config.render_queue_max)
        self.metrics.latency_slo.set(self.config.render_latency_slo)

    async def start(self) -> None:
        self.store.listener(on_state=self._listener_state)
        # A failed pass must not stop the boot: the next boot tries again.
        try:
            await self.settle_legacy()
        except Exception:
            logger.exception("could not settle the renders an older release left pending")
        self._pruner = asyncio.create_task(self._prune_forever())

    async def aclose(self) -> None:
        if self._pruner is not None:
            self._pruner.cancel()
            with suppress(asyncio.CancelledError):
                await self._pruner
            self._pruner = None

    def _memo(self) -> dict[str, Any]:
        return {"activity_timeout": self.config.activity_timeout}

    async def submit(
        self,
        slug: str,
        params: Mapping[str, ParamValue],
        *,
        model_version: str | None = None,
        supersedes: str | None = None,
        inputs: Mapping[str, Any] | None = None,
    ) -> Job:
        """Start the job's execution, or join the open one rendering the same content,
        and answer the row its first activity wrote."""
        if self.snapshots is not None:
            # The bambuddy store (spec §6.1): workers read the source from the store,
            # so every job names a revision whose snapshot exists before it starts.
            model_version = await self.snapshots.pin(slug, model_version)
            if model_version is None:
                # Started without one, the worker would look for a live source it has not got.
                raise SnapshotUnavailableError(
                    f"no commit of {slug} to snapshot for its render: the API has no git"
                    " history, or the template was never committed"
                )
        start = RenderStart(
            slug=slug,
            params=dict(params),
            inputs=dict(inputs) if inputs is not None else legacy_inputs(params),
            model_version=model_version,
            render_key=render_key(slug, params, model_version),
            max_pending=self.config.render_queue_max,
            search_attributes=self.search_attributes,
        )
        size = len(pydantic_data_converter.payload_converter.to_payload(start).data)
        if size > MAX_WORKFLOW_INPUT_BYTES:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"these parameters make a render request of {size} bytes; the most a render"
                f" can carry is {MAX_WORKFLOW_INPUT_BYTES}",
            )
        previous = await self._superseded(supersedes, slug) if supersedes else None
        if previous is not None and previous.workflow_id == workflow_id_for_key(start.render_key):
            # The same render it replaces: answered with it, as the row did (no claim).
            self.metrics.render_coalesced.inc()
            return previous
        answer = await self._accepted(start)
        if answer.queue_full is not None:
            self.metrics.render_rejected.inc()
            raise QueueFullError(answer.queue_full, self.retry_after())
        assert answer.job is not None
        # Started first, so a refused submit supersedes nothing.
        if previous is not None:
            await self._release(previous, "superseded")
        if answer.coalesced:
            self.metrics.render_coalesced.inc()
        else:
            self.metrics.render_submitted.inc()
        return answer.job

    async def _superseded(self, job_id: str, slug: str) -> Job | None:
        try:
            job = await asyncio.to_thread(self.store.read, job_id)
        except JobNotFoundError:
            return None
        if job.slug != slug or job.state not in ("pending", "running"):
            return None
        return job

    async def _accepted(self, start: RenderStart) -> RenderAnswer:
        """The `accepted` answer of ``render-<render_key>``, started or joined. An
        execution closing on its last release is waited out once and started again."""
        workflow_id = workflow_id_for_key(start.render_key)
        for attempt in range(2):
            try:
                answer = await start_command(
                    self.client,
                    "TemplatePipeline",
                    start,
                    id=workflow_id,
                    task_queue=self.task_queue,
                    update=ACCEPTED_UPDATE,
                    result_type=RenderAnswer,
                    reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    memo=self._memo(),
                )
            except RPCError as error:
                # An Update that reached the execution as it completed is aborted.
                if error.status != RPCStatusCode.NOT_FOUND or attempt:
                    raise
                answer = RenderAnswer(closing=True)
            if not answer.closing:
                return answer
            with suppress(Exception):
                async with asyncio.timeout(CLOSING_WAIT):
                    await self.client.get_workflow_handle(workflow_id).result()
        raise CommandStillAcceptingError(workflow_id)

    async def cancel(self, job_id: str, *, slug: str) -> Job | None:
        """Withdraw one request for the job; the last one cancels it."""
        job = await self._superseded(job_id, slug)
        if job is None:
            return None
        return await self._release(job, "withdrawn")

    async def _release(self, job: Job, reason: str) -> Job | None:
        """Take one claim off ``job``: the cancelled job when it was the last."""
        if job.workflow_run_id is None:
            # A row an older release inserted, whose workflow is `render-<id>`.
            error = SUPERSEDED_ERROR if reason == "superseded" else CANCELLED_ERROR
            legacy = await asyncio.to_thread(
                self.store.release_claim, job.id, slug=job.slug, error=error
            )
            if legacy is not None:
                self._settled(legacy, "superseded")
                await self._cancel_workflow(legacy)
            return legacy
        assert job.workflow_id is not None
        handle = self.client.get_workflow_handle(job.workflow_id, run_id=job.workflow_run_id)
        try:
            answer: ReleaseAnswer = await handle.execute_update(
                RELEASE_UPDATE, reason, result_type=ReleaseAnswer, rpc_timeout=RPC_TIMEOUT
            )
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                return None  # it has closed: settled, nothing to release
            logger.warning(
                "could not release a render's claim",
                extra={"job_id": job.id, "status": error.status.name},
            )
            self.metrics.store_errors.labels("cancel_workflow").inc()
            return None
        except Exception as error:
            # The new render is started either way: this never fails its submit.
            logger.warning(
                "could not release a render's claim",
                extra={"job_id": job.id, "error_type": type(error).__name__},
                exc_info=True,
            )
            self.metrics.store_errors.labels("cancel_workflow").inc()
            return None
        if answer.cancelled is not None:
            self._settled(answer.cancelled, "superseded")
        return answer.cancelled

    async def settle_legacy(self) -> list[str]:
        """Once at start: fail the pending rows an older release inserted that no
        workflow will run. Only a row naming none, past `LEGACY_GRACE`, is certainly
        orphaned; one naming its workflow is left to the older build, which starts it
        after its insert (review #1066 1.2)."""
        stale = await asyncio.to_thread(self.store.legacy_pending, LEGACY_GRACE)
        failed = [job.id for job in stale]
        if failed:
            settled = await asyncio.to_thread(
                self.store.fail_legacy, failed, LEGACY_UNSTARTED_ERROR
            )
            logger.warning(
                "failed the renders an older release left pending with no workflow",
                extra={"job_ids": [job.id for job in settled]},
            )
        return failed

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
        the caller then waits `PREVIEW_TRANSFER` longer. There the run's id names the
        revision too, so a join never spans two commits: a newer commit's call starts its
        own run while an older one finishes, and the scheduler (which stores the image
        under the source key it read first) never gets an older commit's image. On the
        local store the id names the source key the same way."""
        revision: str | None = None
        wait = timeout
        if self.snapshots is not None:
            # The bambuddy store: the worker has no volume, so it renders the snapshot
            # of the last commit (as `submit`), and may first bring it and its fonts in.
            revision = await self.snapshots.pin(slug, None)
            if revision is None:
                # Started without one, the worker would look for a live source it has not got.
                raise SnapshotUnavailableError(
                    f"no commit of {slug} to snapshot for its preview: the API has no git"
                    " history, or the template was never committed"
                )
            wait += PREVIEW_TRANSFER.total_seconds()
            run_id = f"preview-{slug}-{revision[:12]}"
        else:
            # The local store renders the live source, so the id names what it reads
            # (the scheduler's `source_key`): a call after an edit does not join a run
            # of the source before it (#903).
            key = await asyncio.to_thread(source_key, self.paths, slug)
            run_id = f"preview-{slug}" if key is None else f"preview-{slug}-{key[:12]}"
        preview_timeout = timeout + ACTIVITY_TIMEOUT_MARGIN
        handle = await self.client.start_workflow(
            RenderPreview.run,
            args=[slug, revision],
            id=run_id,
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            memo={**self._memo(), "preview_timeout": preview_timeout},
            rpc_timeout=RPC_TIMEOUT,
        )
        png: bytes = await asyncio.wait_for(handle.result(), wait)
        return png

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

    async def _cancel_workflow(self, job: Job) -> None:
        try:
            await self.client.get_workflow_handle(workflow_id_for(job.id)).cancel(
                rpc_timeout=RPC_TIMEOUT
            )
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                # Never started. A closed one accepts the cancel without an error.
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

    async def _prune_forever(self) -> None:
        while True:
            await asyncio.sleep(self.prune_interval)
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
