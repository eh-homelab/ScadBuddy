"""The sensor notifications' pass on its own Schedule (#6646, plan Decisions 40 and 41): a
minute apart on the ``bambuddy`` queue, whatever ``spool_overlay_refresh_seconds`` says,
its one activity tried once."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import Client, ScheduleActionStartWorkflow, ScheduleOverlapPolicy
from temporalio.worker import Worker

from scadbuddy.filament import sensors
from scadbuddy.filament.sensor_notify import SENSOR_NOTIFY_ACTIVITY, DeliveryReport
from scadbuddy.workflows.commands import SHORT_TASK_TIMEOUT
from scadbuddy.workflows.sensor_notify import (
    INTERVAL,
    RUN_TIMEOUT,
    SEND_TIMEOUT,
    SENSOR_NOTIFY_WORKFLOW,
    SensorNotifyPass,
    ensure_sensor_notify_schedule,
    sensor_notify_schedule_id_for,
)
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal


def test_a_live_send_is_never_ended_interrupted() -> None:
    assert SEND_TIMEOUT < RUN_TIMEOUT < sensors.INTERRUPTED_AFTER


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


def _queue() -> str:
    return f"bambuddy-{uuid.uuid4().hex[:8]}"


async def test_a_pass_runs_its_activity_once(client: Client) -> None:
    calls: list[str] = []

    @activity.defn(name=SENSOR_NOTIFY_ACTIVITY)
    async def deliver() -> DeliveryReport:
        calls.append("deliver")
        return DeliveryReport(sent=2, failed=1)

    queue = _queue()
    async with Worker(client, task_queue=queue, workflows=[SensorNotifyPass], activities=[deliver]):
        report = await client.execute_workflow(
            SensorNotifyPass.run, id=f"notify-{uuid.uuid4().hex}", task_queue=queue
        )
    assert report == DeliveryReport(sent=2, failed=1)
    assert calls == ["deliver"]


async def test_a_failed_pass_is_not_retried_within_the_run(client: Client) -> None:
    """A claimed row is never sent twice; the next tick is the retry for the rest."""
    calls: list[str] = []

    @activity.defn(name=SENSOR_NOTIFY_ACTIVITY)
    async def deliver() -> DeliveryReport:
        calls.append("deliver")
        raise RuntimeError("the API is down")

    queue = _queue()
    async with Worker(client, task_queue=queue, workflows=[SensorNotifyPass], activities=[deliver]):
        report = await client.execute_workflow(
            SensorNotifyPass.run, id=f"notify-{uuid.uuid4().hex}", task_queue=queue
        )
    assert report is None
    assert calls == ["deliver"]


async def test_the_schedule_runs_a_pass_every_minute(client: Client) -> None:
    queue = _queue()
    handle = client.get_schedule_handle(sensor_notify_schedule_id_for(queue))
    try:
        await ensure_sensor_notify_schedule(client, queue)
        described = await handle.describe()
    finally:
        await handle.delete()
        await terminate_open_workflows(client, queue)
    action = described.schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.workflow == SENSOR_NOTIFY_WORKFLOW
    assert action.task_queue == queue
    assert action.execution_timeout == RUN_TIMEOUT
    # #7288: a task the server loses is tried again after 3 s, not 10 s.
    assert action.task_timeout == SHORT_TASK_TIMEOUT
    assert described.schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    (spec,) = described.schedule.spec.intervals
    assert spec.every == INTERVAL == timedelta(minutes=1)
