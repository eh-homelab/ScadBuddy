"""``PrintRun`` (#1052, spec 2026-10-01 §5.1-§5.3) against fake activities registered
under the real names, on a dev server: the shape every command copies."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

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
)
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
from scadbuddy.bambuddy.runs import UNEXPECTED_DETAIL, PrintRun, PrintRunError
from scadbuddy.library.outputs import PlateSend
from scadbuddy.workflows.commands import start_command
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
)
from scadbuddy.workflows.printing import PrintRunWorkflow
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
    async def enqueue(self, input: EnqueueInput) -> int:
        self.calls.append(f"enqueue:{input.plate_id}")
        if self.enqueue_error is not None:
            raise self.enqueue_error
        return 50 + input.plate_id

    @activity.defn(name="print_record")
    async def record(self, input: RecordInput) -> list[PlateSend]:
        self.calls.append("record")
        if self.record_error is not None:
            raise self.record_error
        return input.sent

    @activity.defn(name="print_finish")
    async def finish(self, input: FinishInput) -> PrintRun:
        self.calls.append("finish")
        items = [item for outcome in input.outcomes for item in outcome.queue_item_ids]
        result = PrintRunResult(
            library_file_id=41, copies=1, queue_item_ids=items, bambuddy_url="http://b/queue"
        )
        return self._run("succeeded", result=result)

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
