"""An ``Operation`` of a kind its build does not register waits for a build that does
(#4139), so the print worker can roll beside the API's rolling update: the API's new
pods start a new kind's operations while the old print build is still current."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from scadbuddy.operations.store import Operation
from scadbuddy.workflows import client as temporal
from scadbuddy.workflows.commands import (
    AlreadyClosedError,
    CommandStillAcceptingError,
    start_command,
)
from scadbuddy.workflows.operation_models import OperationAnswer, OperationInput
from scadbuddy.workflows.registered import RegisteredActivities
from tests.support.deployment import wait_until_build_serves
from tests.support.temporal import temporal_client, terminate_open_workflows
from tests.test_library_versioning import _make
from tests.test_operation_workflow import Fake, op_input

#: How long the old build is left current with the operation waiting on it: long
#: enough for its workflow task to be retried there more than once.
OLD_BUILD_HOLD = 25.0


def test_the_interceptor_names_the_workers_activities() -> None:
    fake = Fake()
    assert RegisteredActivities(fake.all()).names == {
        "op.test.check",
        "op_insert",
        "op.test.run",
        "op_finish",
    }


async def _send_until_answered(
    client: Client, queue: str, arg: OperationInput, still: list[float]
) -> Operation:
    """As the browser's ``command()`` and ``run_operation`` do: the same request, sent
    again after each still-accepting answer (the route's 202 or 503), whose times
    ``still`` collects; once the execution has closed, the route answers from its record
    (here, the execution's result). Anything else, a refusal included, is raised."""
    loop = asyncio.get_running_loop()
    workflow_id = f"op-{arg.kind}-{arg.key}"
    while True:
        try:
            answer = await start_command(
                client,
                "Operation",
                arg,
                id=workflow_id,
                task_queue=queue,
                update="accepted",
                result_type=OperationAnswer,
                reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except AlreadyClosedError:
            closed: Operation = await client.get_workflow_handle(
                workflow_id, result_type=Operation
            ).result()
            return closed
        except CommandStillAcceptingError:
            still.append(loop.time())
            await asyncio.sleep(1)
            continue
        assert answer.refusal is None and answer.operation is not None
        return answer.operation


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.mark.requires_temporal
async def test_a_new_kind_waits_for_the_build_that_has_it(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old print build is current and lacks the kind ``test``; the new one has it.
    The operation's workflow task fails on the old build (no activity is scheduled, so
    no attempt is spent and nothing is recorded ``failed``), and is retried until the
    new build is current, which runs the whole operation."""
    name = f"scadbuddy-print-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(temporal, "PRINT_DEPLOYMENT_NAME", name)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    old_fake, new_fake = Fake(), Fake()
    old = temporal.bambuddy_worker(
        client, queue, [old_fake.insert, old_fake.finish], build_id="old"
    )
    new = temporal.bambuddy_worker(client, queue, new_fake.all(), build_id="new")
    arg = op_input()
    workflow_id = f"op-{arg.kind}-{arg.key}"
    try:
        async with old:
            await _make(client, name, "old")
            assert await wait_until_build_serves(
                client, queue, "old", timeout=30, poll=0.05, max_poll=0.5, deployment_name=name
            )
            still: list[float] = []
            answering = asyncio.create_task(_send_until_answered(client, queue, arg, still))
            loop = asyncio.get_running_loop()
            began = loop.time()
            await asyncio.sleep(OLD_BUILD_HOLD)
            handle = client.get_workflow_handle(workflow_id)
            described = await handle.describe()
            attempt = described.raw_description.pending_workflow_task.attempt
            held = loop.time() - began
            print(f"deferred workflow task: attempt {attempt} after {held:.1f}s")
            # Retried, and backed off: not a tight loop.
            assert 2 <= attempt <= held / 5 + 2
            history = await handle.fetch_history()
            assert not [
                event
                for event in history.events
                if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            ]
            # The route answers still accepting (202, or 503 to send again), never the
            # refusal Temporal's "Workflow Task in failed state" used to be.
            assert not answering.done()
            assert still
            async with new:
                await _make(client, name, "new")
                done = await asyncio.wait_for(answering, 60)
    finally:
        await terminate_open_workflows(client, queue)
    assert done.status == "succeeded"
    assert old_fake.calls == []
    assert new_fake.calls == ["check", "insert", "run", "finish:ok"]


@activity.defn(name="op.other.check")
async def _other_check(request: dict[str, object]) -> dict[str, object]:
    return {}


@pytest.mark.requires_temporal
async def test_a_kind_the_build_registers_runs_at_once(client: Client) -> None:
    """The deferral asks only about the operation's own kind: another kind's activities
    on the worker change nothing for it."""
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    fake = Fake()
    arg = op_input()
    try:
        async with temporal.bambuddy_worker(client, queue, [*fake.all(), _other_check]):
            answer = await start_command(
                client,
                "Operation",
                arg,
                id=f"op-{arg.kind}-{arg.key}",
                task_queue=queue,
                update="accepted",
                result_type=OperationAnswer,
                reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
    finally:
        await terminate_open_workflows(client, queue)
    assert answer.operation is not None and answer.operation.status == "succeeded"
    assert fake.calls == ["check", "insert", "run", "finish:ok"]
