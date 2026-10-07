"""Whether a test app's in-process render worker serves its task queue yet (the api
`client` wait). Not in `tests.support.temporal`: a workflow test imports that module
inside the workflow sandbox, which refuses what `scadbuddy.workflows.client` imports."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio.api.enums.v1 import TaskQueueKind, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.service import RPCError

from scadbuddy.workflows.client import DEPLOYMENT_NAME

#: How long a test's app may take to have its render build serve its queue: the
#: worker's own `MAKE_CURRENT_DEADLINE`, with room for the describe.
BUILD_SERVES_TIMEOUT = 75.0
#: The first wait between two checks of whether it does, doubled after each up to
#: `BUILD_SERVES_MAX_POLL`: under load the dev server answered slowly, and rate-limits
#: the Worker Deployment calls the worker's `make_current` makes beside this wait.
BUILD_SERVES_POLL = 0.25
BUILD_SERVES_MAX_POLL = 2.0


async def build_serves(client: Client, task_queue: str, build_id: str) -> bool:
    """Whether ``task_queue`` routes its new workflows to ``build_id``, as the queue
    itself sees it (`DescribeTaskQueue`'s versioning info: the routing matching uses).
    Neither the deployment's current version nor the version's list of queues says
    so: a queue registering with a version already current was listed, and current,
    well before its renders were routed. Until then a render started on the queue
    waits unrouted. Any `RPCError` is a no, asked again later."""
    try:
        described = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=client.namespace,
                task_queue=TaskQueue(name=task_queue, kind=TaskQueueKind.TASK_QUEUE_KIND_NORMAL),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            ),
            timeout=timedelta(seconds=5),
        )
    except RPCError:
        return False
    current = described.versioning_info.current_deployment_version
    return current.deployment_name == DEPLOYMENT_NAME and current.build_id == build_id


async def wait_until_build_serves(
    client: Client,
    task_queue: str,
    build_id: str,
    *,
    timeout: float,
    poll: float,
    max_poll: float,
) -> bool:
    """Ask `build_serves` until it holds (True) or ``timeout`` seconds have passed
    (False), ``poll`` seconds apart at first, doubling up to ``max_poll``. The time
    waited is summed on the loop's monotonic clock, each step clamped at zero: this
    host's monotonic clock has stepped back too."""
    loop = asyncio.get_running_loop()
    waited, last = 0.0, loop.time()
    while not await build_serves(client, task_queue, build_id):
        if waited >= timeout:
            return False
        await asyncio.sleep(poll)
        poll = min(poll * 2, max_poll)
        now = loop.time()
        waited += max(0.0, now - last)
        last = now
    return True
