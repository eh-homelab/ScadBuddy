"""``LanCommand`` replays the histories it wrote (#7276, plan 2026-11-25 Task 2).

On the versioned print worker it is AUTO_UPGRADE (`client.print_deployment`), so a run
left open across a deploy replays on the new code. A change to the commands
``LanCommandWorkflow.run`` issues goes behind ``workflow.patched``; this test is what says
so.

``one_command`` was recorded (``SCADBUDDY_RECORD_HISTORIES=1`` runs the recording below)
from a run of one K-profile read against a stub ``lan_command``. Never re-record it: a later change
adds a history recorded on the changed code beside it, and records only that one
(``-k <name>``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, Worker

from scadbuddy.printers.lan_command_models import LanCommandInput, LanCommandReply, LanCommandRun
from scadbuddy.printers.lan_commands import extrusion_cali_get
from scadbuddy.workflows.client import sandboxed_runner
from scadbuddy.workflows.lan_command import LanCommandWorkflow, lan_command_id
from scadbuddy.workflows.print_models import LAN_COMMAND_ACTIVITY, LAN_REPLY_WAIT_READ
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "lan_command_histories"
NAMES = sorted(path.stem for path in HISTORIES.glob("*.json"))


@pytest.mark.parametrize("name", NAMES)
async def test_the_history_replays(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"lan-command-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    await Replayer(
        workflows=[LanCommandWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(history)


def test_the_one_command_history_holds_one_lan_command() -> None:
    history = WorkflowHistory.from_json(
        "lan-command-one", (HISTORIES / "one_command.json").read_text()
    )
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled == [LAN_COMMAND_ACTIVITY]


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
# Only the newest history is recorded: the older ones were recorded by the code of their
# time and are never re-recorded.
@pytest.mark.parametrize("name", ["one_command"])
async def test_record(recording_temporal: str, name: str) -> None:
    @activity.defn(name=LAN_COMMAND_ACTIVITY)
    async def lan_command(input: LanCommandInput) -> LanCommandReply:
        return LanCommandReply(
            confirmed=True, reply={"command": input.name, "sequence_id": "1", "filaments": []}
        )

    client = await Client.connect(recording_temporal, data_converter=pydantic_data_converter)
    queue = f"lan-command-{uuid.uuid4().hex[:8]}"
    run = LanCommandRun(
        commands=[
            LanCommandInput(
                printer_id="printer-1",
                name="extrusion_cali_get",
                payload=extrusion_cali_get("0.4", "0"),
                wait_s=LAN_REPLY_WAIT_READ,
                kind="read",
            )
        ]
    )
    async with Worker(
        client,
        task_queue=queue,
        workflows=[LanCommandWorkflow],
        activities=[lan_command],
        workflow_runner=sandboxed_runner(),
    ):
        handle = await client.start_workflow(
            LanCommandWorkflow.run,
            run,
            id=lan_command_id("printer-1", "kprofile-set", queue),
            task_queue=queue,
        )
        await handle.result()
        history = await handle.fetch_history()
    HISTORIES.mkdir(exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())
