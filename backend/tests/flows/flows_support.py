"""What the flow tests share: a run's client and store, and its scripts."""

import asyncio
import hashlib
import uuid
from typing import Any

from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporalio.client import Client, WorkflowExecutionStatus

from scadbuddy.flows.models import TERMINAL, Run
from scadbuddy.flows.store import FlowStore
from scadbuddy.workflows.project import PROJECT_WORKFLOW, FlowStart, RunFlow


class Keys:
    """A subject's data key derived from its name: the codec runs, the key table is not
    needed, and a recorded history opens again in a later process (the replay test)."""

    async def key_for(self, subject: str, create: bool) -> bytes:
        return hashlib.sha256(subject.encode()).digest()


class Flows:
    def __init__(self, client: Client, queue: str, store: FlowStore) -> None:
        self.client = client
        self.queue = queue
        self.store = store
        #: Workers a test started beside the fixture's, shut down after it.
        self.cleanup: list[tuple[Any, Any]] = []

    async def start(self, script: str, *, update_id: str | None = None) -> str:
        definition = await self.store.create_definition("t", script, {"kind": "browser"})
        run_id = str(uuid.uuid4())
        await self.send(run_id, definition.id, script, update_id=update_id)
        return run_id

    async def send(
        self, run_id: str, definition_id: str, script: str, *, update_id: str | None = None
    ) -> Any:
        return await AgentClient(self.client, f"flow-{run_id}").start_and_submit_message(
            "execute",
            RunFlow(script=script).model_dump(),
            workflow_name=PROJECT_WORKFLOW,
            task_queue=self.queue,
            start_config=AgentConfig(),
            start_data=FlowStart(
                run_id=run_id,
                definition_id=definition_id,
                version=1,
                name="t",
                started_by={"kind": "browser"},
            ),
            update_id=update_id,
        )

    async def row(self, run_id: str, check: Any, timeout: float = 30) -> Run:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            run = await self.store.get_run(run_id)
            if run is not None and check(run):
                return run
            if loop.time() > deadline:
                raise AssertionError(f"timed out; last row {run!r}")
            await asyncio.sleep(0.1)

    async def finished(self, run_id: str) -> Run:
        run = await self.row(run_id, lambda r: r.status in TERMINAL)
        handle = self.client.get_workflow_handle(f"flow-{run_id}")
        for _ in range(100):
            if (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED:
                return run
            await asyncio.sleep(0.1)
        raise AssertionError("the execution did not complete (Ruling 8)")


def script(*body: str) -> str:
    lines = ["import asyncio", "async def main():", *(f"    {b}" for b in body)]
    return "\n".join([*lines, "asyncio.run(main())"])
