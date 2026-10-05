from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio.api.workflowservice.v1 import (
    CountWorkflowExecutionsRequest,
    DescribeWorkerDeploymentRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import VersioningBehavior
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkerDeploymentVersion
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from scadbuddy.bambuddy.runs import PrintRunStore
from scadbuddy.workflows.activities import RenderActivities
from scadbuddy.workflows.pipelines import RenderPiece, RenderPreview, TemplatePipeline
from scadbuddy.workflows.printing import PrintRunWorkflow

RENDER_TASK_QUEUE_DEFAULT = "render"
DEPLOYMENT_NAME = "scadbuddy-render"
RPC_TIMEOUT = timedelta(seconds=10)

logger = logging.getLogger(__name__)


async def connect(address: str, namespace: str, *, lazy: bool = False) -> Client:
    """``lazy`` connects on the first call instead of here (the API, which must boot
    with Temporal down); the worker connects eagerly and fails fast. Every client
    traces (spec 2026-10-01 §4): context rides in workflow headers, and a worker built
    on this client takes the same interceptor."""
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=pydantic_data_converter,
        lazy=lazy,
        interceptors=[TracingInterceptor()],
    )


def connect_lazily(address: str, namespace: str) -> Client:
    """The API's client, built synchronously: `create_app` runs inside uvicorn's loop,
    where nothing can be awaited. A lazy connect never suspends, so driving the
    coroutine once returns the client; it connects on its first call."""
    coro = connect(address, namespace, lazy=True)
    try:
        coro.send(None)
    except StopIteration as done:
        client: Client = done.value
        return client
    coro.close()
    raise RuntimeError("a lazy Temporal connect suspended")


def render_worker(
    client: Client,
    task_queue: str,
    activities: RenderActivities,
    *,
    build_id: str,
    max_concurrent_activities: int,
    graceful_shutdown_timeout: timedelta = timedelta(),
) -> Worker:
    """The render worker (spec §3.5): versioned by build id so a rolling deploy lets
    old workers drain the workflows they started."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[TemplatePipeline, RenderPiece, RenderPreview],
        activities=activities.all(),
        # The interceptor's workflow spans run inside the sandbox; OpenTelemetry's
        # module state must be the process's, not a sandboxed copy.
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules("opentelemetry")
        ),
        max_concurrent_activities=max_concurrent_activities,
        graceful_shutdown_timeout=graceful_shutdown_timeout,
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name=DEPLOYMENT_NAME, build_id=build_id),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
    )


def print_worker(
    client: Client,
    task_queue: str,
    activities: Sequence[Callable[..., Any]],
    *,
    graceful_shutdown_timeout: timedelta = timedelta(seconds=30),
) -> Worker:
    """The ``bambuddy`` worker (#1052, spec 2026-10-01 §5.5). Unversioned: a change to
    ``PrintRun`` that alters its commands is made with ``workflow.patched``, so a run
    started on the old code finishes on the new. ``tests/test_print_replay.py`` replays
    committed histories to hold that; a new activity name also needs ``patched``, or an
    old replica takes its task and fails it as unregistered."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[PrintRunWorkflow],
        activities=activities,
        graceful_shutdown_timeout=graceful_shutdown_timeout,
    )


async def make_current(client: Client, *, namespace: str, build_id: str) -> None:
    """Make `build_id` the deployment's current version: a versioned worker takes new
    workflows only once its version is current."""
    await client.workflow_service.set_worker_deployment_current_version(
        SetWorkerDeploymentCurrentVersionRequest(
            namespace=namespace,
            deployment_name=DEPLOYMENT_NAME,
            build_id=build_id,
            ignore_missing_task_queues=True,
            allow_no_pollers=True,
        ),
        timeout=RPC_TIMEOUT,
    )


async def is_current(client: Client, *, namespace: str, build_id: str) -> bool:
    """Whether `build_id` is the deployment's current version. Then any worker of this
    build serves the runs pinned to it, so one that stops need not drain (#874)."""
    response = await client.workflow_service.describe_worker_deployment(
        DescribeWorkerDeploymentRequest(namespace=namespace, deployment_name=DEPLOYMENT_NAME),
        timeout=RPC_TIMEOUT,
    )
    current = response.worker_deployment_info.routing_config.current_deployment_version
    return current.build_id == build_id


async def drained(client: Client, *, namespace: str, build_id: str) -> bool:
    """Whether no workflow pinned to `build_id` is still running. A visibility count,
    not `DescribeWorkerDeploymentVersion`'s drainage status: that one is absent while
    the version is current and still says DRAINING well after its last run has ended."""
    response = await client.workflow_service.count_workflow_executions(
        CountWorkflowExecutionsRequest(
            namespace=namespace,
            query=(
                f'TemporalWorkerDeploymentVersion="{DEPLOYMENT_NAME}:{build_id}"'
                ' AND ExecutionStatus="Running"'
            ),
        ),
        timeout=RPC_TIMEOUT,
    )
    return response.count == 0


__all__ = [
    "DEPLOYMENT_NAME",
    "RENDER_TASK_QUEUE_DEFAULT",
    "connect",
    "drained",
    "is_current",
    "make_current",
    "print_worker",
    "pydantic_data_converter",
    "render_worker",
]


#: A run younger than this is left alone: its execution may not have been described yet.
LOST_RUN_GRACE = timedelta(minutes=1)


async def _running(client: Client, workflow_id: str, run_id: str | None) -> bool:
    """Whether that run (or, with no run id, the workflow's latest) is running."""
    try:
        described = await client.get_workflow_handle(workflow_id, run_id=run_id).describe()
    except RPCError as error:
        if error.status != RPCStatusCode.NOT_FOUND:
            raise
        return False
    return described.status == WorkflowExecutionStatus.RUNNING


async def reconcile_lost_runs(
    client: Client, store: PrintRunStore, *, older_than: timedelta = LOST_RUN_GRACE
) -> int:
    """End each ``running`` print run whose execution has closed or is gone (review
    #1061): terminated in the Temporal UI, it never runs ``print_fail``. One still
    running is left to end its row itself, and so is one whose workflow still runs
    under a later run id: a reset continues the same row there. A row a pre-#1052 pod
    inserted and stopped beating is ended too (review #1061 (3) 2), as one that may
    have queued: a stalled pod may yet queue it (review #1316 2a). Returns how many it
    ended."""
    ended = 0
    # One row that cannot be ended is logged and left for the next pass; the rest are
    # still ended (review #1316 (9) 3a).
    for run_id, workflow_id, workflow_run_id in await store.running_executions(older_than):
        try:
            if await _running(client, workflow_id, workflow_run_id) or await _running(
                client, workflow_id, None
            ):
                continue
            if (await store.fail_lost(run_id)).status == "failed":
                ended += 1
        except Exception:
            logger.exception("could not end a lost print run", extra={"run_id": run_id})
    for run_id in await store.stale_pre_1052_runs():
        try:
            if (await store.fail_pre_1052(run_id)).status == "failed":
                ended += 1
        except Exception:
            logger.exception("could not end a pre-#1052 print run", extra={"run_id": run_id})
    return ended
