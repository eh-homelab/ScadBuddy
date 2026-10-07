"""RenderService: renders on Temporal (spec 2026-09-27 §3.3), in the command shape
of spec 2026-10-01 §4.5 (#1053).

A submit starts `TemplatePipeline` as ``render-<render_key>`` with update-with-start:
the workflow's first activity inserts the `render_jobs` row and its `accepted`
Update answers it, so a row exists only once its execution does and nothing needs
reconciling. An identical request that reaches the open execution joins it as one more
claim (workflow state); a supersede sends the job's execution its `release` Update,
and the last one cancels it. The projection publishes every ``job.*``
event in its own transaction; nothing is announced here.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import timedelta
from typing import Any

from fastapi import status
from temporalio.client import (
    Client,
    WorkflowUpdateFailedError,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.config import ACTIVITY_TIMEOUT_MARGIN, Config
from scadbuddy.core.metrics import Metrics, RenderOutcome, SettlePass
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.core.tracing import (
    current_traceparent,
    failure_class,
    link_to,
    span,
)
from scadbuddy.library.previews import source_key
from scadbuddy.render.inputs import legacy_inputs
from scadbuddy.render.job_models import (
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
    LEGACY_GRACE,
    VISIBILITY_GRACE,
    JobProjection,
    execution_gone,
    legacy_unrun,
    open_runs,
    run_closed,
    workflow_id_for,
    workflow_id_for_key,
)
from scadbuddy.render.schema import ParamValue
from scadbuddy.store.snapshots import SnapshotStore
from scadbuddy.workflows.commands import (
    COMMAND_ANSWER_DEADLINE,
    CONNECT_MARGIN_SECONDS,
    CommandClosedError,
    CommandStillAcceptingError,
    TemporalBusyError,
    TemporalRefusedError,
    TemporalUnavailableError,
    late_answer,
    start_command,
)
from scadbuddy.workflows.models import (
    CLOSING,
    RELEASE_UPDATE,
    ReleaseAnswer,
    RenderAnswer,
    RenderStart,
)
from scadbuddy.workflows.pipelines import PREVIEW_TRANSFER, RenderPreview
from scadbuddy.workflows.print_models import ACCEPTED_UPDATE

logger = logging.getLogger(__name__)

#: How long a request waits on one Temporal call it makes besides the start (a release,
#: a describe): the SDK's own retry budget is ~10 s per call.
RPC_TIMEOUT = timedelta(seconds=5)
#: What a settle pass waits on one describe: `rpc_timeout` does not bound a lazy
#: client's first connect, which retries for minutes on its own (review #1066 1.1).
DESCRIBE_BOUND = RPC_TIMEOUT.total_seconds() + CONNECT_MARGIN_SECONDS
#: The most describes one settle pass makes. A row is described only when the listing
#: of open runs leaves its run out (closed, or Visibility behind), which is rare; any
#: past this wait for the next pass.
SETTLE_DESCRIBES = 50
#: How long a submit that reached an execution closing on its last release waits for it
#: to close before it starts again (ruling 10 of the phase 2b plan).
CLOSING_WAIT = 5.0
#: How long a release waits on its Update: `rpc_timeout` bounds each poll, not the
#: Update, and the SDK polls again (review #1066 (8) 1).
RELEASE_BOUND = RPC_TIMEOUT.total_seconds() + CONNECT_MARGIN_SECONDS
#: Every Temporal call a submit makes (both starts, the wait between them, the release)
#: answers within this; past it one describe (`DESCRIBE_SECONDS`) says whether the
#: request is still accepting or Temporal unavailable, and the two stay below Envoy's
#: 15 s route timeout. Either way the client sends it again with the same key (review
#: #1066 (8) 1).
SUBMIT_DEADLINE = COMMAND_ANSWER_DEADLINE.total_seconds() + CONNECT_MARGIN_SECONDS
#: The codes gRPC itself ends a call with: they say nothing of whether the start
#: reached Temporal (review #1066 (8) 2).
ENDED_RPC = frozenset({RPCStatusCode.DEADLINE_EXCEEDED, RPCStatusCode.CANCELLED})

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
        #: Upsert §4.2's Search Attributes (registered on the cluster first).
        self.search_attributes = search_attributes
        self._listened_before = False
        self._boot: asyncio.Task[None] | None = None
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
        # The boot pass, in the background: the lifespan never waits on Temporal
        # (review #1066 1.1). It contains its own failures; the housekeeping prune
        # settles again (#1054).
        self._boot = asyncio.create_task(self.settle())

    async def aclose(self) -> None:
        """Only the boot pass runs in the background: housekeeping prunes (#1054)."""
        if self._boot is not None:
            self._boot.cancel()
            with suppress(asyncio.CancelledError):
                await self._boot
            self._boot = None

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
        request_id: str | None = None,
    ) -> Job:
        """Start the job's execution, or join the open one rendering the same content,
        and answer the row its first activity wrote. ``request_id`` (the request's
        `Idempotency-Key`) makes a re-sent request the same claim, not another: it is
        the `accepted` Update's id, which Temporal answers with its first outcome on
        the open run and, once that run has closed, on the closed one (until another
        run of the key starts)."""
        refusal: Exception | None = None
        with span("render.submit", attributes={"scadbuddy.slug": slug}) as current:
            try:
                job, coalesced = await self._submit(
                    slug,
                    params,
                    model_version=model_version,
                    supersedes=supersedes,
                    inputs=inputs,
                    request_id=request_id,
                )
            except (ApiError, QueueFullError) as error:
                if isinstance(error, ApiError) and error.status >= 500:
                    raise
                # The client's answer (a 413, a full queue's 429), not a failure: the
                # span names it and ends UNSET, as the server span does (#1245).
                current.set_attribute("scadbuddy.failure_class", failure_class(error))
                refusal = error
            else:
                current.set_attribute("scadbuddy.job_id", job.id)
                current.set_attribute("scadbuddy.coalesced", coalesced)
                # A coalesced request links to the trace of the render it joined.
                if (
                    coalesced
                    and job.traceparent != current_traceparent()
                    and (link := link_to(job.traceparent)) is not None
                ):
                    current.add_link(link.context)
                return job
        raise refusal

    async def _submit(
        self,
        slug: str,
        params: Mapping[str, ParamValue],
        *,
        model_version: str | None = None,
        supersedes: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> tuple[Job, bool]:
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
        previous = await self._superseded(supersedes, slug) if supersedes else None
        start = RenderStart(
            slug=slug,
            params=dict(params),
            inputs=dict(inputs) if inputs is not None else legacy_inputs(params),
            model_version=model_version,
            render_key=render_key(slug, params, model_version),
            max_pending=self.config.render_queue_max,
            search_attributes=self.search_attributes,
            traceparent=current_traceparent(),
            supersedes=previous.id if previous is not None else None,
        )
        size = len(pydantic_data_converter.payload_converter.to_payload(start).data)
        if size > MAX_WORKFLOW_INPUT_BYTES:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"these parameters make a render request of {size} bytes; the most a render"
                f" can carry is {MAX_WORKFLOW_INPUT_BYTES}",
            )
        if previous is not None and previous.workflow_id == workflow_id_for_key(start.render_key):
            # The same render it replaces: answered with it, as the row did (no claim).
            self.metrics.render_coalesced.inc()
            return previous, True
        try:
            answer = await self._started(start, previous, request_id)
        except (CommandStillAcceptingError, CommandClosedError):
            # Re-sent by the client with the same key: pending, not an error (review
            # #1066 (5) 3.1).
            self.metrics.render_accept_pending.inc()
            raise
        except Exception:
            # Answered as a 503 or 500, and counted (review #1066 4.1).
            self.metrics.store_errors.labels("start_workflow").inc()
            raise
        if answer.queue_full is not None:
            self.metrics.render_rejected.inc()
            raise QueueFullError(answer.queue_full, self.retry_after())
        assert answer.job is not None
        if answer.coalesced:
            self.metrics.render_coalesced.inc()
        else:
            self.metrics.render_submitted.inc()
        return answer.job, answer.coalesced

    async def _superseded(self, job_id: str, slug: str) -> Job | None:
        try:
            job = await asyncio.to_thread(self.store.read, job_id)
        except JobNotFoundError:
            return None
        if job.slug != slug or job.state not in ("pending", "running"):
            return None
        return job

    async def _started(
        self, start: RenderStart, previous: Job | None, request_id: str | None
    ) -> RenderAnswer:
        """`_accepted`, then the supersede, under one `SUBMIT_DEADLINE`. Past it the
        request is still accepting if the execution exists; if Temporal cannot say (a
        client that never connected, say), it is unavailable, as `start_command` answers
        a call that outlived its own bound."""
        bound = asyncio.timeout(SUBMIT_DEADLINE)
        try:
            async with bound:
                answer = await self._accepted(start, request_id)
                # Started first, so a refused submit supersedes nothing.
                if answer.job is not None and previous is not None:
                    await self._supersede(previous, request_id)
        except TimeoutError as error:
            if not bound.expired():
                raise
            raise await late_answer(self.client, workflow_id_for_key(start.render_key)) from error
        return answer

    async def _accepted(self, start: RenderStart, request_id: str | None) -> RenderAnswer:
        """The `accepted` answer of ``render-<render_key>``, started or joined. An
        execution closing (its last claim released, or its render raised) is waited out
        once and started again; still closing, the request is still accepting, and the
        client sends it again with the same key."""
        workflow_id = workflow_id_for_key(start.render_key)
        for _ in range(2):
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
                    update_id=request_id,
                )
            except (TemporalBusyError, TemporalRefusedError) as error:
                # `start_command` classifies its `RPCError` (#1316); the cause says which.
                cause = error.__cause__
                if not isinstance(cause, RPCError):
                    raise
                if cause.status in ENDED_RPC:
                    # The start may have reached Temporal: still accepting if the
                    # execution exists (review #1066 (8) 2).
                    raise await late_answer(self.client, workflow_id) from cause
                # An Update that reached the execution as it completed is aborted, the
                # same race as a `CLOSING` rejection (review #1066 (11) 1). A missing
                # namespace is NOT_FOUND too: configuration, raised at once.
                if not execution_gone(cause):
                    raise
                answer = RenderAnswer(closing=True)
            except WorkflowUpdateFailedError as error:
                # Rejected by a closing run, so its id is not in that run's history:
                # sent again after the close, it starts the next run (review #1066 (7) 1).
                cause = error.cause
                if not (isinstance(cause, ApplicationError) and cause.type == CLOSING):
                    raise
                answer = RenderAnswer(closing=True)
            if not answer.closing:
                return answer
            with suppress(Exception):
                async with asyncio.timeout(CLOSING_WAIT):
                    await self.client.get_workflow_handle(workflow_id).result()
        raise CommandStillAcceptingError(workflow_id)

    async def _supersede(self, job: Job, request_id: str | None) -> None:
        """Release the job a new render replaces. The new render is started either way:
        a release that fails is a warning, never the submit's failure."""
        try:
            await self._release(job, request_id)
        except Exception as error:
            logger.warning(
                "could not release a render's claim",
                extra={"job_id": job.id, "error_type": type(error).__name__},
                exc_info=True,
            )

    async def _release(self, job: Job, request_id: str | None) -> Job | None:
        """Take the superseding request's claim off ``job``: the cancelled job when it
        was the last. With ``request_id`` the Update id is the request's, so a re-sent
        request releases once (review #1066 finding 1)."""
        if job.workflow_run_id is None:
            # A row an older release inserted, whose workflow is `render-<id>`.
            legacy = await asyncio.to_thread(
                self.store.release_claim, job.id, slug=job.slug, error=SUPERSEDED_ERROR
            )
            if legacy is not None:
                self._settled(legacy, "superseded")
                await self._cancel_workflow(legacy)
            return legacy
        assert job.workflow_id is not None
        handle = self.client.get_workflow_handle(job.workflow_id, run_id=job.workflow_run_id)
        try:
            async with asyncio.timeout(RELEASE_BOUND):
                answer: ReleaseAnswer = await handle.execute_update(
                    RELEASE_UPDATE,
                    "superseded",
                    id=f"{request_id}:release:{job.id}" if request_id is not None else None,
                    result_type=ReleaseAnswer,
                    rpc_timeout=RPC_TIMEOUT,
                )
        except RPCError as error:
            if execution_gone(error):
                return None  # it has closed: settled, nothing to release
            self.metrics.store_errors.labels("cancel_workflow").inc()
            raise TemporalUnavailableError(job.workflow_id) from error
        except (WorkflowUpdateRPCTimeoutOrCancelledError, TimeoutError) as error:
            # Past its bound: the Update may have reached the execution, and may still
            # release the claim.
            self.metrics.store_errors.labels("cancel_workflow").inc()
            raise CommandStillAcceptingError(job.workflow_id) from error
        if answer.cancelled is not None:
            self._settled(answer.cancelled, "superseded")
        return answer.cancelled

    async def settle(self) -> None:
        """In the background at start and on every housekeeping prune: fail the rows
        nothing will settle, which would otherwise hold their render key and count
        towards the queue (review #1066 1.2). A failed pass is logged and counted; the
        next one tries again."""
        passes: tuple[tuple[SettlePass, Callable[[], Awaitable[list[str]]]], ...] = (
            ("legacy", self.settle_legacy),
            ("closed", self.settle_closed),
        )
        for settle_pass, settle in passes:
            try:
                await settle()
            except Exception:
                self.metrics.settle_errors.labels(settle_pass).inc()
                logger.exception("could not settle the renders nothing will run")

    async def settle_closed(self) -> list[str]:
        """Fail the unsettled rows whose run closed without settling them (terminated
        by hand, or timed out). Only a row past `VISIBILITY_GRACE` whose run the
        listing of open runs leaves out is described (review #1066 (9) 4)."""
        jobs = await asyncio.to_thread(self.store.unsettled, VISIBILITY_GRACE)
        running = await self._open_runs("closed") if jobs else None
        if running is None:
            return []
        candidates = [job for job in jobs if (job.workflow_id, job.workflow_run_id) not in running]
        closed: list[str] = []
        for job in candidates[:SETTLE_DESCRIBES]:
            try:
                async with asyncio.timeout(DESCRIBE_BOUND):
                    if not await run_closed(self.client, job, rpc_timeout=RPC_TIMEOUT):
                        continue
            except (RPCError, TimeoutError) as error:
                self._unanswered("closed", error)
                break
            closed.append(job.id)
        if closed:
            settled = await asyncio.to_thread(self.store.fail_closed, closed)
            self._settle_failed("closed", settled)
            logger.warning(
                "failed the renders whose workflow closed without settling them",
                extra={"job_ids": [job.id for job in settled]},
            )
        return closed

    async def settle_legacy(self) -> list[str]:
        """Fail the rows an older release inserted that no workflow will settle: pending
        and never run, or running when its workflow closed. Only a row past
        `LEGACY_GRACE` is judged: a younger one may be between the older API's insert
        and its start (review #1066 1.2). A row whose workflow the listing of open runs
        has is left alone without a describe (review #1066 (9) 4)."""
        jobs = await asyncio.to_thread(self.store.legacy_unsettled, LEGACY_GRACE)
        running = await self._open_runs("legacy") if jobs else None
        if running is None:
            return []
        open_ids = {workflow_id for workflow_id, _ in running}
        candidates = [job for job in jobs if job.workflow_id not in open_ids]
        failed: list[str] = []
        for job in candidates[:SETTLE_DESCRIBES]:
            try:
                async with asyncio.timeout(DESCRIBE_BOUND):
                    if not await legacy_unrun(self.client, job, rpc_timeout=RPC_TIMEOUT):
                        continue
            except (RPCError, TimeoutError) as error:
                self._unanswered("legacy", error)
                break
            failed.append(job.id)
        if failed:
            settled = await asyncio.to_thread(self.store.fail_legacy, failed)
            self._settle_failed("legacy", settled)
            logger.warning(
                "failed the renders an older release left with no workflow to settle them",
                extra={"job_ids": [job.id for job in settled]},
            )
        return failed

    async def _open_runs(self, settle_pass: SettlePass) -> set[tuple[str, str]] | None:
        """The open render runs, or None when Temporal does not answer in time."""
        try:
            async with asyncio.timeout(DESCRIBE_BOUND):
                return await open_runs(self.client, rpc_timeout=RPC_TIMEOUT)
        except (RPCError, TimeoutError) as error:
            self._unanswered(settle_pass, error)
            return None

    def _unanswered(self, settle_pass: SettlePass, error: Exception) -> None:
        logger.warning(
            "could not ask Temporal about the unsettled renders; the next pass tries again",
            extra={"settle_pass": settle_pass, "error_type": type(error).__name__},
        )
        self.metrics.settle_errors.labels(settle_pass).inc()

    def _settle_failed(self, settle_pass: SettlePass, jobs: list[Job]) -> None:
        self.metrics.settle_failed.labels(settle_pass).inc(len(jobs))
        for job in jobs:
            self._settled(job, "failed")

    async def prune(self) -> None:
        """Settled jobs past `job_ttl` (and their blob refs), and revision exports; then
        the rows nothing will settle (review #1066 1.2), even when the prune fails
        (review #1095b 1)."""
        ttl = self.config.job_ttl
        try:
            await asyncio.to_thread(self.store.prune, ttl)
            await asyncio.to_thread(prune_revision_exports, self.paths, ttl)
        finally:
            await self.settle()

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
            if execution_gone(error):
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
