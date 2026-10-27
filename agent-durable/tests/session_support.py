"""What the DurableSession tests share: a session row, a stand-in agent-tools worker,
and the agent's worker on a dev server (spec 2026-10-01 §6.3)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

import psycopg
from temporalio import activity
from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.common import RawValue
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from scadbuddy_durable.gate.ids import durable_request_id
from scadbuddy_durable.session import access, tools
from scadbuddy_durable.session.models import (
    SEND_MESSAGE_UPDATE,
    TOOLS_QUEUE,
    ImageRef,
    Message,
    Owner,
    SendAnswer,
    SessionStart,
)
from scadbuddy_durable.session.runner import session_of
from scadbuddy_durable.session.workflow import DurableSession

Conn = psycopg.AsyncConnection[Any]
Connect = Callable[[], AbstractAsyncContextManager[Conn]]

ME = Owner("browser", "browser", "You")
SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}

MANIFEST = [
    tools.ManifestEntry("render_preview", "Renders.", SCHEMA, "read", None),
    tools.ManifestEntry("print_output", "Prints.", SCHEMA, "outward", "approval"),
    tools.ManifestEntry("ask_user", "Asks.", SCHEMA, "read", "answer"),
    tools.ManifestEntry("wait_for_user", "Waits.", SCHEMA, "read", "answer"),
]


async def insert_session(conn: Conn, *, budget: float = 1.0, status: str = "running") -> str:
    sid = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO ai_sessions
          (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,
           status, max_turns, budget_usd, mode)
        VALUES (%s, 'chat', 'browser', 'browser', 'You', 'browser', 'browser',
                %s, 10, %s, 'durable')
        """,
        (sid, status, budget),
    )
    return sid


async def events(conn: Conn, sid: str) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT event FROM ai_session_events WHERE session_id = %s ORDER BY seq", (sid,)
    )
    return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in await cur.fetchall()]


async def until(
    check: Callable[[], Awaitable[bool]], timeout: float = 60.0, every: float = 0.2
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(every)


async def status(conn: Conn, sid: str) -> str:
    cur = await conn.execute("SELECT status FROM ai_sessions WHERE id = %s", (sid,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def settled(connect: Connect, sid: str, timeout: float = 60.0) -> str:
    """Waits until the session's turn has ended; returns its status."""
    seen = ""

    async def done() -> bool:
        nonlocal seen
        async with connect() as conn:
            seen = await status(conn, sid)
        return seen in ("idle", "failed")

    await until(done, timeout)
    return seen


class StandInTools:
    """agent-tools as the agent service serves it, played here: each call is recorded,
    an outward call runs only with its approval row (Ruling 10), an answer tool returns
    its recorded response, and describe_call answers a fixed summary and hash.
    The registry tools apply ai_access as toolActivities.ts does (#2088, #2171): off, a
    tool is refused naming Settings; at outward, it runs only with its approval.
    ``tiers`` and ``disabled`` lay a change over ai_access's rows, as Settings changes it
    mid-turn; ``tool_tier`` (agent temporal/toolTier.ts) answers from the same."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect
        self.ran: list[tuple[str, dict[str, Any]]] = []
        self.describe_refuses = False
        #: describe_call's ToolError, when the agent refuses the call before it parks (#4442).
        self.describe_tool_error: str | None = None
        #: The input hash describe_call gives a tool, when not the default ``"a" * 64``.
        self.input_hashes: dict[str, str] = {}
        # turn_tools' answer (agent temporal/turnTools.ts, #2320): none unless a test offers one.
        self.turn_offered: list[dict[str, Any]] = []
        self.turn_packages: list[dict[str, Any]] = []
        # What turn_tools says to tell the model (#2393, the built-ins it provides).
        self.turn_note: str | None = None
        # memory_recall's block (agent temporal/memoryActivities.ts, #2320), and each
        # memory activity's (name, turn); memory_fails makes both fail.
        self.memory_context: str | None = None
        self.memory: list[tuple[str, str]] = []
        self.memory_fails = False
        self.tier_refuses = False
        # tool_tier (#2171): the access laid over ai_access, each tool it was asked, and
        # tool_tier_refuses makes it fail.
        self.tiers: dict[str, str] = {}
        self.disabled: set[str] = set()
        self.tiers_asked: list[str] = []
        self.tool_tier_refuses = False
        self.turn_tools_fails = False
        # #2581: the replica queue turn_tools pins every offered tool to (the agent's
        # hostQueue, set when its plugin clients are kept), and the queues turn_tools and
        # turn_tools_close, and each turn tool's call, ran on.
        self.host_queue: str | None = None
        self.listed_on: list[str] = []
        self.closed_on: list[str] = []
        self.called_on: list[str] = []

    async def _response(self, tool: str) -> tuple[str, Any] | None:
        info = activity.info()
        session = session_of(info.workflow_id)
        assert session is not None
        rid = durable_request_id(session, info.workflow_run_id or "", info.activity_id[5:])
        async with self._connect() as conn:
            cur = await conn.execute(
                "SELECT outcome, response FROM ai_input_responses WHERE request_id = %s", (rid,)
            )
            row = await cur.fetchone()
        return None if row is None else (str(row[0]), row[1])

    async def _access(self, name: str) -> tuple[str, bool]:
        """A registry tool's effective tier and whether it is off, as effectiveTool has it."""
        async with self._connect() as conn:
            rows = await access.read_rows(conn, access.REGISTRY_SOURCES)
        tiers, disabled = tools.sanitise_overrides(tools.manifest(), rows)
        own = next((e.tier for e in tools.manifest() if e.name == name), "outward")
        tier = self.tiers.get(name, tiers.get(name, own))
        return tier, name in disabled or name in self.disabled

    async def _gated(self, name: str) -> None:
        """What toolActivities.ts checks before a registry tool runs: refused when off, and
        run only with its approval when its effective tier is outward."""
        tier, off = await self._access(name)
        if off:
            raise ApplicationError(
                f"{name} is disabled in Settings → Plugins; the user can turn it back on there",
                type="ToolError",
                non_retryable=True,
            )
        if tier == "outward":
            recorded = await self._response(name)
            if recorded is None or recorded[0] != "approved":
                raise ApplicationError("no approval", type="NotApproved", non_retryable=True)

    def activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name="render_preview")
        async def render_preview(args: dict[str, Any]) -> str:
            await self._gated("render_preview")
            self.ran.append(("render_preview", args))
            return "rendered"

        @activity.defn(name="print_output")
        async def print_output(args: dict[str, Any]) -> str:
            await self._gated("print_output")
            self.ran.append(("print_output", args))
            return "printed"

        @activity.defn(name="get_printer_camera")
        async def get_printer_camera(args: dict[str, Any]) -> str:
            await self._gated("get_printer_camera")
            self.ran.append(("get_printer_camera", args))
            return "a frame"

        @activity.defn(name="ask_user")
        async def ask_user(args: dict[str, Any]) -> str:
            recorded = await self._response("ask_user")
            self.ran.append(("ask_user", args))
            if recorded is None or recorded[0] != "answered":
                outcome = recorded[0] if recorded else "missing"
                raise ApplicationError(f"not answered: {outcome}", type="ToolError")
            return json.dumps(recorded[1])

        @activity.defn(name="wait_for_user")
        async def wait_for_user(args: dict[str, Any]) -> str:
            recorded = await self._response("wait_for_user")
            self.ran.append(("wait_for_user", args))
            return recorded[0] if recorded else "missing"

        @activity.defn(name="gate.describe_call")
        async def describe_call(args: dict[str, Any]) -> dict[str, str]:
            if self.describe_refuses:
                raise ApplicationError("no such session", type="UnknownSession", non_retryable=True)
            if self.describe_tool_error is not None:
                raise ApplicationError(
                    self.describe_tool_error, type="ToolError", non_retryable=True
                )
            digest = self.input_hashes.get(args["tool"], "a" * 64)
            return {"summary": f"{args['tool']} call", "input_hash": digest}

        @activity.defn(name="turn_tools")
        async def turn_tools() -> dict[str, Any]:
            if self.turn_tools_fails:
                raise ApplicationError("the agent is away", non_retryable=True)
            self.listed_on.append(activity.info().task_queue)
            pin = {"task_queue": self.host_queue} if self.host_queue else {}
            return {
                "tools": [{**t, **pin} for t in self.turn_offered],
                "packages": self.turn_packages,
                **({"system_note": self.turn_note} if self.turn_note else {}),
            }

        @activity.defn(name="turn_tools_close")
        async def turn_tools_close() -> None:
            self.closed_on.append(activity.info().task_queue)

        # As httpRequest.ts httpTierOf tiers http_request: no method, a GET or a HEAD is
        # read, anything else outward.
        def tier(args: dict[str, Any]) -> str:
            return "read" if args.get("method", "GET") in ("GET", "HEAD") else "outward"

        @activity.defn(name="turn_tool_tier")
        async def turn_tool_tier(args: dict[str, Any]) -> dict[str, str]:
            if self.tier_refuses:
                raise ApplicationError(
                    "the plugin did not answer", type="ToolError", non_retryable=True
                )
            return {"tier": tier(args["input"])}

        @activity.defn(name="tool_tier")
        async def tool_tier(args: dict[str, Any]) -> dict[str, Any]:
            self.tiers_asked.append(args["tool"])
            if self.tool_tier_refuses:
                raise ApplicationError("the agent is away", type="ToolError", non_retryable=True)
            tier, off = await self._access(args["tool"])
            return {"tier": tier, "disabled": off}

        @activity.defn(name="memory_recall")
        async def memory_recall(args: dict[str, Any]) -> dict[str, str | None]:
            self.memory.append(("memory_recall", args["turn_id"]))
            if self.memory_fails:
                raise ApplicationError("hindsight is down")
            return {"context": self.memory_context}

        @activity.defn(name="memory_retain")
        async def memory_retain(args: dict[str, Any]) -> None:
            self.memory.append(("memory_retain", args["turn_id"]))
            if self.memory_fails:
                raise ApplicationError("hindsight is down")

        @activity.defn(dynamic=True)
        async def turn_tool(raw: Sequence[RawValue]) -> str:
            name = activity.info().activity_type
            assert name.startswith("turn:"), name
            args = activity.payload_converter().from_payload(raw[0].payload, dict)
            if tier(args) == "outward":
                recorded = await self._response(name)
                if recorded is None or recorded[0] != "approved":
                    raise ApplicationError("no approval", type="NotApproved", non_retryable=True)
            self.ran.append((name, args))
            self.called_on.append(activity.info().task_queue)
            return f"{name} did {args.get('method')}"

        return [
            render_preview,
            print_output,
            get_printer_camera,
            ask_user,
            wait_for_user,
            describe_call,
            turn_tools,
            turn_tools_close,
            turn_tool_tier,
            tool_tier,
            turn_tool,
            memory_recall,
            memory_retain,
        ]


def tools_worker(client: Client, stand_in: StandInTools) -> Worker:
    return Worker(client, task_queue=TOOLS_QUEUE, activities=stand_in.activities())


async def start_session(
    client: Client,
    sid: str,
    task_queue: str,
    system_append: str | None = None,
    execution_timeout: timedelta = timedelta(minutes=10),
) -> WorkflowHandle[DurableSession, None]:
    return await client.start_workflow(
        DurableSession.run,
        SessionStart(session_id=sid, creator=ME, owner=ME, system_append=system_append),
        id=f"session-{sid}",
        task_queue=task_queue,
        execution_timeout=execution_timeout,
    )


async def send(
    handle: WorkflowHandle[Any, Any],
    text: str,
    images: list[ImageRef] | None = None,
    system_append: str | None = None,
) -> SendAnswer:
    answer: SendAnswer = await handle.execute_update(
        SEND_MESSAGE_UPDATE,
        Message(
            turn_id=str(uuid.uuid4()),
            text=text,
            author=ME,
            images=images or [],
            system_append=system_append,
        ),
        result_type=SendAnswer,
    )
    return answer


async def send_next(
    handle: WorkflowHandle[Any, Any],
    text: str,
    timeout: float = 30.0,
    system_append: str | None = None,
) -> SendAnswer:
    """`send` for the turn after one `settled` saw end. `finish_turn` writes the idle
    `settled` reads, and the workflow leaves the turn only once that activity's result
    is back, so a send in between is refused `busy`: what a send during a turn gets.
    Only that refusal is waited out."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            return await send(handle, text, system_append=system_append)
        except WorkflowUpdateFailedError as err:
            busy = isinstance(err.cause, ApplicationError) and err.cause.type == "busy"
            if not busy or asyncio.get_running_loop().time() > deadline:
                raise
        await asyncio.sleep(0.1)
