"""What the DurableSession tests share: a session row, a stand-in agent-tools worker,
and the agent's worker on a dev server (spec 2026-10-01 §6.3)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

import psycopg
from temporalio import activity
from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from scadbuddy_durable.gate.ids import durable_request_id
from scadbuddy_durable.session import tools
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
    its recorded response, and describe_call answers a fixed summary and hash."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect
        self.ran: list[tuple[str, dict[str, Any]]] = []
        self.describe_refuses = False

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

    def activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name="render_preview")
        async def render_preview(args: dict[str, Any]) -> str:
            self.ran.append(("render_preview", args))
            return "rendered"

        @activity.defn(name="print_output")
        async def print_output(args: dict[str, Any]) -> str:
            recorded = await self._response("print_output")
            if recorded is None or recorded[0] != "approved":
                raise ApplicationError("no approval", type="NotApproved", non_retryable=True)
            self.ran.append(("print_output", args))
            return "printed"

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
            return {"summary": f"{args['tool']} call", "input_hash": "a" * 64}

        return [render_preview, print_output, ask_user, wait_for_user, describe_call]


def tools_worker(client: Client, stand_in: StandInTools) -> Worker:
    return Worker(client, task_queue=TOOLS_QUEUE, activities=stand_in.activities())


async def start_session(
    client: Client, sid: str, task_queue: str, system_append: str | None = None
) -> WorkflowHandle[DurableSession, None]:
    return await client.start_workflow(
        DurableSession.run,
        SessionStart(session_id=sid, creator=ME, owner=ME, system_append=system_append),
        id=f"session-{sid}",
        task_queue=task_queue,
        execution_timeout=timedelta(minutes=10),
    )


async def send(
    handle: WorkflowHandle[Any, Any], text: str, images: list[ImageRef] | None = None
) -> SendAnswer:
    answer: SendAnswer = await handle.execute_update(
        SEND_MESSAGE_UPDATE,
        Message(turn_id=str(uuid.uuid4()), text=text, author=ME, images=images or []),
        result_type=SendAnswer,
    )
    return answer


async def send_next(
    handle: WorkflowHandle[Any, Any], text: str, timeout: float = 30.0
) -> SendAnswer:
    """`send` for the turn after one `settled` saw end. `finish_turn` writes the idle
    `settled` reads, and the workflow leaves the turn only once that activity's result
    is back, so a send in between is refused `busy`: what a send during a turn gets.
    Only that refusal is waited out."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            return await send(handle, text)
        except WorkflowUpdateFailedError as err:
            busy = isinstance(err.cause, ApplicationError) and err.cause.type == "busy"
            if not busy or asyncio.get_running_loop().time() > deadline:
                raise
        await asyncio.sleep(0.1)
