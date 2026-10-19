"""`workflows.client.routes_to` (#4255): a queue routes to a build only when both its
workflow-type and its activity-type copies of the routing say so."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.api.deployment.v1 import WorkerDeploymentVersion
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueueVersioningInfo
from temporalio.api.workflowservice.v1 import (
    DescribeTaskQueueRequest,
    DescribeTaskQueueResponse,
)
from temporalio.client import Client

from scadbuddy.workflows.client import routes_to

WORKFLOW = TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW
ACTIVITY = TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY


def _client(current: dict[int, str]) -> Client:
    """A client whose queue reports ``current[type]`` as its current build, per type."""

    async def describe_task_queue(
        request: DescribeTaskQueueRequest, **kwargs: Any
    ) -> DescribeTaskQueueResponse:
        return DescribeTaskQueueResponse(
            versioning_info=TaskQueueVersioningInfo(
                current_deployment_version=WorkerDeploymentVersion(
                    deployment_name="d", build_id=current[request.task_queue_type]
                )
            )
        )

    service = SimpleNamespace(describe_task_queue=describe_task_queue)
    return cast(Client, SimpleNamespace(namespace="default", workflow_service=service))


@pytest.mark.parametrize(
    ("workflow_build", "activity_build", "routed"),
    [
        ("new", "new", True),
        # The #4255 window: the workflow type has heard of the new build, the activity
        # type has not, so a sweep only the new build has would reach the old one.
        ("new", "old", False),
        ("old", "new", False),
        ("old", "old", False),
    ],
)
async def test_both_queue_types_must_route_to_the_build(
    workflow_build: str, activity_build: str, routed: bool
) -> None:
    client = _client({WORKFLOW: workflow_build, ACTIVITY: activity_build})
    assert await routes_to(client, "q", deployment_name="d", build_id="new") is routed
