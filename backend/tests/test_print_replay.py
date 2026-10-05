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
runs). Recorded by the patched one: ``cancelled_once_queued`` (the cancel during
``print_finish`` waits for the run, which succeeds and closes),
``cancelled_during_last_enqueue`` (the same during the last ``print_enqueue``) and
``cancelled_while_queueing`` (a cancel while the first of two plates is recorded: may be
queued, and no window).

``PLATES_PATCH`` (review #1316 (3)) is the second. Recorded by #1061's workflow, which
cancels the activity: ``cancelled_during_first_enqueue_1061`` (a cancel while the first
of two plates' ``print_enqueue`` runs) and ``cancelled_during_start_enqueue_1061`` (a
cancel while ``print_start_enqueue`` runs). Recorded by the patched one, which waits:
``cancelled_during_first_enqueue`` (plate 1 is recorded, plate 2 never sliced) and
``cancelled_during_start_enqueue`` (no ``POST /queue/``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from scadbuddy.workflows.printing import PrintRunWorkflow

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
