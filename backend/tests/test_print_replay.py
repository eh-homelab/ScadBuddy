"""``PrintRun`` replays the histories it wrote (review #1061 1b).

The ``bambuddy`` worker is unversioned and runs in every API replica, so during a
rolling update an old pod and a new one share running executions. A change to
``PrintRunWorkflow`` that would not replay these histories needs ``workflow.patched``;
this test is what says so.

These histories are the phase-1 baseline (#1052), recorded before any ``PrintRun`` ran in
the wild: ``succeeded``, ``refused``, ``enqueue_failed``, ``cancelled_during_print`` (a
cancel while ``print_plan`` runs: the run is recorded cancelled) and
``cancelled_during_insert`` (a cancel while ``print_insert`` runs: the row is recorded
cancelled). They were re-recorded once, when the pre-merge patches came out (#1236), and
``cancelled_during_print`` once more, when a cancel stopped being recorded as an
unexpected failure (review #1061 (3) 1). Never re-record them again: a later change to
the workflow's commands, including a new activity, goes behind ``workflow.patched`` and
adds a history recorded on the changed code beside them, so both replay.

``CANCEL_PATCH`` (review #1316 1, 2) is the first: ``cancelled_during_print_1061`` and
``cancelled_during_finish_1061`` were written by the workflow as #1061 merged it (a
cancel while ``print_finish`` runs cancels it, records the run failed and holds the
repeat window); ``cancelled_once_queued`` (the same cancel waits for the run, which
succeeds and closes) and ``cancelled_while_queueing`` (a cancel while the first of two
plates is recorded: may be queued, and no window) by the patched one.
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
        "cancelled_during_print_1061",
        "cancelled_during_finish_1061",
        "cancelled_once_queued",
        "cancelled_while_queueing",
    ],
)
async def test_print_run_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(f"print-{name}", (HISTORIES / f"{name}.json").read_text())
    replayer = Replayer(workflows=[PrintRunWorkflow], data_converter=pydantic_data_converter)
    await replayer.replay_workflow(history)
