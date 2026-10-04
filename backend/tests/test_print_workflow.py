"""``PrintRun`` (#1052, spec 2026-10-01 §5.1-§5.3) against fake activities registered
under the real names, on a dev server: the shape every command copies."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import (
    Interceptor,
    SignalExternalWorkflowInput,
    UnsandboxedWorkflowRunner,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from scadbuddy.bambuddy.dispatch import QueueOutcome, SlicePlan, SliceStarted
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.follow import FOLLOW_ACTIVITY, FollowInput
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import (
    PlannedRun,
    PlatePlan,
    PreparedPlates,
    PrintRunRequest,
    PrintRunResult,
    QueuedPlate,
)
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
from scadbuddy.bambuddy.runs import UNEXPECTED_DETAIL, PrintRun, PrintRunError
from scadbuddy.library.outputs import PlateSend
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.workflows.commands import start_command
from scadbuddy.workflows.follow import FollowPrint, follow_id
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.print_models import (
    FAILED,
    REFUSED,
    AcceptAnswer,
    Checked,
    EnqueueInput,
    FailInput,
    FinishInput,
    InsertInput,
    PlanInput,
    PrintRunInput,
    RecordInput,
    SliceStartInput,
    SourceSpec,
    SucceedInput,
)
from scadbuddy.workflows.printing import (
    ACCEPT_TIMEOUT,
    CANCELLED,
    CLIENT_ACCEPTING,
    READ_RETRY,
    PrintRunWorkflow,
)
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_temporal

PLAN = SlicePlan(
    printer_preset=PresetRef(source="cloud", id="GM042"),
    process_preset=PresetRef(source="cloud", id="GP243"),
    filament_presets=[PresetRef(source="cloud", id="GFG99")],
    filament_colours=["#688197"],
    bed_type="Supertack Plate",
)
REQUEST = PrintRunRequest(
    filament_plan=FilamentPlan(slots=[]), choices=PrintChoices(nozzles=[NozzleChoice(size="0.4")])
)
REFUSAL = PrintRunError(status=422, title="Unprocessable Content", detail="no plates")


class Fake:
    """The activities, recording calls; each behaviour is a switch a test sets."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.refuse = False
        self.plan_error: ApplicationError | None = None
        self.enqueue_error: ApplicationError | None = None
        self.enqueue_attempted = False
        self.plates = [1]
        #: How many of the next `print_insert` attempts fail before one succeeds.
        self.insert_failures = 0
        self.record_error: Exception | None = None
        self.follows: list[FollowInput] = []
        self.output_id = uuid.uuid4().hex
        #: How many of the next `print_succeed` attempts fail before one succeeds.
        self.succeed_failures = 0
        #: Set, the check waits on it, so a test can act while it is in flight.
        self.check_gate: asyncio.Event | None = None
        self.project_id: int | None = None

    def _run(self, status: str = "running", **fields: object) -> PrintRun:
        return PrintRun(
            id="run-1",
            output_id="o" * 32,
            status=status,  # type: ignore[arg-type]
            created_at=datetime(2026, 10, 2, tzinfo=UTC),
            **fields,  # type: ignore[arg-type]
        )

    @activity.defn(name="print_check")
    async def check(self, input: PrintRunInput) -> Checked:
        self.calls.append("check")
        if self.check_gate is not None:
            await self.check_gate.wait()
        if self.refuse:
            raise ApplicationError(REFUSAL.detail, REFUSAL, type=REFUSED, non_retryable=True)
        return Checked(
            source=input.source, prepared=PreparedPlates(plate_ids=self.plates, printer_id=1)
        )

    @activity.defn(name="print_insert")
    async def insert(self, input: InsertInput) -> PrintRun:
        self.calls.append("insert")
        if self.insert_failures:
            self.insert_failures -= 1
            raise RuntimeError("the database blinked")
        return self._run()

    @activity.defn(name="print_plan")
    async def plan(self, input: PlanInput) -> PlannedRun:
        self.calls.append("plan")
        if self.plan_error is not None:
            raise self.plan_error
        return PlannedRun(
            library_file_id=41,
            project_id=self.project_id,
            printer_id=1,
            nozzle_size="0.4",
            copies=1,
            options=PrintOptions(),
            plates=[PlatePlan(plate_id=plate, plan=PLAN) for plate in self.plates],
        )

    @activity.defn(name="print_slice_start")
    async def slice_start(self, input: SliceStartInput) -> SliceStarted:
        self.calls.append(f"slice_start:{input.plate_id}")
        return SliceStarted(job_id=9, preset_key="k")

    @activity.defn(name="print_slice_wait")
    async def slice_wait(self, job_id: int) -> int:
        self.calls.append("slice_wait")
        return 52

    @activity.defn(name="print_start_enqueue")
    async def start_enqueue(self, run_id: str) -> None:
        self.calls.append("start_enqueue")
        self.enqueue_attempted = True

    @activity.defn(name="print_enqueue")
    async def enqueue(self, input: EnqueueInput) -> QueuedPlate:
        self.calls.append(f"enqueue:{input.plate_id}")
        if self.enqueue_error is not None:
            raise self.enqueue_error
        return QueuedPlate(item_id=50 + input.plate_id)

    @activity.defn(name="print_record")
    async def record(self, input: RecordInput) -> list[PlateSend]:
        self.calls.append("record")
        if self.record_error is not None:
            raise self.record_error
        return input.sent

    @activity.defn(name="print_finish")
    async def finish(self, input: FinishInput) -> PrintRunResult:
        self.calls.append("finish")
        items = [item for outcome in input.outcomes for item in outcome.queue_item_ids]
        return PrintRunResult(
            library_file_id=41, copies=1, queue_item_ids=items, bambuddy_url="http://b/queue"
        )

    @activity.defn(name="print_succeed")
    async def succeed(self, input: SucceedInput) -> PrintRun:
        self.calls.append("succeed")
        if self.succeed_failures:
            self.succeed_failures -= 1
            raise RuntimeError("the database blinked")
        return self._run("succeeded", result=input.result)

    @activity.defn(name=FOLLOW_ACTIVITY)
    async def follow_print(self, input: FollowInput) -> str:
        self.follows.append(input)
        return "settled"

    @activity.defn(name="print_fail")
    async def fail(self, input: FailInput) -> PrintRun:
        self.calls.append(f"fail:{input.error.status}:{input.error.detail}")
        return self._run("failed", error=input.error, may_have_queued=self.enqueue_attempted)

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.check,
            self.insert,
            self.plan,
            self.slice_start,
            self.slice_wait,
            self.start_enqueue,
            self.enqueue,
            self.record,
            self.finish,
            self.succeed,
            self.fail,
        ]


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
async def worker(client: Client, fake: Fake) -> AsyncIterator[str]:
    queue = f"print-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PrintRunWorkflow, FollowPrint],
        activities=[*fake.all(), fake.follow_print],
    ):
        yield queue


def run_input(*, window: float = 0.0, output_id: str | None = None) -> PrintRunInput:
    return PrintRunInput(
        subject="o" * 32,
        slug="demo",
        key=uuid.uuid4().hex,
        source=SourceSpec(kind="output", output_id=output_id or "o" * 32),
        request=REQUEST,
        repeat_window_s=window,
    )


async def start(client: Client, queue: str, arg: PrintRunInput) -> AcceptAnswer:
    return await start_command(
        client,
        "PrintRun",
        arg,
        id=f"print-{arg.key}",
        task_queue=queue,
        update="accepted",
        result_type=AcceptAnswer,
        reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )


async def ended(client: Client, arg: PrintRunInput) -> PrintRun:
    handle = client.get_workflow_handle(f"print-{arg.key}", result_type=PrintRun)
    result: PrintRun = await handle.result()
    return result


async def test_a_refusal_answers_the_update_and_fails_the_execution_with_no_record(
    client: Client, worker: str, fake: Fake
) -> None:
    fake.refuse = True
    arg = run_input()
    answer = await start(client, worker, arg)
    assert answer.run is None and answer.refusal == REFUSAL
    with pytest.raises(WorkflowFailureError):
        await ended(client, arg)
    assert fake.calls == ["check"]


async def test_an_accepted_run_answers_the_row_then_succeeds(
    client: Client, worker: str, fake: Fake
) -> None:
    fake.plates = [1, 2]
    arg = run_input()
    answer = await start(client, worker, arg)
    assert answer.run is not None and answer.run.status == "running" and not answer.repeated
    run = await ended(client, arg)
    assert run.status == "succeeded" and run.result is not None
    assert run.result.queue_item_ids == [51, 52]
    assert fake.calls == [
        "check",
        "insert",
        "plan",
        "slice_start:1",
        "slice_wait",
        "start_enqueue",
        "enqueue:1",
        "record",
        "slice_start:2",
        "slice_wait",
        "enqueue:2",
        "record",
        "finish",
        "succeed",
    ]


async def test_a_second_update_is_a_repeat_with_the_same_row(
    client: Client, worker: str, fake: Fake
) -> None:
    arg = run_input(window=5)
    first = await start(client, worker, arg)
    second = await start(client, worker, arg)
    assert first.run is not None and second.run is not None
    assert second.repeated and second.run.id == first.run.id
    assert fake.calls.count("check") == 1
    await client.get_workflow_handle(f"print-{arg.key}").terminate()


async def test_an_unexpected_activity_error_after_the_record_completes_the_workflow(
    client: Client, worker: str, fake: Fake
) -> None:
    """§4.2: once the record exists, every outcome completes the execution."""
    fake.plan_error = ApplicationError("boom", type="Boom", non_retryable=True)
    arg = run_input()
    await start(client, worker, arg)
    run = await ended(client, arg)
    assert run.status == "failed"
    assert fake.calls[-1] == f"fail:500:{UNEXPECTED_DETAIL}"


async def test_a_failure_the_activity_reports_is_recorded_in_its_words(
    client: Client, worker: str, fake: Fake
) -> None:
    error = PrintRunError(status=422, title="Unprocessable Content", detail="slot 2 has no spool")
    fake.plan_error = ApplicationError(error.detail, error, type=FAILED, non_retryable=True)
    arg = run_input()
    await start(client, worker, arg)
    run = await ended(client, arg)
    assert run.error == error


async def test_an_enqueue_failure_records_may_have_queued_and_holds_the_window(
    client: Client, worker: str, fake: Fake
) -> None:
    error = PrintRunError(status=504, title="Gateway Timeout", detail="queue timed out")
    fake.enqueue_error = ApplicationError(error.detail, error, type=FAILED, non_retryable=True)
    arg = run_input(window=30)
    first = await start(client, worker, arg)
    handle = client.get_workflow_handle(f"print-{arg.key}")
    # The fake records `fail` before the workflow holds its answer: repeat until it does.
    again = await start(client, worker, arg)
    while again.run is not None and again.run.status == "running":
        await asyncio.sleep(0.05)
        again = await start(client, worker, arg)
    assert again.repeated and again.run is not None and first.run is not None
    assert again.run.id == first.run.id and again.run.may_have_queued
    assert (await handle.describe()).status is not None  # still open: the window holds
    await handle.terminate()


async def test_slice_start_and_enqueue_have_maximum_attempts_one(
    client: Client, worker: str, fake: Fake
) -> None:
    arg = run_input()
    await start(client, worker, arg)
    await ended(client, arg)
    attempts: dict[str, int] = {}
    async for event in client.get_workflow_handle(f"print-{arg.key}").fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            scheduled = event.activity_task_scheduled_event_attributes
            attempts[scheduled.activity_type.name] = scheduled.retry_policy.maximum_attempts
    assert attempts["print_slice_start"] == 1
    assert attempts["print_enqueue"] == 1
    assert attempts["print_slice_wait"] != 1


async def test_the_long_activities_heartbeat_so_a_dead_worker_is_noticed(
    client: Client, worker: str, fake: Fake
) -> None:
    """A worker that dies mid-upload or mid-slice is noticed within the heartbeat
    timeout, not at the activity's whole budget (the run then resumes elsewhere)."""
    arg = run_input()
    await start(client, worker, arg)
    await ended(client, arg)
    heartbeats: dict[str, float] = {}
    async for event in client.get_workflow_handle(f"print-{arg.key}").fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            scheduled = event.activity_task_scheduled_event_attributes
            heartbeats[scheduled.activity_type.name] = (
                scheduled.heartbeat_timeout.ToTimedelta().total_seconds()
            )
    assert heartbeats["print_plan"] == 30
    assert heartbeats["print_slice_wait"] == 30


async def test_a_failure_before_any_enqueue_closes_at_once(
    client: Client, worker: str, fake: Fake
) -> None:
    fake.plan_error = ApplicationError("boom", type="Boom", non_retryable=True)
    arg = run_input(window=600)
    await start(client, worker, arg)
    run = await asyncio.wait_for(ended(client, arg), timeout=10)
    assert run.status == "failed" and not run.may_have_queued


async def test_the_insert_is_retried_alone_so_a_refusal_never_follows_a_record(
    client: Client, worker: str, fake: Fake
) -> None:
    """A retried insert does not run Bambuddy's checks again: a blip there after the
    row committed would fail the execution and strand the row `running` (#1052 review)."""
    fake.insert_failures = 1
    arg = run_input()
    answer = await start(client, worker, arg)
    assert answer.run is not None and answer.refusal is None
    await ended(client, arg)
    assert fake.calls[:3] == ["check", "insert", "insert"]
    assert fake.calls.count("check") == 1


async def test_a_record_that_keeps_failing_ends_the_run_failed(
    client: Client, worker: str, fake: Fake
) -> None:
    """§4.2: every outcome after the record completes the execution, so an activity
    that does more than a database write is not retried forever."""
    fake.record_error = RuntimeError("the data volume is gone")
    arg = run_input()
    await start(client, worker, arg)
    run = await asyncio.wait_for(ended(client, arg), timeout=60)
    assert run.status == "failed" and run.may_have_queued
    assert fake.calls[-1] == f"fail:500:{UNEXPECTED_DETAIL}"


def test_outcomes_are_plain_models() -> None:
    """What `print_record` and `print_finish` carry is the dispatch's own outcome."""
    assert QueueOutcome(slice_job_id=1, sliced_library_file_id=2, printer_id=1).queue_item_ids == []


async def test_an_output_run_starts_its_follow(client: Client, worker: str, fake: Fake) -> None:
    arg = run_input(output_id=uuid.uuid4().hex)
    await start(client, worker, arg)
    run = await ended(client, arg)
    assert run.status == "succeeded"
    assert arg.source.output_id is not None
    follow = client.get_workflow_handle(follow_id(arg.source.output_id))
    assert await follow.result() == "settled"
    assert [f.output_id for f in fake.follows] == [arg.source.output_id]


async def test_a_second_run_pokes_the_follow_already_running(
    client: Client, worker: str, fake: Fake
) -> None:
    output = uuid.uuid4().hex
    # A follow already running for the output, on a queue nobody serves: it stays open.
    await client.start_workflow(
        FollowPrint.run, output, id=follow_id(output), task_queue=f"idle-{uuid.uuid4().hex[:8]}"
    )
    handle = client.get_workflow_handle(follow_id(output))
    try:
        arg = run_input(output_id=output)
        await start(client, worker, arg)
        assert (await ended(client, arg)).status == "succeeded"
        signals = [
            e
            async for e in handle.fetch_history_events()
            if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
        ]
        assert [e.workflow_execution_signaled_event_attributes.signal_name for e in signals] == [
            "poke"
        ]
    finally:
        await handle.terminate()


class _LoseFirstPoke(WorkflowOutboundInterceptor):
    """The follow closed between the child start and the poke: the signal fails."""

    lost = False

    async def signal_external_workflow(self, input: SignalExternalWorkflowInput) -> None:
        if not self.lost:
            self.lost = True
            raise ApplicationError("workflow execution already completed")
        await super().signal_external_workflow(input)


class _LosingPokes(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_LoseFirstPoke(outbound))


class LosePokes(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        return _LosingPokes


async def test_a_poke_that_finds_the_follow_closed_starts_it_again(
    client: Client, fake: Fake
) -> None:
    """Review I5: a new print is never left unfollowed because the follow of the last one
    closed between the child start and the poke."""
    output = uuid.uuid4().hex
    await client.start_workflow(
        FollowPrint.run, output, id=follow_id(output), task_queue=f"idle-{uuid.uuid4().hex[:8]}"
    )
    handle = client.get_workflow_handle(follow_id(output))
    queue = f"print-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[PrintRunWorkflow, FollowPrint],
            activities=[*fake.all(), fake.follow_print],
            interceptors=[LosePokes()],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            arg = run_input(output_id=output)
            await start(client, queue, arg)
            assert (await ended(client, arg)).status == "succeeded"
        signals = [
            e
            async for e in handle.fetch_history_events()
            if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
        ]
        assert len(signals) == 1
    finally:
        await handle.terminate()


async def test_a_queued_print_whose_record_blinks_still_ends_succeeded(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1061 F3: once every plate is queued, a Postgres blip delays the record;
    it never turns the run into a failure."""
    fake.succeed_failures = 2
    arg = run_input()
    await start(client, worker, arg)
    run = await asyncio.wait_for(ended(client, arg), timeout=60)
    assert run.status == "succeeded"
    assert not any(call.startswith("fail") for call in fake.calls)
    attempts: dict[str, int] = {}
    async for event in client.get_workflow_handle(f"print-{arg.key}").fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            scheduled = event.activity_task_scheduled_event_attributes
            attempts[scheduled.activity_type.name] = scheduled.retry_policy.maximum_attempts
    assert attempts["print_succeed"] == 0  # unlimited, as every record write


async def test_a_cancel_during_the_check_answers_the_update_before_the_execution_ends(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1061 1c: the Update answers a refusal, never outlived by its execution."""
    fake.check_gate = asyncio.Event()
    arg = run_input()
    accepting = asyncio.create_task(start(client, worker, arg))
    try:
        while "check" not in fake.calls:
            await asyncio.sleep(0.05)
        await client.get_workflow_handle(f"print-{arg.key}").cancel()
        answer = await accepting
        assert answer.run is None
        assert answer.refusal == CANCELLED
        with pytest.raises(WorkflowFailureError):
            await ended(client, arg)
        assert fake.calls == ["check"]
    finally:
        fake.check_gate.set()


async def test_a_cancel_during_the_repeat_window_completes_with_the_run(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1061 (third) 5: the window ends early, but the execution completes, so
    the failed-only reuse policy still keeps a re-sent request off a second print."""
    arg = run_input(window=600)
    answer = await start(client, worker, arg)
    assert answer.run is not None
    handle = client.get_workflow_handle(f"print-{arg.key}")
    # Inside the window: its timer has started.
    while not any(
        event.event_type == EventType.EVENT_TYPE_TIMER_STARTED
        for event in (await handle.fetch_history()).events
    ):
        await asyncio.sleep(0.05)
    await handle.cancel()
    finished = await ended(client, arg)
    assert finished.status == "succeeded"
    assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED


def test_the_clients_still_accepting_budget_outlasts_the_check() -> None:
    """Review #1061 (third) 6: the frontend's and the agent's 240 s, held to the check."""
    attempts = READ_RETRY.maximum_attempts
    backoff = sum(
        READ_RETRY.initial_interval.total_seconds() * READ_RETRY.backoff_coefficient**n
        for n in range(attempts - 1)
    )
    worst = attempts * ACCEPT_TIMEOUT.total_seconds() + backoff
    assert CLIENT_ACCEPTING.total_seconds() == 240
    assert CLIENT_ACCEPTING.total_seconds() > worst


class _ForgetfulSource:
    async def remember_project(self, project_id: int, *, printer_id: int, nozzle_size: str) -> None:
        raise RuntimeError("the data volume is gone")


class _Settings:
    def load(self) -> StoredSettings:
        return StoredSettings(bambuddy_url="http://bambuddy.test", bambuddy_api_key="bb_test")


async def test_a_queued_print_whose_project_is_not_remembered_still_ends_succeeded(
    client: Client, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1061 1a: the real `print_finish`; remembering the project's printer is
    best effort, so a run with every plate queued never ends `failed`."""

    async def source(*_: object) -> _ForgetfulSource:
        return _ForgetfulSource()

    unused: Any = None
    real = PrintActivities(
        PrintDeps(
            settings_store=cast(Any, _Settings()),
            outputs=unused,
            uploads=unused,
            catalogue=unused,
            store=unused,
            observer=unused,
        )
    )
    monkeypatch.setattr(real, "_source", source)
    fake.project_id = 7
    activities = [real.finish if a == fake.finish else a for a in fake.all()]
    queue = f"print-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client, task_queue=queue, workflows=[PrintRunWorkflow], activities=activities
    ):
        arg = run_input()
        await start(client, queue, arg)
        run = await asyncio.wait_for(ended(client, arg), timeout=60)
    assert run.status == "succeeded" and run.result is not None
    assert run.result.queue_item_ids == [51]
    assert not any(call.startswith("fail") for call in fake.calls)


async def test_a_check_no_client_waits_for_any_more_refuses_the_print(
    client: Client, fake: Fake
) -> None:
    """Review #1061 1b: a run whose worker picked it up after every client stopped
    re-sending it (`CLIENT_ACCEPTING`) is refused, never printed with nobody watching."""
    unused: Any = None
    real = PrintActivities(
        PrintDeps(
            settings_store=unused,
            outputs=unused,
            uploads=unused,
            catalogue=unused,
            store=unused,
            observer=unused,
        )
    )
    activities = [real.check if a == fake.check else a for a in fake.all()]
    queue = f"print-{uuid.uuid4().hex[:8]}"
    arg = run_input().model_copy(
        update={"accepted_at": datetime.now(UTC) - CLIENT_ACCEPTING - timedelta(seconds=1)}
    )
    async with Worker(
        client, task_queue=queue, workflows=[PrintRunWorkflow], activities=activities
    ):
        answer = await start(client, queue, arg)
        with pytest.raises(WorkflowFailureError):
            await ended(client, arg)
    assert answer.run is None and answer.refusal is not None
    assert answer.refusal.status == 409
    assert "Nothing was queued" in answer.refusal.detail
    assert fake.calls == []
