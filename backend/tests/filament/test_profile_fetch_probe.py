"""`TemporalQueueProbe` against a fake Temporal client (#6625): the API keeps
DescribeTaskQueue's answer for `POLLERS_CACHE_SECONDS` per queue, so every read of a
fetch's status does not ask Temporal again, and it cancels a run's workflow by its id.
`tests/profile_fetch/test_workflow.py` reads the real queue."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest, DescribeTaskQueueResponse

from scadbuddy.filament.profile_fetch import (
    POLLERS_CACHE_SECONDS,
    PollerCache,
    TemporalQueueProbe,
)


class FakeService:
    def __init__(self, pollers: bool) -> None:
        self.pollers = pollers
        self.fail = False
        #: Only this queue type's read fails (#7513); None for neither.
        self.fail_type: int | None = None
        #: This queue type answers with no pollers, whatever ``pollers`` says (#7578).
        self.empty_type: int | None = None
        self.reads: list[tuple[str, int]] = []

    async def describe_task_queue(self, request: DescribeTaskQueueRequest, **_: object) -> Any:
        # One read per queue and type: its root partition. The others
        # (`pollers.partition_name`) answer alike, unrecorded.
        if not request.task_queue.name.startswith("/_sys/"):
            self.reads.append((request.task_queue.name, request.task_queue_type))
        if self.fail or request.task_queue_type == self.fail_type:
            raise RuntimeError("Temporal is down")
        found = self.pollers and request.task_queue_type != self.empty_type
        return DescribeTaskQueueResponse(pollers=[PollerInfo(identity="w")] if found else [])


class FakeHandle:
    def __init__(self, client: FakeClient, workflow_id: str) -> None:
        self.client = client
        self.workflow_id = workflow_id

    async def cancel(self) -> None:
        self.client.cancelled.append(self.workflow_id)


class FakeClient:
    namespace = "default"

    def __init__(self, pollers: bool = True) -> None:
        self.workflow_service = FakeService(pollers)
        self.cancelled: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return FakeHandle(self, workflow_id)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _queue() -> str:
    return f"profile-fetch-{uuid.uuid4().hex}"


def test_the_answer_is_kept_for_30_seconds_per_queue() -> None:
    assert POLLERS_CACHE_SECONDS == 30.0
    clock = Clock()
    cache = PollerCache(clock=clock)
    client = FakeClient(pollers=False)
    one, other = _queue(), _queue()

    def has(queue: str) -> bool:
        probe = TemporalQueueProbe(client, queue, cache)  # type: ignore[arg-type]
        return asyncio.run(probe.has_pollers())

    assert has(one) is False
    # No poller on either type: both were read.
    assert [n for n, _ in client.workflow_service.reads] == [one, one]
    client.workflow_service.pollers = True
    clock.now += POLLERS_CACHE_SECONDS - 1
    assert has(one) is False
    assert len(client.workflow_service.reads) == 2
    # Another queue is read on its own, both types at once (#7425).
    assert has(other) is True
    assert client.workflow_service.reads[2:] == [
        (other, TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
        (other, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY),
    ]
    clock.now += 2
    assert has(one) is True
    assert len(client.workflow_service.reads) == 6


def test_the_apps_probes_share_one_cache() -> None:
    """`profile_fetch.probe` makes a new probe on each read; they share the module's
    cache, so a second read within 30 s asks Temporal nothing."""
    client = FakeClient(pollers=True)
    queue = _queue()
    for _ in range(3):
        probe = TemporalQueueProbe(client, queue)  # type: ignore[arg-type]
        assert asyncio.run(probe.has_pollers()) is True
    assert len(client.workflow_service.reads) == 2


def test_a_failed_read_is_not_kept() -> None:
    client = FakeClient(pollers=True)
    client.workflow_service.fail = True
    queue = _queue()
    probe = TemporalQueueProbe(client, queue, PollerCache())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        asyncio.run(probe.has_pollers())
    client.workflow_service.fail = False
    assert asyncio.run(probe.has_pollers()) is True
    assert len(client.workflow_service.reads) == 4


def test_pollers_on_the_activity_type_alone_are_the_answer() -> None:
    """#7578: both reads answer, the workflow type with none: the activity type's
    pollers still mean a worker will take the run."""
    client = FakeClient(pollers=True)
    client.workflow_service.empty_type = TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW
    probe = TemporalQueueProbe(client, _queue(), PollerCache())  # type: ignore[arg-type]
    assert asyncio.run(probe.has_pollers()) is True


def test_a_failed_activity_read_does_not_hide_the_workflow_pollers() -> None:
    """#7513: both types are read at once (#7425); a poller on one is the answer even
    when the other type's read failed."""
    client = FakeClient(pollers=True)
    client.workflow_service.fail_type = TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY
    probe = TemporalQueueProbe(client, _queue(), PollerCache())  # type: ignore[arg-type]
    assert asyncio.run(probe.has_pollers()) is True


def test_a_failed_read_beside_an_empty_one_raises_and_is_not_kept() -> None:
    """With no poller on the type that answered, the failed type may have one: the
    probe cannot say no, so it raises, and keeps nothing (#7513)."""
    client = FakeClient(pollers=False)
    client.workflow_service.fail_type = TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW
    probe = TemporalQueueProbe(client, _queue(), PollerCache())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        asyncio.run(probe.has_pollers())
    client.workflow_service.fail_type = None
    client.workflow_service.pollers = True
    assert asyncio.run(probe.has_pollers()) is True
    assert len(client.workflow_service.reads) == 4


def test_cancel_asks_temporal_to_cancel_the_runs_workflow() -> None:
    client = FakeClient()
    run_id = str(uuid.uuid4())
    probe = TemporalQueueProbe(client, _queue())  # type: ignore[arg-type]
    asyncio.run(probe.cancel(run_id))
    assert client.cancelled == [f"profile-fetch:{run_id}"]
