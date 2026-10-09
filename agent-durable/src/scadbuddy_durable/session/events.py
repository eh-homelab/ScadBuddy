"""A durable turn's live output, into the session's log (plan 5c Ruling 6).

``follow_session`` is the subscriber: an activity DurableSession starts with each
turn. It reads the workflow's live output with the plugin's ``follow_agent`` from
``ai_sessions.durable_offset``, translates each event into the panel's protocol
(agent/src/sessions/protocol.ts), and appends the events and moves the offset in one
transaction, so a retried or doubled subscriber writes each event once. It ends at
the turn's last event (``done``, ``error``, ``cancelled``). The turn's result, its
error and its final status are ``finish_turn``'s, written after this ends.

What the log gets of a tool call's input is what the agent's ``scrubForLog`` lets
through: a sensitive argument's value blanked at any depth, and an input longer than
INPUT_MAX as JSON cut to a preview. Nothing of an event is logged or traced here.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

import psycopg
from temporalio import activity
from temporalio.claude_agent_sdk import follow_agent
from temporalio.exceptions import ApplicationError

from scadbuddy_durable.gate.store import PROTOCOL_VERSION, _append
from scadbuddy_durable.session import tools
from scadbuddy_durable.session.models import FollowArgs, FollowResult

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]

# agent/src/sessions/sdkEvents.ts INPUT_MAX, SENSITIVE_KEY, REDACTED.
INPUT_MAX = 4096
SENSITIVE_KEY = re.compile(
    r"secret|token|passw(or)?d|passphrase|api[-_]?key|authori[sz]ation|credential|cookie"
    r"|private[-_]?key",
    re.IGNORECASE,
)
REDACTED = "[redacted]"
# The plugin's events that end a turn's output.
LAST = frozenset({"done", "error", "cancelled"})
FOLLOW_HEARTBEAT = timedelta(seconds=30)


def _blank(value: Any) -> Any:
    if isinstance(value, list):
        return [_blank(v) for v in value]
    if isinstance(value, dict):
        return {
            k: REDACTED if SENSITIVE_KEY.search(str(k)) else _blank(v) for k, v in value.items()
        }
    return value


def logged_input(raw: Any) -> dict[str, Any]:
    """A tool call's input as the session log keeps it (sdkEvents.ts scrubForLog)."""
    value = _blank(raw) if isinstance(raw, dict) else {"input": raw}
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    # JavaScript counts UTF-16 code units; the agent's cut is by that length.
    if len(text.encode("utf-16-le")) // 2 <= INPUT_MAX:
        return value
    return {"truncated": True, "preview": text[: INPUT_MAX - 1] + "…"}


def translate(
    session_id: str, event: Mapping[str, Any], tiers: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The panel's events for one plugin event (none for the ones the panel has no use for)."""
    kind = event.get("type")
    base = {"v": PROTOCOL_VERSION, "sessionId": session_id}
    if kind == "text":
        text = event.get("text")
        if not isinstance(text, str) or not text:
            return []
        message = f"durable-{event['offset']}"
        return [
            {**base, "type": "assistant.text.delta", "messageId": message, "delta": text},
            {**base, "type": "assistant.text.done", "messageId": message},
        ]
    if kind == "tool_call":
        name = str(event.get("name", "unknown"))
        return [
            {
                **base,
                "type": "tool.call",
                "id": str(event.get("id", "")),
                "name": name,
                "input": logged_input(event.get("input", {})),
                "risk": tiers.get(name, "outward"),
            }
        ]
    if kind == "tool_result":
        status = str(event.get("status", ""))
        return [
            {
                **base,
                "type": "tool.result",
                "id": str(event.get("id", "")),
                "ok": status == "done",
                "summary": status,
            }
        ]
    # prompt (the user.turn the agent service wrote), approval_needed (the gate's
    # approval.required, §6.6), retry, continued_as_new; done, error and cancelled are
    # finish_turn's to report.
    return []


async def append_from(
    conn: psycopg.AsyncConnection[Any],
    session_id: str,
    offset: int,
    events: list[dict[str, Any]],
) -> bool:
    """Appends the events of stream offset ``offset`` and moves the offset past it.

    False, and nothing written, when another subscriber already wrote that offset.
    """
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT durable_offset FROM ai_sessions WHERE id = %s FOR UPDATE", (session_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise LookupError(f"session {session_id} does not exist")
        if int(row[0]) > offset:
            return False
        await _append(conn, session_id, events)
        await conn.execute(
            "UPDATE ai_sessions SET durable_offset = %s WHERE id = %s", (offset + 1, session_id)
        )
        return True


class SessionEvents:
    """``follow_session``, over the connection ``connect`` gives."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    async def _start(self, session_id: str) -> int:
        async with self._connect() as conn:
            cur = await conn.execute(
                "SELECT durable_offset FROM ai_sessions WHERE id = %s", (session_id,)
            )
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"session {session_id} does not exist")
        return int(row[0])

    @activity.defn(name="follow_session")
    async def follow_session(self, args: FollowArgs) -> FollowResult:
        tiers = {e.name: e.tier for e in tools.manifest()}
        at = [0]
        failed: str | None = None
        try:
            at[0] = await self._start(args.session_id)
            return await self._follow(args, at, tiers)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # An error here may quote what it failed on (a driver error quotes the row,
            # a decode error the payload). The worker logs, the failure and its span get
            # only the type and the offset, never the message; it is raised outside this
            # block so the original is not even its context (security review of 5c).
            failed = type(err).__name__
        raise ApplicationError(
            f"follow_session failed at offset {at[0]}: {failed}",
            type=failed,
            # A session that is gone stays gone: no retry until the drain cancels it.
            non_retryable=failed == "LookupError",
        )

    async def _follow(self, args: FollowArgs, at: list[int], tiers: dict[str, str]) -> FollowResult:
        written = 0

        async def beat() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(FOLLOW_HEARTBEAT.total_seconds() / 3)

        # A turn with a prompt ends only after it: an earlier turn's end, left unread when
        # its subscriber was cancelled, is written but does not end this one.
        own = not args.prompted
        beater = asyncio.create_task(beat())
        try:
            async for event in follow_agent(activity.client(), args.workflow_id, from_offset=at[0]):
                at[0] = int(event["offset"])
                own = own or event.get("type") == "prompt"
                translated = translate(args.session_id, event, tiers)
                async with self._connect() as conn:
                    if await append_from(conn, args.session_id, at[0], translated):
                        written += len(translated)
                if own and event.get("type") in LAST:
                    return FollowResult(ended=str(event["type"]), events=written)
            return FollowResult(ended="closed", events=written)
        finally:
            beater.cancel()
