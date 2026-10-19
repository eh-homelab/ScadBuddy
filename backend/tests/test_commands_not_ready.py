"""`start_command` on an execution whose workflow task is failing (#4156): still
accepting only while an ``Operation`` defers a kind its build does not register (#4139),
whose task goes on once a build with the kind is current. A task failing for any other
reason waits for nothing, and is refused as before #4139 rather than followed forever."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.workflows.commands import (
    CommandStillAcceptingError,
    TemporalRefusedError,
    start_command,
)
from scadbuddy.workflows.operation import UnregisteredKindError
from tests.support.temporal import temporal_available, temporal_server


@workflow.defn(name="NotReadyDeferred")
class Deferred:
    @workflow.run
    async def run(self) -> None:
        raise UnregisteredKindError("this build does not register 'op.x.run'")

    @workflow.update
    async def accepted(self) -> str:
        return "accepted"


@workflow.defn(name="NotReadyBroken")
class Broken:
    @workflow.run
    async def run(self) -> None:
        raise RuntimeError("a bug in workflow code")

    @workflow.update
    async def accepted(self) -> str:
        return "accepted"


@pytest.fixture(scope="module")
def temporal_address() -> Iterator[str]:
    if not temporal_available():
        pytest.skip("no Temporal")
    with temporal_server() as address:
        yield address


async def _failing(client: Client, workflow_name: str, queue: str) -> str:
    """An execution whose workflow task has failed and is being retried."""
    id = f"not-ready-{uuid.uuid4().hex[:8]}"
    handle = await client.start_workflow(workflow_name, id=id, task_queue=queue)
    async with asyncio.timeout(30):
        while (await handle.describe()).raw_description.pending_workflow_task.attempt < 2:
            await asyncio.sleep(0.1)
    return id


@pytest.mark.requires_temporal
@pytest.mark.parametrize(
    ("workflow_name", "answer"),
    [("NotReadyDeferred", CommandStillAcceptingError), ("NotReadyBroken", TemporalRefusedError)],
)
async def test_only_a_deferred_kind_is_still_accepting(
    temporal_address: str, workflow_name: str, answer: type[Exception]
) -> None:
    client = await Client.connect(temporal_address, data_converter=pydantic_data_converter)
    queue = f"not-ready-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[Deferred, Broken],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        id = await _failing(client, workflow_name, queue)
        with pytest.raises(answer):
            await start_command(
                client,
                workflow_name,
                None,
                id=id,
                task_queue=queue,
                update="accepted",
                result_type=str,
                reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        await client.get_workflow_handle(id).terminate()
