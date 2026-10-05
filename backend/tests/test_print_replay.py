"""``PrintRun`` replays the histories it wrote (review #1061 1b).

The ``bambuddy`` worker is unversioned and runs in every API replica, so during a
rolling update an old pod and a new one share running executions. A change to
``PrintRunWorkflow`` that would not replay these histories needs ``workflow.patched``;
this test is what says so.

These histories are the phase-1 baseline (#1052), recorded before any ``PrintRun`` ran in
the wild: ``succeeded``, ``refused``, ``enqueue_failed``, ``cancelled_during_print`` (a
cancel while ``print_plan`` runs: the run is recorded failed) and
``cancelled_during_insert`` (a cancel while ``print_insert`` runs: the row is recorded
cancelled). They were re-recorded once, when the pre-merge patches came out (#1236).
Never re-record them again: a later change to the workflow's commands, including a new
activity, goes behind ``workflow.patched`` and adds a history recorded on the changed
code beside them, so both replay. A change to an activity's input alone is not a change
to the commands: ``cancelled_during_print`` still carries the 500 that #1061's
``print_fail`` was given, and replays.

``CANCEL_PATCH`` (review #1316) is the first patch. Recorded by the workflow as #1061
merged it: ``cancelled_during_finish_1061`` (a cancel while ``print_finish`` runs
cancels it, records the run failed and holds the repeat window) and
``cancelled_during_enqueue_1061`` (the same while the only plate's ``print_enqueue``
runs), ``cancelled_during_last_record_1061`` (the same while the only plate's
``print_record`` runs) and ``cancelled_during_succeed_1061`` (the same while
``print_succeed`` runs): one for each activity the patch shields (review #1316 3a).
Recorded by the patched one: ``cancelled_once_queued`` (the cancel during
``print_finish`` waits for the run, which succeeds and closes),
``cancelled_during_last_enqueue`` (the same during the last ``print_enqueue``) and
``cancelled_while_queueing`` (a cancel while the first of two plates is recorded: may be
queued, and no window).

``PLATES_PATCH`` (review #1316 (3)) is the second. Recorded by #1061's workflow, which
cancels the activity: ``cancelled_during_first_enqueue_1061`` (a cancel while the first
of two plates' ``print_enqueue`` runs) and ``cancelled_during_start_enqueue_1061`` (a
cancel while ``print_start_enqueue`` runs). Recorded by the patched one, which waits:
``cancelled_during_first_enqueue`` (plate 1 is recorded, plate 2 never sliced) and
``cancelled_during_start_enqueue`` (no ``POST /queue/``, so recorded as queueing nothing;
re-recorded for review #1316 (8) 2, before any deploy).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from temporalio import workflow
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import (
    Interceptor,
    Replayer,
    StartActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from scadbuddy.workflows.print_models import FailInput
from scadbuddy.workflows.printing import CANCELLED_QUEUEING, PrintRunWorkflow

HISTORIES = Path(__file__).parent / "fixtures" / "print_run_histories"


@pytest.mark.parametrize(
    "name",
    [
        "succeeded",
        "refused",
        "enqueue_failed",
        "cancelled_during_print",
        "cancelled_during_insert",
        "cancelled_during_finish_1061",
        "cancelled_during_enqueue_1061",
        "cancelled_during_last_record_1061",
        "cancelled_during_succeed_1061",
        "cancelled_once_queued",
        "cancelled_during_last_enqueue",
        "cancelled_while_queueing",
        "cancelled_during_first_enqueue_1061",
        "cancelled_during_start_enqueue_1061",
        "cancelled_during_first_enqueue",
        "cancelled_during_start_enqueue",
    ],
)
async def test_print_run_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(f"print-{name}", (HISTORIES / f"{name}.json").read_text())
    replayer = Replayer(workflows=[PrintRunWorkflow], data_converter=pydantic_data_converter)
    await replayer.replay_workflow(history)


class Scheduled(Interceptor):
    """Records each activity the workflow schedules, its input, and whether it was
    replayed: a replayed one runs with the input in the history, not this one."""

    def __init__(self) -> None:
        self.activities: list[tuple[str, bool, Any]] = []

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        activities = self.activities

        class Outbound(WorkflowOutboundInterceptor):
            def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle[Any]:
                activities.append((input.activity, workflow.unsafe.is_replaying(), input.args))
                return super().start_activity(input)

        class Inbound(WorkflowInboundInterceptor):
            def init(self, outbound: WorkflowOutboundInterceptor) -> None:
                super().init(Outbound(outbound))

        return Inbound


async def replay_until(name: str, last_event_id: int) -> Scheduled:
    """Replay ``name``'s history up to ``last_event_id``: the workflow task the cut
    leaves open runs as the new worker would run it after an upgrade."""
    recorded = json.loads((HISTORIES / f"{name}.json").read_text())
    recorded["events"] = [e for e in recorded["events"] if int(e["eventId"]) <= last_event_id]
    scheduled = Scheduled()
    replayer = Replayer(
        workflows=[PrintRunWorkflow],
        data_converter=pydantic_data_converter,
        interceptors=[scheduled],
    )
    await replayer.replay_workflow(WorkflowHistory.from_json(f"print-{name}", recorded))
    return scheduled


async def test_a_1061_cancel_left_unhandled_takes_the_patched_branch() -> None:
    """Review #1316 (9) 2b. #1061 handled a cancel during ``print_start_enqueue`` in one
    workflow task, which cancelled the activity and scheduled ``print_fail``
    (``cancelled_during_start_enqueue_1061``, events 40 to 43). Cut after the cancel
    request (event 40), that task runs on the new worker, where ``PLATES_PATCH`` is
    true: it waits for ``print_start_enqueue`` and schedules no ``print_fail`` yet."""
    scheduled = await replay_until("cancelled_during_start_enqueue_1061", 40)
    assert [name for name, _, _ in scheduled.activities][-1] == "print_start_enqueue"


async def test_a_1061_cancel_already_handled_keeps_its_recorded_print_fail() -> None:
    """Review #1316 (9) 2b. Cut after #1061 scheduled ``print_fail`` (event 43), the new
    worker replays that task with ``PLATES_PATCH`` false, so ``print_fail`` is the
    replayed command: it runs with #1061's input. The workflow's own state agrees with
    #1061, which counted the run as queueing from ``print_start_enqueue`` on: what it
    would record says it may be queued, never that nothing was."""
    scheduled = await replay_until("cancelled_during_start_enqueue_1061", 43)
    name, replaying, args = scheduled.activities[-1]
    assert name == "print_fail" and replaying
    fail: FailInput = args[0]
    assert fail.error == CANCELLED_QUEUEING and not fail.unqueued
