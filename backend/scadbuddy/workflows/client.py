from __future__ import annotations

from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkerDeploymentVersion

from scadbuddy.workflows.activities import RenderActivities
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline

RENDER_TASK_QUEUE_DEFAULT = "render"
DEPLOYMENT_NAME = "scadbuddy-render"


async def connect(address: str, namespace: str) -> Client:
    return await Client.connect(
        address, namespace=namespace, data_converter=pydantic_data_converter
    )


def render_worker(
    client: Client,
    task_queue: str,
    activities: RenderActivities,
    *,
    build_id: str,
    max_concurrent_activities: int,
) -> Worker:
    """The render worker (spec §3.5): versioned by build id so a rolling deploy lets
    old workers drain the workflows they started."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[TemplatePipeline, RenderPiece],
        activities=activities.all(),
        max_concurrent_activities=max_concurrent_activities,
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name=DEPLOYMENT_NAME, build_id=build_id),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
    )


__all__ = [
    "DEPLOYMENT_NAME",
    "RENDER_TASK_QUEUE_DEFAULT",
    "connect",
    "pydantic_data_converter",
    "render_worker",
]
