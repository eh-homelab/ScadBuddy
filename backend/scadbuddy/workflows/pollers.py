"""A task queue's pollers on every partition (#7350).

`DescribeTaskQueue` reports the pollers of one partition, the root's when asked by the
queue's name, and a worker's long polls land on any partition: on 2026-11-24 a live agent
replica polled `agent-tools` partitions 1 to 3 for 30 s and was absent from the root's list
(#7306). The deprecated ENHANCED mode, which merged them, is not used. The agent reads them
the same way (`agent/src/temporal/pollers.ts`)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Final

from temporalio.api.enums.v1 import TaskQueueKind, TaskQueueType
from temporalio.api.taskqueue.v1 import PollerInfo, TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client

TASK_QUEUE_PARTITIONS: Final = 4
"""The read partitions of a normal task queue: Temporal's default
`matching.numTaskqueueReadPartitions`. Prod's dynamic config does not set it
(`scadbuddy-temporal-v2-dynamicconfig`, read 2026-11-24), and the dev server the tests run
has these four too."""


def partition_name(task_queue: str, n: int) -> str:
    """The name Temporal gives partition ``n`` of ``task_queue``; partition 0 is the
    queue's own name."""
    return task_queue if n == 0 else f"/_sys/{task_queue}/{n}"


async def queue_pollers(
    client: Client,
    task_queue: str,
    kind: TaskQueueType.ValueType,
    *,
    timeout: timedelta | None = None,
) -> list[PollerInfo]:
    """The pollers of every partition of ``task_queue`` (of ``kind``), one per identity
    at its latest poll; a poller without an identity is kept as it is. Raises when any
    partition's answer fails."""
    answers = await asyncio.gather(
        *(
            client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(
                    namespace=client.namespace,
                    task_queue=TaskQueue(
                        name=partition_name(task_queue, n),
                        kind=TaskQueueKind.TASK_QUEUE_KIND_NORMAL,
                    ),
                    task_queue_type=kind,
                ),
                timeout=timeout,
            )
            for n in range(TASK_QUEUE_PARTITIONS)
        )
    )
    by_identity: dict[str, PollerInfo] = {}
    anonymous: list[PollerInfo] = []
    for answer in answers:
        for poller in answer.pollers:
            if not poller.identity:
                anonymous.append(poller)
                continue
            known = by_identity.get(poller.identity)
            if known is None or known.last_access_time.ToNanoseconds() < (
                poller.last_access_time.ToNanoseconds()
            ):
                by_identity[poller.identity] = poller
    return [*by_identity.values(), *anonymous]
