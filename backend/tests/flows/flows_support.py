"""What the flow tests share: a run's client and store, and its scripts."""

import asyncio
import hashlib
import itertools
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from google.protobuf.message import Message
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError

from scadbuddy.flows.models import TERMINAL, Run, StoredDefinition
from scadbuddy.flows.store import FlowStore
from scadbuddy.workflows.flow_tools import FLOW_TOOL_TIER
from scadbuddy.workflows.project import PROJECT_WORKFLOW, FlowStart, RunFlow

#: The key a flow approval's input hash is made with in these tests (#2318).
HASH_KEY = b"\x05" * 32


class AgentTiers:
    """The agent's side of a flow's tool tiers (#2704): `flow_tool_tier` answered as the
    agent answers it (agent/src/temporal/flowTools.ts) from `tiers` and `disabled`, which
    a test may change while a run is in flight, and the API's view of them."""

    def __init__(self, tiers: Mapping[str, str], disabled: Iterable[str] = ()) -> None:
        self.tiers = dict(tiers)
        self.disabled = set(disabled)
        #: Every tool the workflow asked about, in order.
        self.asked: list[str] = []

    def activity(self) -> Any:
        @activity.defn(name=FLOW_TOOL_TIER)
        async def flow_tool_tier(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args["tool"])
            self.asked.append(name)
            if name not in self.tiers:
                raise ApplicationError(
                    f"the agent has no tool {name}", type="ToolError", non_retryable=True
                )
            return {"tier": self.tiers[name], "disabled": name in self.disabled}

        return flow_tool_tier

    async def tool_tiers(self) -> dict[str, str]:
        """As `flows.agent_tools.AgentTools.tool_tiers`: the enabled tools."""
        return {n: t for n, t in self.tiers.items() if n not in self.disabled}


class Keys:
    """A subject's data key derived from its name: the codec runs, the key table is not
    needed, and a recorded history opens again in a later process (the replay test)."""

    async def key_for(self, subject: str, create: bool) -> bytes:
        return hashlib.sha256(subject.encode()).digest()


_VERSIONS = itertools.count(1)


async def define(store: FlowStore, name: str) -> StoredDefinition:
    """A version row for a run to name: its commit is made up, since a run's tests send
    the script as the run's input and never read it from git (`test_flow_repository.py`
    and `tests/api/test_flows.py` do)."""
    return await asyncio.to_thread(
        store.insert_version,
        str(uuid.uuid4()),
        name,
        next(_VERSIONS),
        uuid.uuid4().hex + "0" * 8,
        {"kind": "browser"},
        approval_timeout_s=None,
    )


class Flows:
    def __init__(self, client: Client, queue: str, store: FlowStore) -> None:
        self.client = client
        self.queue = queue
        self.store = store
        #: Workers a test started beside the fixture's, shut down after it.
        self.cleanup: list[tuple[Any, Any]] = []

    async def start(
        self,
        script: str,
        *,
        update_id: str | None = None,
        inputs_schema: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> str:
        definition = await define(self.store, "t")
        run_id = str(uuid.uuid4())
        await self.send(
            run_id,
            definition.id,
            script,
            update_id=update_id,
            inputs_schema=inputs_schema,
            inputs=inputs,
        )
        return run_id

    async def send(
        self,
        run_id: str,
        definition_id: str,
        script: str,
        *,
        update_id: str | None = None,
        inputs_schema: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> Any:
        return await AgentClient(self.client, f"flow-{run_id}").start_and_submit_message(
            "execute",
            RunFlow(script=script, inputs_schema=inputs_schema, inputs=inputs).model_dump(),
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


async def history_payloads(client: Client, workflow_id: str) -> bytes:
    """Every payload of the workflow's history, decoded (opened, when sealed), joined: what
    a test reads to check that nothing in it holds an image (#2477)."""
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    found: list[Payload] = []

    def walk(message: Message) -> None:
        for field, value in message.ListFields():
            if field.message_type is None:
                continue
            if field.message_type.GetOptions().map_entry:
                items = list(value.values())
            elif field.is_repeated:
                items = list(value)
            else:
                items = [value]
            for item in items:
                if isinstance(item, Payload):
                    found.append(item)
                elif isinstance(item, Message):
                    walk(item)

    for event in history.events:
        walk(event)
    codec = client.data_converter.payload_codec
    decoded = await codec.decode(found) if codec is not None else found
    return b"\n".join(p.data for p in decoded)


#: The agent's tables a parent's tiers are read from (#2482), as far as the backend
#: reads them; the agent's own migrations make them in a deployment.
_AGENT_OWNERS = """
CREATE TABLE IF NOT EXISTS ai_sessions (
    id uuid PRIMARY KEY, owner_kind text NOT NULL, owner_id text NOT NULL,
    archived_at timestamptz
);
CREATE TABLE IF NOT EXISTS ai_mcp_tokens (
    id uuid PRIMARY KEY, tier text NOT NULL, expires_at timestamptz, revoked_at timestamptz
);
"""


def parent_session(
    conn: Any, session: str, owner: str, tier: str = "outward", kind: str | None = None
) -> None:
    """Session `session`, owned by `owner` (`browser`, or a `token:<id>` holding `tier`),
    as the agent records it: whose tiers a parent acts with."""
    conn.execute(_AGENT_OWNERS)
    kind = kind or ("browser" if owner == "browser" else "bearer")
    conn.execute(
        "INSERT INTO ai_sessions (id, owner_kind, owner_id) VALUES (%s, %s, %s)"
        " ON CONFLICT (id) DO UPDATE SET owner_kind = EXCLUDED.owner_kind,"
        " owner_id = EXCLUDED.owner_id",
        (session, kind, owner),
    )
    if owner.startswith("token:"):
        conn.execute(
            "INSERT INTO ai_mcp_tokens (id, tier) VALUES (%s, %s)"
            " ON CONFLICT (id) DO UPDATE SET tier = EXCLUDED.tier",
            (owner.removeprefix("token:"), tier),
        )
