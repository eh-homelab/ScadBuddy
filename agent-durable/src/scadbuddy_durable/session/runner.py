"""The segment runner DurableSession's plugin gets (plan 5c Rulings 2, 3 and 5).

For each segment, ``ScadBuddyRunner``:

- reads the session (from the activity's workflow ID; only a durable one) and what
  its lineage may still spend. A segment with nothing left to spend ends the task as
  ``budget_exhausted``, without a model call;
- opens the first usable credential (``credentials.usable_credentials``), so the
  workflow never holds it, and nothing of it enters history;
- runs the segment with the plugin's ``ClaudeAgentSdkRunner``, built for this segment
  with that credential's environment and the remaining budget as its cap;
- adds what the segment cost to ``ai_sessions.cost_usd`` and one to ``turns``. Every
  attempt that reports a cost is counted: each was really spent.

The images the user sent with the turn are offered through a read-only in-segment
MCP server, ``scadbuddy_images``, whose one tool, ``view_user_images``, returns them
from ``ai_session_blobs`` as image blocks (Ruling 5). The plugin's prompt is text only.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg
from claude_agent_sdk import create_sdk_mcp_server, tool
from temporalio import activity
from temporalio.claude_agent_sdk import ClaudeAgentSdkRunner, SegmentInput, SegmentOutput

from scadbuddy_durable.credentials import credential_env, usable_credentials
from scadbuddy_durable.secrets import Kek
from scadbuddy_durable.session.models import (
    BUDGET_EXHAUSTED,
    SEGMENT_CONTEXT_QUERY,
    SegmentContext,
)

log = logging.getLogger(__name__)

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]

IMAGES_SERVER = "scadbuddy_images"
VIEW_IMAGES = "view_user_images"
VIEW_IMAGES_TOOL = f"mcp__{IMAGES_SERVER}__{VIEW_IMAGES}"
NO_CREDENTIAL = (
    "no usable Claude credential: save one in Settings, or check the secret key it was saved under"
)
NOT_DURABLE = "this workflow is no durable ScadBuddy session"
# What harness/run.ts sets for every classic query beside the credential.
ENGINE_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
}
_SESSION_WORKFLOW = re.compile(
    r"^session-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
# The lineage's budget and spend (agent/src/sessions/manager.ts POOL_BUDGET, POOL_COST).
_POOL = """
    SELECT
      coalesce((SELECT r.budget_usd FROM ai_sessions r
                WHERE r.id = coalesce(s.budget_root_id, s.id)), s.budget_usd),
      (SELECT sum(m.cost_usd) FROM ai_sessions m
       WHERE coalesce(m.budget_root_id, m.id) = coalesce(s.budget_root_id, s.id))
    FROM ai_sessions s WHERE s.id = %s AND s.mode = 'durable'
"""


def worker_env_blanked() -> dict[str, str]:
    """The worker's own settings, emptied for the engine, which inherits the worker's
    environment: the database URL (with its password) and the key files' paths are no
    business of Claude Code's."""
    return {k: "" for k in os.environ if k.startswith(("SCADBUDDY_", "OTEL_", "TEMPORAL_"))}


def session_of(workflow_id: str | None) -> str | None:
    match = _SESSION_WORKFLOW.match(workflow_id or "")
    return match.group(1) if match else None


class SegmentRunner(Protocol):
    async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput: ...


# How a segment's own runner is made: the plugin's, or a test's.
MakeRunner = Callable[..., SegmentRunner]


@dataclass(frozen=True)
class Image:
    name: str
    media_type: str
    data: bytes


def images_server(images: list[Image]) -> Any:
    """The in-segment MCP server that hands the model the turn's images."""

    @tool(
        VIEW_IMAGES,
        "Shows you the images the user attached to their latest message. Call it once"
        " before you answer whenever the message says images are attached.",
        {"type": "object", "properties": {}},
        annotations=None,
    )
    async def view(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [
                {
                    "type": "image",
                    "data": base64.b64encode(i.data).decode(),
                    "mimeType": i.media_type,
                }
                for i in images
            ]
        }

    return create_sdk_mcp_server(IMAGES_SERVER, tools=[view])


class ScadBuddyRunner:
    def __init__(
        self,
        connect: Connect,
        kek: Kek,
        *,
        cwd: str,
        plugin_dir: str | None = None,
        make_runner: MakeRunner = ClaudeAgentSdkRunner,
    ) -> None:
        self._connect = connect
        self._kek = kek
        self._cwd = cwd
        self._plugin_dir = plugin_dir
        self._make = make_runner

    def __repr__(self) -> str:
        return f"ScadBuddyRunner(kek={self._kek.id})"

    async def _images(self, session: str, context: SegmentContext) -> list[Image]:
        names = [r.name for r in context.images]
        if not names:
            return []
        async with self._connect() as conn:
            cur = await conn.execute(
                "SELECT name, media_type, data FROM ai_session_blobs"
                " WHERE session_id = %s AND name = ANY(%s)",
                (session, names),
            )
            found = {r[0]: Image(r[0], r[1], bytes(r[2])) for r in await cur.fetchall()}
        return [found[n] for n in names if n in found]

    async def run(self, inp: SegmentInput, attempt: int) -> SegmentOutput:
        workflow_id = activity.info().workflow_id
        session = session_of(workflow_id)
        if session is None or workflow_id is None:
            return SegmentOutput(session_id=inp.session_id, is_error=True, error=NOT_DURABLE)
        async with self._connect() as conn:
            cur = await conn.execute(_POOL, (session,))
            row = await cur.fetchone()
            if row is None:
                return SegmentOutput(session_id=inp.session_id, is_error=True, error=NOT_DURABLE)
            budget, spent = float(row[0]), float(row[1] or 0)
            if spent >= budget:
                return SegmentOutput(
                    session_id=inp.session_id, is_error=True, error=BUDGET_EXHAUSTED
                )
            credentials = await usable_credentials(conn, self._kek)
        if not credentials:
            return SegmentOutput(session_id=inp.session_id, is_error=True, error=NO_CREDENTIAL)
        extra: dict[str, Any] = {}
        context = await (
            activity.client()
            .get_workflow_handle(workflow_id)
            .query(SEGMENT_CONTEXT_QUERY, result_type=SegmentContext)
        )
        # Claude Code's own prompt and the agent's append, as a classic turn has them.
        extra["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            **({"append": context.system_append} if context.system_append else {}),
        }
        images = await self._images(session, context)
        if images:
            extra["mcp_servers"] = {IMAGES_SERVER: images_server(images)}
            extra["allowed_tools"] = [VIEW_IMAGES_TOOL]
        if self._plugin_dir:
            extra["plugins"] = [{"type": "local", "path": self._plugin_dir}]
        runner = self._make(
            cwd=self._cwd,
            env={**worker_env_blanked(), **ENGINE_ENV, **credential_env(credentials[0])},
            max_budget_usd=budget - spent,
            extra_options=extra,
        )
        out = await runner.run(inp, attempt)
        if out.cost_usd > 0 or not out.is_error:
            async with self._connect() as conn:
                await conn.execute(
                    "UPDATE ai_sessions SET cost_usd = cost_usd + %s, turns = turns + 1,"
                    " updated_at = now() WHERE id = %s",
                    (out.cost_usd, session),
                )
        return out
