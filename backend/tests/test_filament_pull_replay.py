"""``FilamentBbPull`` (#6772): it runs its one activity and answers its outcome, a failed
activity is ``failed``, and it replays the histories it wrote.

On the versioned print worker it is AUTO_UPGRADE (`client.print_deployment`), so a run
left open across a deploy replays on the new code. A change to the commands
``FilamentBbPull.run`` issues goes behind ``workflow.patched``; this test is what says so.

``v1`` was recorded (``SCADBUDDY_RECORD_HISTORIES=1`` runs the recording below) from a
Schedule's pull whose activity answered ``pulled``. Never re-record it: a later change adds
a history recorded on the changed code beside it, and records only that one
(``-k <name>``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, Worker

from scadbuddy.filament.bambuddy_pull import ACTOR, PullReport, Pulls
from scadbuddy.filament.bb_pull_apply import execute_pull
from scadbuddy.filament.bb_pull_models import PULL_READ_ACTIVITY, PullOutcome, PullStart
from scadbuddy.workflows.client import sandboxed_runner
from scadbuddy.workflows.filament_pull import FilamentBbPull
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "filament_pull_histories"
START = PullStart(instance="0123456789ab", actor=ACTOR, pulls=Pulls(), reconcile=True)


@pytest.mark.parametrize("name", ["v1"])
async def test_the_history_replays(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"filament-pull-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    await Replayer(
        workflows=[FilamentBbPull], data_converter=pydantic_data_converter
    ).replay_workflow(history)


def test_the_v1_history_holds_one_read() -> None:
    history = WorkflowHistory.from_json("filament-pull-v1", (HISTORIES / "v1.json").read_text())
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled == [PULL_READ_ACTIVITY]


@pytest.fixture(scope="module")
def temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


def _read(answer: Callable[[PullStart], PullOutcome]) -> Any:
    @activity.defn(name=PULL_READ_ACTIVITY)
    async def pull_read(start: PullStart) -> PullOutcome:
        return answer(start)

    return pull_read


async def _run(address: str, read: Any) -> tuple[PullOutcome, WorkflowHistory]:
    """`execute_pull`, the API's start, against a worker of ``read`` on a queue of its own."""
    client = await Client.connect(address, data_converter=pydantic_data_converter)
    queue = f"filament-pull-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[FilamentBbPull],
        activities=[read],
        workflow_runner=sandboxed_runner(),
    ):
        outcome = await execute_pull(client, queue, START)
        listed = [w async for w in client.list_workflows(f"TaskQueue = '{queue}'")]
        history = await client.get_workflow_handle(listed[0].id).fetch_history()
    return outcome, history


@pytest.mark.requires_temporal
async def test_the_run_answers_its_activitys_outcome(temporal: str) -> None:
    starts: list[PullStart] = []

    def pulled(start: PullStart) -> PullOutcome:
        starts.append(start)
        return PullOutcome(outcome="pulled", report=PullReport(created=2))

    outcome, _ = await _run(temporal, _read(pulled))
    assert outcome == PullOutcome(outcome="pulled", report=PullReport(created=2))
    assert starts == [START]


@pytest.mark.requires_temporal
async def test_a_failed_read_is_failed_and_not_retried(temporal: str) -> None:
    calls: list[PullStart] = []

    def broken(start: PullStart) -> PullOutcome:
        calls.append(start)
        raise RuntimeError("the CRUD service is out of reach")

    outcome, _ = await _run(temporal, _read(broken))
    assert outcome == PullOutcome(outcome="failed")
    # Once: the Schedule's next tick is the retry, as for the API's own activity before.
    assert len(calls) == 1


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
# Only the newest history is recorded: the older ones were recorded by the code of their
# time and are never re-recorded.
@pytest.mark.parametrize("name", ["v1"])
async def test_record(temporal: str, name: str) -> None:
    _, history = await _run(
        temporal, _read(lambda start: PullOutcome(outcome="pulled", report=PullReport()))
    )
    HISTORIES.mkdir(exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())
