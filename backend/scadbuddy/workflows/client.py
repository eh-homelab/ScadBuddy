from __future__ import annotations

from datetime import timedelta

from temporalio.api.workflowservice.v1 import (
    CountWorkflowExecutionsRequest,
    DescribeWorkerDeploymentRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkerDeploymentVersion

from scadbuddy.workflows.activities import RenderActivities
from scadbuddy.workflows.pipelines import RenderPiece, RenderPreview, TemplatePipeline

RENDER_TASK_QUEUE_DEFAULT = "render"
DEPLOYMENT_NAME = "scadbuddy-render"
RPC_TIMEOUT = timedelta(seconds=10)


async def connect(address: str, namespace: str, *, lazy: bool = False) -> Client:
    """``lazy`` connects on the first call instead of here (the API, which must boot
    with Temporal down); the worker connects eagerly and fails fast."""
    return await Client.connect(
        address, namespace=namespace, data_converter=pydantic_data_converter, lazy=lazy
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
        max_concurrent_activities=max_concurrent_activities,
        graceful_shutdown_timeout=graceful_shutdown_timeout,
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name=DEPLOYMENT_NAME, build_id=build_id),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
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
    "pydantic_data_converter",
    "render_worker",
]
