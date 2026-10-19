"""Whether a test app's in-process render worker serves its task queue yet (the api
`client` wait). Not in `tests.support.temporal`: a workflow test imports that module
inside the workflow sandbox, which refuses what `scadbuddy.workflows.client` imports."""

from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.service import RPCError

from scadbuddy.workflows.client import DEPLOYMENT_NAME, routes_to

#: How long a test's app may take to have its render build serve its queue: what a
#: healthy worker start takes to make its build current, with room for the describe.
BUILD_SERVES_TIMEOUT = 75.0
#: The first wait between two checks of whether it does, doubled after each up to
#: `BUILD_SERVES_MAX_POLL`: under load the dev server answered slowly, and rate-limits
#: the Worker Deployment calls the worker's `make_current` makes beside this wait.
BUILD_SERVES_POLL = 0.25
BUILD_SERVES_MAX_POLL = 2.0


async def build_serves(
    client: Client, task_queue: str, build_id: str, deployment_name: str = DEPLOYMENT_NAME
) -> bool:
    """Whether ``task_queue`` routes its new workflows and activities to ``build_id``,
    as the queue itself sees it: `routes_to`, the routing matching uses. Neither the
    deployment's current version nor the version's list of queues says so: a queue
    registering with a version already current was listed, and current, well before
    its renders were routed. Until then a render started on the queue waits unrouted.
    Any `RPCError` is a no, asked again later."""
    try:
        return await routes_to(
            client, task_queue, deployment_name=deployment_name, build_id=build_id
        )
    except RPCError:
        return False


async def wait_until_build_serves(
    client: Client,
    task_queue: str,
    build_id: str,
    *,
    timeout: float,
    poll: float,
    max_poll: float,
    deployment_name: str = DEPLOYMENT_NAME,
) -> bool:
    """Ask `build_serves` until it holds (True) or ``timeout`` seconds have passed
    (False), ``poll`` seconds apart at first, doubling up to ``max_poll``. The time
    waited is summed on the loop's monotonic clock, each step clamped at zero: this
    host's monotonic clock has stepped back too."""
    loop = asyncio.get_running_loop()
    waited, last = 0.0, loop.time()
    while not await build_serves(client, task_queue, build_id, deployment_name):
        if waited >= timeout:
            return False
        await asyncio.sleep(poll)
        poll = min(poll * 2, max_poll)
        now = loop.time()
        waited += max(0.0, now - last)
        last = now
    return True
