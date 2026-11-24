"""A task queue's pollers on every partition (#7350): `DescribeTaskQueue` reports the
root partition's pollers when asked by the queue's name, and a worker's long polls land on
any partition. On 2026-11-24 a live agent replica polled `agent-tools` partitions 1 to 3
and was absent from the root's list (#7306)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from temporalio.api.deployment.v1 import WorkerDeploymentOptions
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest, DescribeTaskQueueResponse
from temporalio.client import Client

from scadbuddy.filament.profile_fetch import PollerCache, TemporalQueueProbe
from scadbuddy.workflows.client import DEPLOYMENT_NAME, pollers_elsewhere
from scadbuddy.workflows.pollers import TASK_QUEUE_PARTITIONS, queue_pollers


def _seen(seconds_ago: float) -> Timestamp:
    seen = Timestamp()
    seen.FromDatetime(datetime.now(UTC) - timedelta(seconds=seconds_ago))
    return seen


def _poller(identity: str, seconds_ago: float) -> PollerInfo:
    return PollerInfo(
        identity=identity,
        last_access_time=_seen(seconds_ago),
        deployment_options=WorkerDeploymentOptions(deployment_name=DEPLOYMENT_NAME, build_id="b1"),
    )


def _server(
    partitions: dict[tuple[str, int], list[PollerInfo]], asked: list[str] | None = None
) -> Client:
    """A Temporal whose pollers are listed by (partition name, task queue type)."""

    async def describe(request: DescribeTaskQueueRequest, **_: object) -> DescribeTaskQueueResponse:
        if asked is not None:
            asked.append(request.task_queue.name)
        key = (request.task_queue.name, request.task_queue_type)
        return DescribeTaskQueueResponse(pollers=partitions.get(key, []))

    return cast(
        Client,
        SimpleNamespace(
            namespace="default", workflow_service=SimpleNamespace(describe_task_queue=describe)
        ),
    )


WORKFLOW = TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW
ACTIVITY = TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY


async def test_a_worker_polling_only_a_non_root_partition_serves_elsewhere() -> None:
    """A stopping worker can leave its pinned runs to it (#4869)."""
    client = _server(
        {
            ("render", WORKFLOW): [_poller("me@pod-a", 0)],
            ("/_sys/render/2", WORKFLOW): [_poller("other@pod-b", 1)],
        }
    )
    elsewhere = await pollers_elsewhere(
        client, "render", deployment_name=DEPLOYMENT_NAME, build_id="b1", identities={"me@pod-a"}
    )
    assert elsewhere == {"other@pod-b"}


async def test_a_profile_fetch_worker_polling_only_a_non_root_partition_is_seen() -> None:
    """Else a running fetch ends "no profile-fetch worker picked the fetch up"."""
    client = _server({("/_sys/profile-fetch/3", ACTIVITY): [_poller("w@pod-f", 1)]})
    probe = TemporalQueueProbe(client, "profile-fetch", PollerCache())
    assert await probe.has_pollers() is True


async def test_every_partition_is_asked_and_each_identity_kept_once_at_its_latest_poll() -> None:
    asked: list[str] = []
    anonymous = PollerInfo(last_access_time=_seen(2))
    client = _server(
        {
            ("q", WORKFLOW): [_poller("w", 50), anonymous],
            ("/_sys/q/1", WORKFLOW): [_poller("w", 5)],
            ("/_sys/q/3", WORKFLOW): [_poller("v", 9)],
        },
        asked,
    )
    pollers = await queue_pollers(client, "q", WORKFLOW)
    assert asked == ["q", "/_sys/q/1", "/_sys/q/2", "/_sys/q/3"]
    assert len(asked) == TASK_QUEUE_PARTITIONS
    now = datetime.now(UTC)
    ages = [
        (p.identity, round((now - p.last_access_time.ToDatetime(tzinfo=UTC)).total_seconds()))
        for p in pollers
    ]
    assert ages == [("w", 5), ("v", 9), ("", 2)]


async def test_one_partition_that_fails_fails_the_read() -> None:
    async def describe(request: DescribeTaskQueueRequest, **_: object) -> DescribeTaskQueueResponse:
        if request.task_queue.name == "/_sys/q/2":
            raise RuntimeError("unavailable")
        return DescribeTaskQueueResponse()

    client = cast(
        Client,
        SimpleNamespace(
            namespace="default", workflow_service=SimpleNamespace(describe_task_queue=describe)
        ),
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        await queue_pollers(client, "q", WORKFLOW)
