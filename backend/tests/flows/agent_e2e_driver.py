"""The `projects` side of the agent's flow end-to-end test (#1057, plan
2026-10-09-durable-phase-6-flows.md §8: "`agent(...)` and `ask_session` against the fake
endpoint"), `agent/test/flowSessions.e2e.test.ts`, which starts it as a process.

A real `projects` worker (ProjectWorkflow and its host functions) on the test's Temporal,
its payloads sealed with the agent's `ai_payload_keys` under the test's KEK, over the
test's schema (migrated here). It reads one JSON object per line from stdin,
``{"script": ...}``, runs that script as a flow run started by the browser user, and
writes one JSON line per run: the run's result and steps, and the outward calls a Reset
to before its first agent-tools call would send again (`outward_since`). It writes
``{"ready": true}`` first, and exits at the end of its input.

Environment: SCADBUDDY_DATABASE_URL (with the schema's search_path),
SCADBUDDY_TEMPORAL_ADDRESS, SCADBUDDY_SECRET_KEY_FILE, SCADBUDDY_FLOW_TOOLS_QUEUE (the
queue the agent's worker serves).
"""

import asyncio
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

from scadbuddy.core.secrets import load_kek
from scadbuddy.flows.history import outward_since
from scadbuddy.flows.models import TERMINAL, Run
from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.payload_codec import PgPayloadKeys
from scadbuddy.workflows.project import PROJECT_WORKFLOW, FlowStart, RunFlow
from scadbuddy.workflows.projects_worker import projects_worker

RUN_TIMEOUT_S = 240


async def _finished(store: FlowStore, run_id: str) -> Run:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RUN_TIMEOUT_S
    while True:
        run = await store.get_run(run_id)
        if run is not None and run.status in TERMINAL:
            return run
        if loop.time() > deadline:
            raise TimeoutError(f"run {run_id} did not finish; last row {run!r}")
        await asyncio.sleep(0.2)


async def _preview(client: Client, run: Run, tools_queue: str) -> list[list[str]]:
    """The outward calls a Reset to before the run's first agent-tools call would repeat."""
    history = await client.get_workflow_handle(run.workflow_id).fetch_history()
    first = min(
        (
            e.event_id
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            and e.activity_task_scheduled_event_attributes.task_queue.name == tools_queue
        ),
        default=None,
    )
    if first is None:
        return []
    point = max(
        e.event_id
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED and e.event_id < first
    )
    preview = await outward_since(client, run, point)
    assert preview.valid, preview
    return [[c.fn, c.call_id] for c in preview.calls]


async def _run(client: Client, store: FlowStore, queue: str, tools_queue: str, script: str) -> Any:
    definition = await store.create_definition("e2e", script, {"kind": "browser"})
    run_id = str(uuid.uuid4())
    await AgentClient(client, f"flow-{run_id}").start_and_submit_message(
        "execute",
        RunFlow(script=script).model_dump(),
        workflow_name=PROJECT_WORKFLOW,
        task_queue=queue,
        start_config=AgentConfig(),
        start_data=FlowStart(
            run_id=run_id,
            definition_id=definition.id,
            version=1,
            name="e2e",
            started_by={"kind": "browser"},
            tools_queue=tools_queue,
        ),
    )
    run = await _finished(store, run_id)
    return {
        "run_id": run_id,
        "status": run.status,
        "result": run.result,
        "steps": [
            {
                "fn": s.fn,
                "call_id": s.call_id,
                "status": s.status,
                "outward": s.outward,
                "session_id": s.session_id,
            }
            for s in run.steps
        ],
        "preview": await _preview(client, run, tools_queue),
    }


async def main() -> None:
    url = os.environ["SCADBUDDY_DATABASE_URL"]
    tools_queue = os.environ["SCADBUDDY_FLOW_TOOLS_QUEUE"]
    kek = load_kek(Path(os.environ["SCADBUDDY_SECRET_KEY_FILE"]))

    @asynccontextmanager
    async def connect() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            yield conn

    jobs = JobProjection(url, pool_size=4)
    jobs.open()
    try:
        client = await connect_flows(
            os.environ["SCADBUDDY_TEMPORAL_ADDRESS"], "default", PgPayloadKeys(connect, kek)
        )
        store = FlowStore(jobs.pool)
        queue = f"projects-{uuid.uuid4().hex[:8]}"
        async with projects_worker(client, queue, FlowActivities(store).all()):
            print(json.dumps({"ready": True}), flush=True)
            loop = asyncio.get_running_loop()
            while line := await loop.run_in_executor(None, sys.stdin.readline):
                script = json.loads(line)["script"]
                answer = await _run(client, store, queue, tools_queue, script)
                print(json.dumps(answer), flush=True)
    finally:
        jobs.close()


if __name__ == "__main__":
    asyncio.run(main())
