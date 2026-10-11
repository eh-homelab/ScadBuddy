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

A segment that raises (#2243) never hands Temporal the engine's own exception: its text
can be anything Claude Code printed, and its traceback alone pushed the failure past
the 4 KiB Temporal keeps of an activity's last failure, which then read only "Failure
exceeds size limit.". The full text goes to the worker's log; Temporal gets a bounded
``ApplicationError`` (``bounded_failure``). A failure that running the same segment
again cannot fix (``final_failure``: a request or prompt too large) ends the turn with
that message instead of retrying.

The turn's images are checked before any model call (``image_problem``): their count,
each one's and all their bytes (sessions/images.ts's bounds) and their long edge against
the ``image_long_edge`` setting. The panel scales to that setting, but nothing scaled an
image sent another way (MCP, the attachment store), and an oversized request reached
Claude, failed, and was sent again on every retry. One over a bound ends the turn with a
readable error and no retry.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg
from claude_agent_sdk import ResultError, create_sdk_mcp_server, tool
from temporalio import activity
from temporalio.claude_agent_sdk import ClaudeAgentSdkRunner, SegmentInput, SegmentOutput
from temporalio.exceptions import ApplicationError

from scadbuddy_durable.credentials import credential_env, usable_credentials
from scadbuddy_durable.secrets import Kek
from scadbuddy_durable.session.models import (
    BUDGET_EXHAUSTED,
    SEGMENT_CONTEXT_QUERY,
    SegmentContext,
)

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]
logger = logging.getLogger(__name__)

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


# The most UTF-8 bytes of an error's text that reach Temporal: with the traceback and the
# codec's sealing, a failure stays under the 4 KiB the server keeps of an activity's last
# failure (limit.mutableStateActivityFailureSize.error).
FAILURE_TEXT_BYTES = 1024
# Errors no retry of the same segment fixes: the request is the same every attempt.
_FINAL = re.compile(
    r"\b(?:prompt|input|request|message|context|payload|image)\b[^.]{0,40}"
    r"\b(?:too (?:large|long)|exceeds)\b|too many tokens|request entity too large",
    re.IGNORECASE,
)
# sessions/images.ts and routes/imageSettings.ts: what one message's images may be.
IMAGES_MAX = 4
IMAGE_BYTES_MAX = 5 * 1024 * 1024 * 3 // 4  # 5 MB of base64, the Messages API's limit
IMAGES_BYTES_MAX = 8 * 1024 * 1024 * 3 // 4
LONG_EDGE_KEY = "image_long_edge"
DEFAULT_LONG_EDGE = 1568
MIN_LONG_EDGE, MAX_LONG_EDGE = 200, 2576
IMAGE_TOO_LARGE = "input too large: "


def image_size(data: bytes) -> tuple[int, int] | None:
    """An image's width and height from its header (PNG, JPEG, GIF, WebP), or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20]), int.from_bytes(data[20:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(
                data[27:30], "little"
            )
        if chunk == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        if chunk == b"VP8 ":
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(
                data[28:30], "little"
            ) & 0x3FFF
        return None
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            length = int.from_bytes(data[i + 2 : i + 4])
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return int.from_bytes(data[i + 7 : i + 9]), int.from_bytes(data[i + 5 : i + 7])
            i += 2 + length
    return None


def image_problem(images: list[Image], long_edge: int) -> str | None:
    """Why the turn's images cannot go to Claude, or None if they can."""
    if len(images) > IMAGES_MAX:
        return f"{IMAGE_TOO_LARGE}{len(images)} images, at most {IMAGES_MAX} per message"
    total = 0
    for i in images:
        total += len(i.data)
        if len(i.data) > IMAGE_BYTES_MAX:
            return f"{IMAGE_TOO_LARGE}image {i.name} is {len(i.data)} bytes, over 3.75 MB"
        size = image_size(i.data)
        if size is None:
            return f"{IMAGE_TOO_LARGE}image {i.name}: its dimensions cannot be read"
        if max(size) > long_edge:
            return (
                f"{IMAGE_TOO_LARGE}image {i.name} is {size[0]}x{size[1]} px, longer than the"
                f" {long_edge} px the image setting allows; send it scaled down"
            )
    if total > IMAGES_BYTES_MAX:
        return f"{IMAGE_TOO_LARGE}the images are {total} bytes together, over 6 MB"
    return None


def error_text(err: BaseException) -> str:
    """An exception's text with its causes', as the log has it."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = err
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__
    return "\n  caused by ".join(parts)


def bounded(text: str, limit: int = FAILURE_TEXT_BYTES) -> str:
    raw = text.encode()
    if len(raw) <= limit:
        return text
    note = f" [... {len(raw) - limit} more bytes in the worker's log]"
    return raw[: limit - len(note.encode())].decode(errors="ignore") + note


def final_failure(err: BaseException) -> bool:
    if isinstance(err, ResultError) and err.api_error_status == 413:
        return True
    return _FINAL.search(str(err)) is not None


def bounded_failure(err: BaseException) -> ApplicationError:
    """What Temporal records for a segment that raised: its bounded text and type, with no
    cause attached (each cause would carry its own traceback)."""
    return ApplicationError(
        bounded(error_text(err)),
        type=err.type if isinstance(err, ApplicationError) and err.type else type(err).__name__,
        non_retryable=isinstance(err, ApplicationError) and err.non_retryable,
    )


def _cost(err: BaseException) -> float:
    data = getattr(err, "data", None)
    value = data.get("total_cost_usd") if isinstance(data, dict) else None
    return float(value) if isinstance(value, int | float) else 0.0


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

    async def _long_edge(self) -> int:
        async with self._connect() as conn:
            cur = await conn.execute(
                "SELECT value FROM ai_settings WHERE key = %s", (LONG_EDGE_KEY,)
            )
            row = await cur.fetchone()
        value = None if row is None else json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if isinstance(value, bool) or not isinstance(value, int):
            return DEFAULT_LONG_EDGE
        return min(max(value, MIN_LONG_EDGE), MAX_LONG_EDGE)

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
        problem = image_problem(images, await self._long_edge()) if images else None
        if problem is not None:
            return SegmentOutput(session_id=inp.session_id, is_error=True, error=problem)
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
        try:
            out = await runner.run(inp, attempt)
        except Exception as err:
            logger.warning("segment of %s failed (attempt %d)", workflow_id, attempt, exc_info=err)
            if not final_failure(err):
                raise bounded_failure(err) from None
            # Ends the turn with the reason, as the plugin's own final errors do.
            out = SegmentOutput(
                session_id=inp.session_id,
                is_error=True,
                error=bounded(str(err)),
                cost_usd=_cost(err),
            )
        if out.cost_usd > 0 or not out.is_error:
            async with self._connect() as conn:
                await conn.execute(
                    "UPDATE ai_sessions SET cost_usd = cost_usd + %s, turns = turns + 1,"
                    " updated_at = now() WHERE id = %s",
                    (out.cost_usd, session),
                )
        return out
