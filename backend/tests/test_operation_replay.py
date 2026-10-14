"""``Operation`` replays the histories it wrote (#2544).

Every workflow on ``scadbuddy-print`` is AUTO_UPGRADE (``workflows/client.py``
``bambuddy_worker``): a run moves to the new build on its next workflow task, so a
change to ``OperationWorkflow``'s commands that would not replay these histories goes
behind ``workflow.patched``, and adds a history recorded on the changed code beside
them. ``tests/test_print_replay.py`` does the same for ``PrintRun``.

Recorded by the workflow as it was at #2544 (``SCADBUDDY_RECORD_HISTORIES=1`` runs the
recording below) against ``test_operation_workflow.py``'s fakes, under their real
activity names: ``succeeded`` (a done kind), ``answered_running`` (a kind that answers
once recorded, #2462), ``refused`` (its check refuses: nothing recorded) and
``failed`` (its effect fails: recorded failed). Never re-record them.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from scadbuddy.workflows.operation import OperationWorkflow
from scadbuddy.workflows.print_models import FAILED
from tests.support.temporal import temporal_client
from tests.test_operation_workflow import BAMBUDDY_502, Fake, ended, op_input, start

HISTORIES = Path(__file__).parent / "fixtures" / "operation_histories"
NAMES = ["succeeded", "answered_running", "refused", "failed"]


@pytest.mark.parametrize("name", NAMES)
async def test_operation_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(f"op-{name}", (HISTORIES / f"{name}.json").read_text())
    replayer = Replayer(workflows=[OperationWorkflow], data_converter=pydantic_data_converter)
    await replayer.replay_workflow(history)


async def _record(client: Client, name: str, workflow_id: str) -> None:
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    HISTORIES.mkdir(parents=True, exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records histories")
async def test_record_operation_histories() -> None:
    async with temporal_client() as client:
        for name in NAMES:
            fake = Fake()
            fake.refuse = name == "refused"
            if name == "failed":
                fake.run_error = ApplicationError(
                    BAMBUDDY_502.detail, BAMBUDDY_502, type=FAILED, non_retryable=True
                )
            queue = f"op-{uuid.uuid4().hex[:8]}"
            arg = op_input(answer_running=name == "answered_running")
            async with Worker(
                client, task_queue=queue, workflows=[OperationWorkflow], activities=fake.all()
            ):
                await start(client, queue, arg)
                if name == "refused":
                    with pytest.raises(Exception):  # noqa: B017 - it ends failed, as refused
                        await ended(client, arg)
                else:
                    await ended(client, arg)
            await _record(client, name, f"op-{arg.kind}-{arg.key}")
