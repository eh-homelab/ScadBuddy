"""``Housekeeping`` replays the histories it wrote (#1846).

The ``library`` worker is unversioned, so a run left open across a deploy replays on
the new code (``workflows/housekeeping.py``'s docstring). A change to the commands
``Housekeeping.run`` issues (a sweep added, `SWEEPS` reordered) goes behind
``workflow.patched``; this test is what says so.

``before_reap`` was recorded by the workflow as it was before ``REAP_PATCH``
(327f66551^, six sweeps), started with no list, so it runs `SWEEPS`. ``reap`` was
recorded by the patched workflow, which also runs ``REAP_SWEEP``. Never re-record
them: a later change adds a history recorded on the changed code beside them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from scadbuddy.workflows.housekeeping import Housekeeping

HISTORIES = Path(__file__).parent / "fixtures" / "housekeeping_histories"


@pytest.mark.parametrize("name", ["before_reap", "reap"])
async def test_housekeeping_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"housekeeping-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    await Replayer(workflows=[Housekeeping]).replay_workflow(history)
