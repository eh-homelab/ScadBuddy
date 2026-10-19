"""An ``Operation`` of a kind its build does not register waits for a build that does
(#4139), so the print worker can roll beside the API's rolling update: the API's new
pods start a new kind's operations while the old print build is still current."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.common import RawValue, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import WorkflowInterceptorClassInput

from scadbuddy.operations.store import Operation
from scadbuddy.workflows import client as temporal
from scadbuddy.workflows.commands import (
    AlreadyClosedError,
    CommandStillAcceptingError,
    start_command,
)
from scadbuddy.workflows.operation_models import (
    REGISTERED_ACTIVITIES,
    OperationAnswer,
    OperationInput,
)
from scadbuddy.workflows.registered import RegisteredActivities
from tests.support.deployment import wait_until_build_serves
from tests.support.temporal import temporal_client, terminate_open_workflows
from tests.test_library_versioning import PROBES_IN_A_ROW, _make
from tests.test_operation_workflow import Fake, op_input

#: How long the old build is left current with the operation waiting on it: long
#: enough for its workflow task to be retried there more than once.
OLD_BUILD_HOLD = 25.0


def test_the_interceptor_names_the_workers_activities() -> None:
    fake = Fake()
    assert RegisteredActivities(fake.all()).names == frozenset(
        {"op.test.check", "op_insert", "op.test.run", "op_finish"}
    )


def test_the_interceptor_reads_temporalios_private_definition() -> None:
    """`RegisteredActivities` takes each name from ``activity._Definition``, which
    temporalio does not promise to keep (#4157): a bump that renames or reshapes it fails
    here, rather than as a worker that defers every kind."""

    @activity.defn(name="op.pinned.run")
    async def pinned() -> None: ...

    assert activity._Definition.must_from_callable(pinned).name == "op.pinned.run"


def test_a_worker_with_a_dynamic_activity_defers_nothing() -> None:
    """A dynamic activity takes every name (#4157): its worker registers every kind, so
    its workflows are told nothing, which `defer_unregistered` reads as defer nothing."""

    @activity.defn(dynamic=True)
    async def anything(args: Sequence[RawValue]) -> None: ...

    registered = RegisteredActivities([anything, *Fake().all()])
    assert registered.names is None
    given = WorkflowInterceptorClassInput(unsafe_extern_functions={})
    registered.workflow_interceptor_class(given)
    assert REGISTERED_ACTIVITIES not in given.unsafe_extern_functions


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


@activity.defn(name="op.probe.check")
async def _probe_check(request: dict[str, object]) -> dict[str, object]:
    # Refused, so a probe never reaches the fakes' insert.
    raise ApplicationError("a probe", non_retryable=True)


async def _workflow_tasks_reach(client: Client, queue: str, build_id: str) -> None:
    """Probe operations until `PROBES_IN_A_ROW` of their first workflow tasks complete on
    ``build_id`` (#4346). `wait_until_build_serves` reads the queue's root partition, and
    a workflow task goes to any partition, which can still route to the previous build
    (#4255): the task the insert's completion schedules reached the new build after the
    rollback, and nothing deferred. Both builds register the probe's kind, so whichever
    takes its first task completes it, and the completion names the build; the probe's
    check then refuses, which closes it. A heuristic, as `PROBES_IN_A_ROW` is: it
    narrows the window, it does not close it."""
    reached: list[str] = []
    async with asyncio.timeout(60):
        while reached[-PROBES_IN_A_ROW:] != [build_id] * PROBES_IN_A_ROW:
            probe = OperationInput(kind="probe", subject="probe", key=uuid.uuid4().hex, request={})
            handle = await client.start_workflow(
                "Operation", probe, id=f"op-probe-{probe.key}", task_queue=queue
            )
            completed = None
            while completed is None:
                await asyncio.sleep(0.05)
                for event in (await handle.fetch_history()).events:
                    # Both builds register the probe's kind, so its task never defers.
                    assert event.event_type != EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED, (
                        "a probe's workflow task failed"
                    )
                    if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED:
                        completed = event.workflow_task_completed_event_attributes
                        break
            reached.append(completed.deployment_version.build_id)


@pytest.mark.requires_temporal
async def test_a_rollback_after_the_insert_defers_the_run_until_the_build_has_it(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run-site deferral (`DEFER_RUN_PATCH`, #4157): the check and the insert run on
    a build with the kind, then one without it becomes current (a rollback). The kind's
    run is never scheduled there, and the operation goes on once a build with the kind
    is current again, running the effect once."""
    name = f"scadbuddy-print-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(temporal, "PRINT_DEPLOYMENT_NAME", name)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    old_fake, new_fake = Fake(), Fake()
    new_fake.insert_gate = asyncio.Event()
    new = temporal.bambuddy_worker(client, queue, [*new_fake.all(), _probe_check], build_id="new")
    old = temporal.bambuddy_worker(
        client, queue, [old_fake.insert, old_fake.finish, _probe_check], build_id="old"
    )
    arg = op_input()
    handle = client.get_workflow_handle(f"op-{arg.kind}-{arg.key}")
    try:
        # The gate is released before the new worker shuts down (exits run in reverse): a
        # failure before the rollback leaves the insert held, and the shutdown would wait
        # out its 30 s on it.
        async with new, _released(new_fake.insert_gate):
            await _make(client, name, "new")
            assert await wait_until_build_serves(
                client, queue, "new", timeout=30, poll=0.05, max_poll=0.5, deployment_name=name
            )
            answering = asyncio.create_task(_send_until_answered(client, queue, arg, []))
            async with asyncio.timeout(30):
                while new_fake.calls != ["check", "insert"]:
                    await asyncio.sleep(0.05)
            async with old:
                # The rollback, while the insert is in flight on the new build.
                await _make(client, name, "old")
                assert await wait_until_build_serves(
                    client, queue, "old", timeout=30, poll=0.05, max_poll=0.5, deployment_name=name
                )
                await _workflow_tasks_reach(client, queue, "old")
                new_fake.insert_gate.set()
                async with asyncio.timeout(60):
                    while (
                        await handle.describe()
                    ).raw_description.pending_workflow_task.attempt < 2:
                        # Named, rather than a bare timeout, if it recurs (#4346).
                        assert "run" not in new_fake.calls, (
                            "the task after the insert reached the new build after the"
                            " rollback (a partition still routing to it, #4255)"
                        )
                        await asyncio.sleep(0.2)
                history = await handle.fetch_history()
                assert [
                    event.activity_task_scheduled_event_attributes.activity_type.name
                    for event in history.events
                    if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
                ] == ["op.test.check", "op_insert"]
                # Failed by the deferral, which only a build without the kind raises: the
                # rollback path, not any retry. One event, however many attempts: Temporal
                # records only the first of a run of workflow task failures.
                assert [
                    event.workflow_task_failed_event_attributes.failure.application_failure_info.type
                    for event in history.events
                    if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED
                ] == ["UnregisteredKindError"]
                assert not answering.done()
                await _make(client, name, "new")
                done = await asyncio.wait_for(answering, 60)
    finally:
        await terminate_open_workflows(client, queue)
    assert done.status == "succeeded"
    assert old_fake.calls == []
    assert new_fake.calls == ["check", "insert", "run", "finish:ok"]


@contextlib.asynccontextmanager
async def _released(gate: asyncio.Event) -> AsyncIterator[None]:
    """Sets ``gate`` on the way out, however the block ends."""
    try:
        yield
    finally:
        gate.set()


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
