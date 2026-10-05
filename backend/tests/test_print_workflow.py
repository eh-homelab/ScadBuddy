"""``PrintRun`` (#1052, spec 2026-10-01 §5.1-§5.3) against fake activities registered
under the real names, on a dev server: the shape every command copies."""

from __future__ import annotations

import asyncio
import dataclasses
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
from temporalio.testing import ActivityEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.bambuddy.dispatch import QueueOutcome, SlicePlan, SliceStarted
from scadbuddy.bambuddy.filaments import FilamentPlan
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
from scadbuddy.workflows import print_activities, printing
from scadbuddy.workflows.commands import start_command
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.print_models import (
    FAILED,
    REFUSED,
    AcceptAnswer,
    Checked,
    CheckInput,
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
    CANCELLED_QUEUEING,
    CANCELLED_UNQUEUED,
    CLIENT_ACCEPTING,
    READ_RETRY,
    UNWAITED,
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
        #: How many of the next `print_succeed` attempts fail before one succeeds.
        self.succeed_failures = 0
        #: How many of the next `print_finish` attempts fail before one succeeds.
        self.finish_failures = 0
        #: Set, the check waits on it, so a test can act while it is in flight.
        self.check_gate: asyncio.Event | None = None
        #: Set, the insert waits on it, then records its row anyway (it never heartbeats).
        self.insert_gate: asyncio.Event | None = None
        self.project_id: int | None = None
        #: An activity named here waits on its event, so a test can cancel while it runs.
        self.gates: dict[str, asyncio.Event] = {}
        #: How long `print_start_enqueue` takes once its gate opens.
        self.start_enqueue_delay = 0.0
        #: The queue items `print_record` wrote, in order.
        self.recorded: list[int] = []

    async def _gate(self, name: str) -> None:
        if name in self.gates:
            await self.gates[name].wait()

    def _run(self, status: str = "running", **fields: object) -> PrintRun:
        return PrintRun(
            id="run-1",
            output_id="o" * 32,
            status=status,  # type: ignore[arg-type]
            created_at=datetime(2026, 10, 2, tzinfo=UTC),
            **fields,  # type: ignore[arg-type]
        )

    @activity.defn(name="print_check")
    async def check(self, check: CheckInput) -> Checked:
        self.calls.append("check")
        if self.check_gate is not None:
            await self.check_gate.wait()
        if self.refuse:
            raise ApplicationError(REFUSAL.detail, REFUSAL, type=REFUSED, non_retryable=True)
        return Checked(
            source=check.input.source,
            prepared=PreparedPlates(plate_ids=self.plates, printer_id=1),
        )

    @activity.defn(name="print_insert")
    async def insert(self, input: InsertInput) -> PrintRun:
        self.calls.append("insert")
        if self.insert_gate is not None:
            await self.insert_gate.wait()
        if self.insert_failures:
            self.insert_failures -= 1
            raise RuntimeError("the database blinked")
        return self._run()

    @activity.defn(name="print_plan")
    async def plan(self, input: PlanInput) -> PlannedRun:
        self.calls.append("plan")
        await self._gate("plan")
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
        await self._gate("slice_wait")
        return 52

    @activity.defn(name="print_start_enqueue")
    async def start_enqueue(self, run_id: str) -> None:
        self.calls.append("start_enqueue")
        await self._gate("start_enqueue")
        await asyncio.sleep(self.start_enqueue_delay)
        self.enqueue_attempted = True

    @activity.defn(name="print_enqueue")
    async def enqueue(self, input: EnqueueInput) -> QueuedPlate:
        self.calls.append(f"enqueue:{input.plate_id}")
        await self._gate("enqueue")
        if self.enqueue_error is not None:
            raise self.enqueue_error
        return QueuedPlate(item_id=50 + input.plate_id)

    @activity.defn(name="print_record")
    async def record(self, input: RecordInput) -> list[PlateSend]:
        self.calls.append("record")
        await self._gate("record")
        if self.record_error is not None:
            raise self.record_error
        self.recorded.extend(input.outcome.queue_item_ids)
        return input.sent

    @activity.defn(name="print_finish")
    async def finish(self, input: FinishInput) -> PrintRunResult:
        self.calls.append("finish")
        await self._gate("finish")
        if self.finish_failures:
            self.finish_failures -= 1
            raise RuntimeError("the settings could not be read")
        items = [item for outcome in input.outcomes for item in outcome.queue_item_ids]
        return PrintRunResult(
            library_file_id=41, copies=1, queue_item_ids=items, bambuddy_url="http://b/queue"
        )

    @activity.defn(name="print_succeed")
    async def succeed(self, input: SucceedInput) -> PrintRun:
        self.calls.append("succeed")
        await self._gate("succeed")
        if self.succeed_failures:
            self.succeed_failures -= 1
            raise RuntimeError("the database blinked")
        return self._run("succeeded", result=input.result)

    @activity.defn(name="print_fail")
    async def fail(self, input: FailInput) -> PrintRun:
        self.calls.append(
            f"fail:{input.error.status}:{input.error.detail}" + (":unqueued" * input.unqueued)
        )
        return self._run(
            "failed",
            error=input.error,
            may_have_queued=self.enqueue_attempted and not input.unqueued,
        )

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
        client, task_queue=queue, workflows=[PrintRunWorkflow], activities=fake.all()
    ):
        yield queue


def run_input(*, window: float = 0.0) -> PrintRunInput:
    return PrintRunInput(
        subject="o" * 32,
        slug="demo",
        key=uuid.uuid4().hex,
        source=SourceSpec(kind="output", output_id="o" * 32),
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
    assert attempts["print_finish"] == 0  # review #1061 1: every plate is queued


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


class _StalledSource:
    async def remember_project(self, project_id: int, *, printer_id: int, nozzle_size: str) -> None:
        await asyncio.Event().wait()


class _Settings:
    def __init__(self, url: str | None = "http://bambuddy.test") -> None:
        self.url = url

    def load(self) -> StoredSettings:
        return StoredSettings(bambuddy_url=self.url, bambuddy_api_key="bb_test")


def real_activities(settings: _Settings | None = None) -> PrintActivities:
    unused: Any = None
    return PrintActivities(
        PrintDeps(
            settings_store=cast(Any, settings or _Settings()),
            outputs=unused,
            uploads=unused,
            catalogue=unused,
            store=unused,
            observer=unused,
            watcher=unused,
        )
    )


async def ended_with_real_finish(
    client: Client, fake: Fake, real: PrintActivities, arg: PrintRunInput
) -> PrintRun:
    activities = [real.finish if a == fake.finish else a for a in fake.all()]
    queue = f"print-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client, task_queue=queue, workflows=[PrintRunWorkflow], activities=activities
    ):
        await start(client, queue, arg)
        return await asyncio.wait_for(ended(client, arg), timeout=30)


async def test_a_queued_print_whose_project_is_not_remembered_still_ends_succeeded(
    client: Client, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1061 1a: the real `print_finish`; remembering the project's printer is
    best effort, so a run with every plate queued never ends `failed`."""
    real = real_activities()
    monkeypatch.setattr(real, "_output_source", lambda *_: _ForgetfulSource())
    fake.project_id = 7
    run = await ended_with_real_finish(client, fake, real, run_input())
    assert run.status == "succeeded" and run.result is not None
    assert run.result.queue_item_ids == [51]
    assert not any(call.startswith("fail") for call in fake.calls)


async def test_a_project_remembered_past_its_budget_is_skipped_and_the_run_succeeds(
    client: Client, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1061 (fourth) 1: a stalled volume would outlast the activity's timeout,
    which no ``except`` sees, and `print_finish` retries without limit; the remember is
    given up after its budget instead."""
    real = real_activities()
    monkeypatch.setattr(real, "_output_source", lambda *_: _StalledSource())
    monkeypatch.setattr(print_activities, "REMEMBER_BUDGET", 0.5)
    fake.project_id = 7
    run = await ended_with_real_finish(client, fake, real, run_input())
    assert run.status == "succeeded" and run.result is not None
    assert run.result.queue_item_ids == [51]


async def test_a_library_print_reads_nothing_back_from_bambuddy_to_finish(
    client: Client, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1061 (fourth) 1: a library file's project is not remembered, so its three
    Bambuddy reads are not made."""
    real = real_activities()
    loaded: list[object] = []
    monkeypatch.setattr(real, "_source", lambda *args: loaded.append(args))
    monkeypatch.setattr(real, "_output_source", lambda *args: loaded.append(args))
    fake.project_id = 7
    arg = run_input().model_copy(update={"source": SourceSpec(kind="library", file_id=41)})
    run = await ended_with_real_finish(client, fake, real, arg)
    assert run.status == "succeeded"
    assert loaded == []


async def test_a_queued_print_whose_bambuddy_url_was_cleared_still_ends_succeeded(
    client: Client, fake: Fake
) -> None:
    """Review #1061 (fourth) 2: every plate is queued, so settings cleared meanwhile
    cost the result its queue link, never the run."""
    run = await ended_with_real_finish(client, fake, real_activities(_Settings(None)), run_input())
    assert run.status == "succeeded" and run.result is not None
    assert run.result.queue_item_ids == [51]
    assert run.result.bambuddy_url is None
    assert not any(call.startswith("fail") for call in fake.calls)


async def test_a_finish_that_fails_a_few_times_still_ends_succeeded(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1061 (fourth) 3: `print_finish` failing (a settings blip) is retried, and
    the run is recorded as it queued."""
    fake.finish_failures = 2
    arg = run_input()
    await start(client, worker, arg)
    run = await asyncio.wait_for(ended(client, arg), timeout=60)
    assert run.status == "succeeded" and not run.may_have_queued
    assert fake.calls.count("finish") == 3
    assert not any(call.startswith("fail") for call in fake.calls)


async def test_a_check_no_client_waits_for_any_more_refuses_the_print() -> None:
    """Review #1061 1b: a run whose worker picked it up after every client stopped
    re-sending it (`CLIENT_ACCEPTING`) is refused, never printed with nobody watching.
    Measured on the server's clock (review #1061 3): the execution's start, which the
    workflow passes in, against the attempt's start, never the worker's own clock."""
    unused: Any = None
    real = PrintActivities(
        PrintDeps(
            settings_store=unused,
            outputs=unused,
            uploads=unused,
            catalogue=unused,
            store=unused,
            observer=unused,
            watcher=unused,
        )
    )
    # A worker whose clock is behind: by its own, the run is a few seconds old.
    started_at = datetime.now(UTC) - timedelta(seconds=10)
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info, started_time=started_at + CLIENT_ACCEPTING + timedelta(seconds=1)
    )
    with pytest.raises(ApplicationError) as raised:
        await env.run(real.check, CheckInput(input=run_input(), started_at=started_at))
    assert raised.value.type == REFUSED
    assert raised.value.details[0] == UNWAITED


async def test_a_check_that_ends_after_every_client_stopped_waiting_records_nothing(
    client: Client, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1061 (2) 2: the check started inside `CLIENT_ACCEPTING` but ended past
    it. The workflow refuses before `print_insert`, so nothing prints unwatched. The
    budget runs from the execution's start (review #1061 3), so the route sends no
    time of its own; unsandboxed, so a test can shorten it. Both ends are the server's
    wall clock, which on a loaded host jumps seconds against this test's sleeps (+8 s
    and -0.5 s within 10 s, measured), so no hold is timed against a short budget: the
    shortest one is past at any clock reading, and the fake check, which never reads it,
    stands for one that started inside it."""
    monkeypatch.setattr(printing, "CLIENT_ACCEPTING", timedelta.min)
    arg = run_input()
    queue = f"print-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PrintRunWorkflow],
        activities=fake.all(),
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        answer = await start(client, queue, arg)
        assert answer.run is None and answer.refusal == UNWAITED
        with pytest.raises(WorkflowFailureError):
            await ended(client, arg)
        assert fake.calls == ["check"]


async def test_a_cancel_during_the_insert_answers_the_run_recorded_as_cancelled(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1061 2: the insert may commit after the cancel, so it is shielded; its
    row is recorded failed with `CANCELLED`, and the Update answers it, rather than an
    orphaned `running` row the reconciler fails minutes later."""
    fake.insert_gate = asyncio.Event()
    arg = run_input()
    accepting = asyncio.create_task(start(client, worker, arg))
    try:
        while "insert" not in fake.calls:
            await asyncio.sleep(0.05)
        handle = client.get_workflow_handle(f"print-{arg.key}")
        await handle.cancel()
        # The cancel is in the history before the insert finishes.
        while not any(
            event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED
            for event in (await handle.fetch_history()).events
        ):
            await asyncio.sleep(0.05)
        fake.insert_gate.set()
        answer = await accepting
        assert answer.run is not None and answer.run.status == "failed"
        assert answer.run.error == CANCELLED
        finished = await ended(client, arg)
        assert finished.status == "failed"
        assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED
        assert fake.calls == ["check", "insert", f"fail:409:{CANCELLED.detail}"]
    finally:
        fake.insert_gate.set()


async def cancel_during(
    client: Client, worker: str, fake: Fake, activity_name: str, *, window: float = 0.0
) -> PrintRun:
    """Start a run, cancel it while ``activity_name`` first runs, and return how it ended:
    within 60 s, so a cancel that leaves a repeat window open fails the test."""
    gate = fake.gates[activity_name] = asyncio.Event()
    arg = run_input(window=window)
    try:
        await start(client, worker, arg)
        while not any(call.split(":")[0] == activity_name for call in fake.calls):
            await asyncio.sleep(0.05)
        handle = client.get_workflow_handle(f"print-{arg.key}")
        await handle.cancel()
        while not any(
            event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED
            for event in (await handle.fetch_history()).events
        ):
            await asyncio.sleep(0.05)
        gate.set()
        finished = await asyncio.wait_for(ended(client, arg), timeout=60)
        assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED
        return finished
    finally:
        gate.set()


@pytest.mark.parametrize("activity_name", ["enqueue", "record", "finish", "succeed"])
async def test_a_cancel_once_every_plate_is_queued_still_ends_succeeded(
    client: Client, worker: str, fake: Fake, activity_name: str
) -> None:
    """Review #1061 (3) 1: from the last ``print_enqueue`` on, the print is queued, so a
    cancel must not record it failed; the last ``print_enqueue`` (whose ``POST /queue/``
    a cancel cannot take back, review #1316 (2) 1) and ``print_record``, ``print_finish``
    and ``print_succeed`` are shielded, as the insert is (review #1316 1). The cancel also
    ends the repeat window, rather than being swallowed (review #1316 2)."""
    finished = await cancel_during(client, worker, fake, activity_name, window=600)
    assert finished.status == "succeeded"
    assert not any(call.startswith("fail") for call in fake.calls)


@pytest.mark.parametrize("activity_name", ["plan", "slice_wait"])
async def test_a_cancel_before_any_enqueue_is_recorded_cancelled(
    client: Client, worker: str, fake: Fake, activity_name: str
) -> None:
    """Review #1061 (3) 1: a cancel, never "failed unexpectedly while preparing". The
    row exists and Bambuddy may have sliced a plate, so not "before it started" either
    (review #1316 (9) 2a)."""
    finished = await cancel_during(client, worker, fake, activity_name)
    assert finished.status == "failed" and not finished.may_have_queued
    assert fake.calls[-1] == f"fail:409:{CANCELLED_UNQUEUED.detail}"
    assert "nothing was queued" in CANCELLED_UNQUEUED.detail
    assert "before it started" not in CANCELLED_UNQUEUED.detail


async def test_a_cancel_after_the_enqueue_says_the_print_may_be_queued(
    client: Client, worker: str, fake: Fake
) -> None:
    """The first of two plates is queued and the second is not: the run may be queued,
    and the cancel ends its repeat window (review #1316 2)."""
    fake.plates = [1, 2]
    finished = await cancel_during(client, worker, fake, "record", window=600)
    assert "enqueue:2" not in fake.calls
    assert finished.status == "failed" and finished.may_have_queued
    assert fake.calls[-1] == f"fail:409:{CANCELLED_QUEUEING.detail}"
    assert "check Bambuddy's queue" in CANCELLED_QUEUEING.detail


async def test_a_cancel_during_an_earlier_plates_enqueue_records_its_item_and_stops(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1316 (3) 1: the ``POST /queue/`` for plate 1 may land after the cancel, so
    the run waits for it and records its item, then queues no further plate."""
    fake.plates = [1, 2]
    finished = await cancel_during(client, worker, fake, "enqueue", window=600)
    assert fake.recorded == [51]
    assert "enqueue:2" not in fake.calls and "slice_start:2" not in fake.calls
    assert finished.status == "failed" and finished.may_have_queued
    assert fake.calls[-1] == f"fail:409:{CANCELLED_QUEUEING.detail}"


async def test_a_cancel_while_the_enqueue_is_recorded_as_started_agrees_with_the_row(
    client: Client, worker: str, fake: Fake
) -> None:
    """Review #1316 (3) 2: ``print_start_enqueue`` writes the row's ``enqueue_attempted``;
    the run waits for it, then stops before any ``POST /queue/``, so it records that
    nothing was queued, and its message and the row's ``may_have_queued`` agree (review
    #1316 (8) 2)."""
    # Its write lands well after the cancel: a run that did not wait records first.
    fake.start_enqueue_delay = 2.0
    finished = await cancel_during(client, worker, fake, "start_enqueue", window=600)
    assert not any(call.startswith("enqueue") for call in fake.calls)
    assert fake.enqueue_attempted  # the write finished before the run failed
    assert finished.status == "failed" and not finished.may_have_queued
    assert fake.calls[-1] == f"fail:409:{CANCELLED_UNQUEUED.detail}:unqueued"
