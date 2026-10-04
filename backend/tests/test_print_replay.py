"""``PrintRun`` replays the histories it wrote (review #1061 1b).

The ``bambuddy`` worker is unversioned and runs in every API replica, so during a
rolling update an old pod and a new one share running executions. A change to
``PrintRunWorkflow`` that would not replay these histories needs ``workflow.patched``;
this test is what says so.

These histories are the phase-1 baseline (#1052), recorded before any ``PrintRun`` ran in
the wild: ``succeeded``, ``refused``, ``enqueue_failed``, ``cancelled_during_print`` (a
cancel while ``print_plan`` runs: the run is recorded failed) and
``cancelled_during_insert`` (a cancel while ``print_insert`` runs: the row is recorded
cancelled), with ``succeeded_followed`` (an output's run that starts its ``FollowPrint``,
#1053). All six were re-recorded once, for #1236, when the pre-merge patches came out
(the follow's included, so every output's run follows its print). Never re-record them
again: a later change to the workflow's commands, including a new activity, goes behind
``workflow.patched`` and adds a history recorded on the changed code beside them, so
both replay.
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
        "succeeded_followed",
        "refused",
        "enqueue_failed",
        "cancelled_during_print",
        "cancelled_during_insert",
    ],
)
async def test_print_run_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(f"print-{name}", (HISTORIES / f"{name}.json").read_text())
    replayer = Replayer(workflows=[PrintRunWorkflow], data_converter=pydantic_data_converter)
    await replayer.replay_workflow(history)
