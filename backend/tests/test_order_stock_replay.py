"""The orders' stock workflows replay the histories they wrote (#6949 K1 Task 16).

On the versioned ``library`` worker (#4090) `FulfilOrderItem`, `RecostLot` and
`MergePrinter` run AUTO_UPGRADE (their ``Versioned*`` classes): an open run moves to the
new build at its next workflow task and replays there. So a change to the commands they
issue goes behind ``workflow.patched`` and adds a history recorded on the changed code
beside these.

`tests/workflows/test_order_stock.py` records them on a real Temporal
(``SCADBUDDY_RECORD_HISTORIES=1``): a fulfil, a refused one, a slow take, a record retry,
an unfulfil that waits for the fulfil, a re-cost retry, a merge, a refused merge, a
takeover of a terminated fulfil and a failed take. Each file is named after its scene.
CI records them: dispatch `ci.yml` with the input ``record_histories`` and commit the
files of the ``order-stock-histories-<shard>`` artifacts here. Never re-record one."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from scadbuddy.workflows.order_stock import FulfilOrderItem, MergePrinter, RecostLot

HISTORIES = Path(__file__).parent / "fixtures" / "order_stock_histories"
#: The scenes `tests/workflows/test_order_stock.py` records.
RECORDED = (
    "fulfil",
    "refused",
    "slow_take",
    "record_retry",
    "unfulfil_waits",
    "recost_retry",
    "merge",
    "merge_refused",
    "takeover",
    "take_failed",
)


def _workflow_id(history: WorkflowHistory) -> str | None:
    """The id the run had, where its commands name it: `RecostLot` keys each Go step
    ``<workflow id>:<round>``, so a replay under another id schedules other activity ids.
    The history keeps no id of its own; the first ``inventory.Recost`` step's gives it."""
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            scheduled = event.activity_task_scheduled_event_attributes
            if scheduled.activity_type.name == "inventory.Recost":
                return scheduled.activity_id.rsplit(":", 1)[0]
    return None


@pytest.mark.parametrize("name", sorted(p.stem for p in HISTORIES.glob("*.json")))
async def test_order_stock_replays(name: str) -> None:
    text = (HISTORIES / f"{name}.json").read_text()
    history = WorkflowHistory.from_json(
        _workflow_id(WorkflowHistory.from_json("probe", text)) or f"order-stock-{name}", text
    )
    await Replayer(
        workflows=[FulfilOrderItem, RecostLot, MergePrinter],
        data_converter=pydantic_data_converter,
    ).replay_workflow(history)


def test_every_history_is_a_recorded_scene() -> None:
    names = {p.stem for p in HISTORIES.glob("*.json")}
    assert names <= set(RECORDED)
