"""The DurableSession rig shared by the workflow and projector tests: ScriptedClaude, stub
tools on agent-tools, and a snapshot recorder."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.claude_agent_sdk import AgentState, ClaudeAgentPlugin, follow_agent
from temporalio.claude_agent_sdk.testing import Final, HistoryItem, ScriptedClaude, ToolCall
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.worker import Worker

from scadbuddy_durable.models import PENDING_QUERY, TASK_QUEUE, Message, SessionInput, SnapshotInput
from scadbuddy_durable.segments import SAVE_SNAPSHOT
from scadbuddy_durable.tools import TOOL_QUEUE, TOOLS
from scadbuddy_durable.workflow import DurableSession
from tests.short_runs import LongStop, ShortRuns

WAIT = 60.0


# ---- the scripted Claude --------------------------------------------------------------


@dataclass
class Seen:
    """Every (prompt, history) the policy was asked about, in order."""

    calls: list[tuple[str, list[HistoryItem]]] = field(default_factory=list)

    def last_for(self, prompt: str) -> list[HistoryItem]:
        return [h for p, h in self.calls if p == prompt][-1]

    def first_for(self, prompt: str) -> list[HistoryItem]:
        return [h for p, h in self.calls if p == prompt][0]


def make_policy(seen: Seen) -> Callable[[str, list[HistoryItem]], ToolCall | list[ToolCall] | Final]:
    """`read …` calls get_model, `print …` calls print_output (outward), `both …` calls
    get_model and update_source in one message; anything else is answered at once. A
    task's calls carry its prompt as `tag`, so the policy knows which ones are its own."""

    def policy(prompt: str, history: list[HistoryItem]) -> ToolCall | list[ToolCall] | Final:
        seen.calls.append((prompt, list(history)))
        mine = [h for h in history if h.input.get("tag") == prompt]
        tag = {"tag": prompt}
        if prompt.startswith("read") and not mine:
            return ToolCall("get_model", tag)
        if prompt.startswith("print") and not mine:
            return ToolCall("print_output", tag)
        if prompt.startswith("both") and not mine:
            return [ToolCall("get_model", tag), ToolCall("update_source", tag)]
        return Final(f"answer to {prompt}")

    return policy


# ---- stub tools and the snapshot recorder ---------------------------------------------


class Stubs:
    """Activities named like the sample tools; a gated tool waits until released."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str, dict[str, Any]]] = []  # (call id, tool, input)
        self.gates: dict[str, asyncio.Event] = {}
        self.started: dict[str, asyncio.Event] = {t.name: asyncio.Event() for t in TOOLS}

    def gate(self, name: str) -> asyncio.Event:
        self.gates[name] = asyncio.Event()
        return self.gates[name]

    def ids(self, name: str | None = None) -> list[str]:
        return [i for i, n, _ in self.runs if name is None or n == name]

    def activities(self) -> list[Callable[..., Any]]:
        def make(name: str) -> Callable[..., Any]:
            async def stub(args: dict[str, Any]) -> dict[str, Any]:
                self.runs.append((activity.info().activity_id.removeprefix("tool-"), name, args))
                self.started[name].set()
                gate = self.gates.get(name)
                if gate is not None:
                    await gate.wait()
                return {"ran": name, "tag": args.get("tag")}

            return activity.defn(name=name)(stub)

        return [make(t.name) for t in TOOLS]

    def release(self) -> None:
        for gate in self.gates.values():
            gate.set()


class Snaps:
    def __init__(self) -> None:
        self.saved: list[SnapshotInput] = []

        @activity.defn(name=SAVE_SNAPSHOT)
        async def save_snapshot(inp: SnapshotInput) -> None:
            self.saved.append(inp)

        self.activity = save_snapshot

    def latest(self, session_id: str) -> SnapshotInput:
        return [s for s in self.saved if s.session_id == session_id][-1]


@dataclass
class Rig:
    client: Client
    seen: Seen
    stubs: Stubs
    snaps: Snaps
    ids: list[str] = field(default_factory=list)

    def new(self, *, expiry: int = 3600, sid: str | None = None) -> tuple[str, SessionInput]:
        sid = sid or str(uuid.uuid4())
        self.ids.append(f"session-{sid}")
        return f"session-{sid}", SessionInput(session_id=sid, max_turns=7, approval_expiry_seconds=expiry)

    def handle(self, wid: str) -> WorkflowHandle[Any, AgentState]:
        return self.client.get_workflow_handle_for(DurableSession.run, wid)

    async def send(
        self,
        wid: str,
        inp: SessionInput,
        text: str,
        state: AgentState | None = None,
        *,
        context: str | None = None,
        workflow: Any = DurableSession,
    ) -> WorkflowHandle[Any, AgentState]:
        """Update-with-start, as the agent service sends every message (ruling 6)."""
        op: WithStartWorkflowOperation[Any, AgentState] = WithStartWorkflowOperation(
            workflow.run,
            args=[inp, state, None],
            id=wid,
            task_queue=TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
        await self.client.execute_update_with_start_workflow(
            DurableSession.send_message, Message(text, context), start_workflow_operation=op
        )
        return await op.workflow_handle()

    async def event(self, wid: str, kind: str, count: int = 1, from_offset: int = 0) -> dict[str, Any]:
        """The `count`-th live event of `kind` from `from_offset` (the run's events)."""

        async def find() -> dict[str, Any]:
            n = 0
            async for ev in follow_agent(self.client, wid, from_offset=from_offset):
                if ev["type"] == kind:
                    n += 1
                    if n == count:
                        return ev
            raise AssertionError(f"{wid} closed before {count} {kind} event(s)")

        return await asyncio.wait_for(find(), WAIT)

    async def pending(self, wid: str) -> dict[str, Any]:
        async def poll() -> dict[str, Any]:
            while True:
                items: list[dict[str, Any]] = await self.handle(wid).query(PENDING_QUERY)
                if items:
                    return items[0]
                await asyncio.sleep(0.1)

        return await asyncio.wait_for(poll(), WAIT)

    async def started(self, name: str) -> None:
        await asyncio.wait_for(self.stubs.started[name].wait(), WAIT)

    async def snapshot_where(self, sid: str, pred: Callable[[SnapshotInput], bool]) -> SnapshotInput:
        async def poll() -> SnapshotInput:
            while True:
                hits = [s for s in self.snaps.saved if s.session_id == sid and pred(s)]
                if hits:
                    return hits[-1]
                await asyncio.sleep(0.05)

        return await asyncio.wait_for(poll(), WAIT)


@asynccontextmanager
async def rig_on(client: Client, state_dir: Path) -> AsyncIterator[Rig]:
    seen, stubs, snaps = Seen(), Stubs(), Snaps()
    runner = ScriptedClaude(make_policy(seen), state_dir)
    rig = Rig(client, seen, stubs, snaps)
    try:
        async with (
            Worker(
                client,
                task_queue=TASK_QUEUE,
                workflows=[DurableSession, ShortRuns, LongStop],
                activities=[snaps.activity],
                plugins=[ClaudeAgentPlugin(runner, heartbeat_every=1.0)],
            ),
            Worker(client, task_queue=TOOL_QUEUE, activities=stubs.activities()),
        ):
            try:
                yield rig
            finally:
                stubs.release()
                for wid in rig.ids:
                    try:
                        await client.get_workflow_handle(wid).terminate("test over")
                    except Exception:
                        pass
    finally:
        stubs.release()
