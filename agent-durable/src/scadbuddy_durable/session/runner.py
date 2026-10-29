"""The segment runner DurableSession's plugin gets (plan 5c Rulings 2, 3 and 5).

For each segment, ``ScadBuddyRunner``:

- reads the session (from the activity's workflow ID; only a durable one) and what
  its lineage may still spend. A segment with nothing left to spend ends the task as
  ``budget_exhausted``, without a model call;
- opens the usable credentials (``credentials.usable_credentials``), so the workflow
  never holds one, and nothing of them enters history; the segment runs on the first,
  and on the next whenever a failure was the credential's (``fallback.py``), as a
  classic turn falls back (``harness/fallback.ts``);
- runs the segment with the plugin's ``ClaudeAgentSdkRunner``, built for this segment
  with that credential's environment and the remaining budget as its cap;
- adds what the segment cost to ``ai_sessions.cost_usd`` and one to ``turns``. Every
  attempt that reports a cost is counted: each was really spent. A segment that reached
  its cap is charged at least what was left (the plugin reports no cost for it), and
  ends the task as ``budget_exhausted``.

The images the user sent with the turn are offered through a read-only in-segment
MCP server, ``scadbuddy_images``, whose tool ``view_user_images`` returns them
from ``ai_session_blobs`` as image blocks (Ruling 5). The plugin's prompt is text only.
The same server's ``view_tool_images``, on every segment, returns the images a durable
tool returned, which the agent kept in ``ai_session_blobs`` and named in the tool's
result text (#2320): a durable activity's result reaches the model as text only.

A segment that raises (#2243) never hands Temporal the engine's own exception: its text
can be anything Claude Code printed, and its traceback alone pushed the failure past
the 4 KiB Temporal keeps of an activity's last failure, which then read only "Failure
exceeds size limit.". Temporal gets a bounded ``ApplicationError`` (``bounded_failure``),
sealed by the codec like every failure; the worker's log gets only the error's type and
size, never its text, which is the session's content. A failure that running the same segment
again cannot fix (``final_failure``: a request or prompt too large) ends the turn with
that message instead of retrying.

The turn's images are checked before any model call (``image_problem``): their count,
each one's and all their bytes (sessions/images.ts's bounds) and their long edge against
the ``image_long_edge`` setting. The panel scales to that setting, but nothing scaled an
image sent another way (MCP, the attachment store), and an oversized request reached
Claude, failed, and was sent again on every retry. One over a bound ends the turn with a
readable error and no retry.

Settings → Model is the segment's model, and the own plugin's skills load only while
Settings → Tools & plugins has it on, read per segment as a classic turn reads them per turn.
The turn's plugin packages (#2320) load from the payload volume, each only once it
hashes to its pin (``package_hash.py``). The own plugin loads without its items switched
off (#4757, ``own_plugin_copy``; its off subagents are not defined), and every Skill, Agent
or Task call is judged by access as it is made (``CallAccess``), as a classic turn's are.

The tools Settings → Tools & plugins disabled (the workflow's ``segment_context``) are left out
of the segment's declared tools (#2088), so Claude is never offered them.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeGuard

import psycopg
from claude_agent_sdk import (
    AgentDefinition,
    HookMatcher,
    ResultError,
    create_sdk_mcp_server,
    tool,
)
from temporalio import activity
from temporalio.claude_agent_sdk import (
    ClaudeAgentSdkRunner,
    SegmentInput,
    SegmentOutput,
    ToolOutcome,
)

# Private to the git-pinned plugin (#2945): the per-activity ContextVar its `emit` reads
# as it publishes. A pin bump (ai-integrations#33 and after) must check it still is, or
# HeldText stops holding anything, with no error. Guarded by the tests
# test_the_plugins_runner_publishes_text_through_the_held_contextvar and
# test_a_refused_credentials_scripted_notice_never_reaches_the_transcript (#3383).
from temporalio.claude_agent_sdk._events import _EMIT, emit
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from scadbuddy_durable.credentials import (
    Credential,
    Health,
    audit_health,
    cooldown_until,
    credential_env,
    rate_limit_reset,
    record_health,
    usable_credentials,
)
from scadbuddy_durable.secrets import Kek
from scadbuddy_durable.session import access, subagents
from scadbuddy_durable.session.fallback import classify, credentials_fault
from scadbuddy_durable.session.models import (
    BUDGET_EXHAUSTED,
    BUILTIN_CALL_UPDATE,
    BUILTIN_RELEASE_UPDATE,
    PARK_DEADLINE_S,
    SEGMENT_CONTEXT_QUERY,
    SEGMENT_TIMEOUT,
    SegmentContext,
    TurnPackage,
)
from scadbuddy_durable.session.package_hash import PackageContentError, hash_tree

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]
logger = logging.getLogger(__name__)


def _http_url(value: object) -> TypeGuard[str]:
    """Whether ``value`` is an absolute http(s) URL with a host."""
    if not isinstance(value, str) or not value:
        return False
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


#: Schemes a refused URL may be named by. Any other "scheme" may be part of the URL:
#: urlsplit reads ``localhost:8081/mcp``'s host as its scheme.
NAMED_SCHEMES = frozenset({"file", "javascript", "data", "ftp", "mailto", "ws", "wss"})


def _url_problem(value: str) -> str:
    """Why :func:`_http_url` refused ``value``, naming nothing of the URL but its scheme.
    Only for a value it refused: an accepted URL would read as "no host". Not asserted:
    a misuse should not fail a worker's segment (#3765); tests/test_subagents.py pins it."""
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return "unparseable"
    if parts.scheme not in ("http", "https"):
        return f"scheme {parts.scheme!r}" if parts.scheme in NAMED_SCHEMES else "no scheme"
    return "no host"


IMAGES_SERVER = "scadbuddy_images"
VIEW_IMAGES = "view_user_images"
VIEW_IMAGES_TOOL = f"mcp__{IMAGES_SERVER}__{VIEW_IMAGES}"
# The images a durable tool returned (#2320): the agent keeps each in ai_session_blobs
# and names it in the result (agent/src/temporal/toolActivities.ts `resultText`).
VIEW_TOOL_IMAGES = "view_tool_images"
VIEW_TOOL_IMAGES_TOOL = f"mcp__{IMAGES_SERVER}__{VIEW_TOOL_IMAGES}"
TOOL_IMAGES_MAX = 4
# agent/src/sessions/blobs.ts BLOB_NAME: a name that is not this shape is never looked up.
BLOB_NAME = re.compile(r"^[0-9a-f]{64}\.(png|jpg|gif|webp)$")
# The Messages API's own bound on an image's long edge: a tool's image is not the user's
# upload, so the image_long_edge setting (the panel's scaling) is not its bound.
API_LONG_EDGE = 8000
NO_CREDENTIAL = (
    "no usable Claude credential: save one in Settings, or check the secret key it was saved under"
)
NOT_DURABLE = "this workflow is no durable ScadBuddy session"
# What harness/run.ts sets for every classic query beside the credential.
ENGINE_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    # Claude Code names a subscription limit's reset in its own zone, and
    # rate_limit_reset reads only "(UTC)" (#4107; agent/src/harness/options.ts).
    "TZ": "UTC",
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
# Claude Code's own words when a segment reaches its max_budget_usd (the result's
# subtype is error_max_budget_usd; its text is "Reached maximum budget ($N)"). Only those
# words with the amount that follows, and only at the start: the SDK raises them as
# "Claude Code returned an error result: Reached maximum budget ($N) (exit code: 1)"
# (claude-agent-sdk 0.2.164, the pin in pyproject.toml: claude_agent_sdk/_internal/
# query.py:523 builds that prefix; check it on every bump), the CLI's own errors first.
# The rest of an error's text is the session's content, and one that quotes the subtype
# or the words, even the whole phrase, is not the cap (#2675).
_BUDGET_REACHED = re.compile(
    r"(?:Claude Code returned an error result: )?Reached maximum budget \(\$"
)
# sessions/images.ts and routes/imageSettings.ts: what one message's images may be.
IMAGES_MAX = 4
IMAGE_BYTES_MAX = 5 * 1024 * 1024 * 3 // 4  # 5 MB of base64, the Messages API's limit
IMAGES_BYTES_MAX = 8 * 1024 * 1024 * 3 // 4
LONG_EDGE_KEY = "image_long_edge"
DEFAULT_LONG_EDGE = 1568
MIN_LONG_EDGE, MAX_LONG_EDGE = 200, 2576
# agent/src/sessions/manager.ts SETTING_MODEL.
MODEL_KEY = "model"
# agent/src/routes/durableSubagents.ts SETTING_DURABLE_SUBAGENTS: `{url, peers}`; a string
# `url` replaces the worker's own subagent URL (an agent-durable outside the agent's pod).
SUBAGENTS_KEY = "durable_subagents"
IMAGE_TOO_LARGE = "input too large: "
# agent/src/temporal/turnPackages.ts sharedName: a package's folder on the shared volume.
# A shared copy's folder (agent/src/temporal/turnPackages.ts sharePackage): `<hash>`, as an
# agent before #4757 shares it, or `<hash>/<package name>` (vet.ts SAFE_NAME_RE), so Claude
# Code names a root skill and a manifest-less package by the package's name.
_SHARED_NAME = re.compile(r"[0-9a-f]{64}(?:/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,63}))?")


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


def _remote_entry(config: Any) -> bool:
    # plugins/packages/mcp.ts isRemoteEntry
    return isinstance(config, dict) and (
        config.get("command") is None
        and (isinstance(config.get("url"), str) or config.get("type") in ("http", "sse"))
    )


def declares_remote_servers(root: str) -> bool:
    """Whether a package declares an MCP server reached by URL, in its ``.mcp.json`` or
    its manifest's ``mcpServers`` (inline, or the files it names), as
    plugins/packages/mcp.ts ``declaresRemoteServers`` reads them. A config that cannot
    be read counts as declaring one."""
    maps: list[Any] = []

    def read(rel: str, required: bool) -> Any:
        full = os.path.join(root, rel)
        if not required and not os.path.exists(full):
            return None
        with open(full, encoding="utf-8") as f:
            return json.load(f)

    try:
        maps.append(read(".mcp.json", False))
        manifest = read(os.path.join(".claude-plugin", "plugin.json"), False)
        declared = manifest.get("mcpServers") if isinstance(manifest, dict) else None
        for entry in declared if isinstance(declared, list) else [declared]:
            if isinstance(entry, str):
                if re.match(r"https?://", entry) or re.search(r"\.(mcpb|dxt)$", entry, re.I):
                    continue
                maps.append(read(entry, True))
            else:
                maps.append(entry)
    # PEP 758 (3.14): both types, unparenthesised; not Python 2's `except E, name`.
    except OSError, ValueError:
        return True
    for m in maps:
        servers = (
            m.get("mcpServers")
            if isinstance(m, dict) and isinstance(m.get("mcpServers"), dict)
            else m
        )
        if isinstance(servers, dict) and any(_remote_entry(c) for c in servers.values()):
            return True
    return False


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
    note = f" [... {len(raw) - limit} more bytes]"
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
class TurnSettings:
    long_edge: int
    model: str | None
    own_plugin: bool
    # The own plugin's items switched off (#4757): its `builtin:own-plugin` rows whose
    # access is `off` (`skill:<name>`, `subagent:<name>`), left out of what the segment loads.
    own_off: frozenset[str] = frozenset()
    # A Workflow agent's chat (agent/src/background/workflowAgents.ts): its own effort and
    # the subagent definition its turns run as; its own model is `model`.
    effort: str | None = None
    agent_type: str | None = None


@dataclass(frozen=True)
class Image:
    name: str
    media_type: str
    data: bytes


LoadImages = Callable[[list[str]], Awaitable[list[Image]]]


def tool_images_loader(connect: Connect, session: str) -> LoadImages:
    """Reads the named images of this session's blobs, and of no other session's."""

    async def load(names: list[str]) -> list[Image]:
        async with connect() as conn:
            cur = await conn.execute(
                "SELECT name, media_type, data FROM ai_session_blobs"
                " WHERE session_id = %s AND name = ANY(%s)",
                (session, names),
            )
            found = {r[0]: Image(r[0], r[1], bytes(r[2])) for r in await cur.fetchall()}
        return [found[n] for n in names if n in found]

    return load


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def view_tool_images_tool(load: LoadImages) -> Any:
    """``view_tool_images``: the images a tool returned, by the names its result gave."""

    @tool(
        VIEW_TOOL_IMAGES,
        "Shows you images a tool returned. A tool result that carried images names them"
        " ([image <name>]); call this with those names to see them, at most"
        f" {TOOL_IMAGES_MAX} per call.",
        {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": TOOL_IMAGES_MAX,
                }
            },
            "required": ["names"],
        },
        annotations=None,
    )
    async def view(args: dict[str, Any]) -> dict[str, Any]:
        names = args.get("names")
        if not isinstance(names, list) or not names or len(names) > TOOL_IMAGES_MAX:
            return _error(f"names takes 1 to {TOOL_IMAGES_MAX} image names a tool result gave")
        wanted = list(dict.fromkeys(str(n) for n in names))
        bad = [n for n in wanted if not BLOB_NAME.match(n)]
        if bad:
            return _error(f"not an image name a tool result gave: {', '.join(bad)}")
        images = await load(wanted)
        missing = [n for n in wanted if n not in {i.name for i in images}]
        if missing:
            return _error(f"no image of this session is named {', '.join(missing)}")
        problem = image_problem(images, API_LONG_EDGE)
        if problem is not None:
            return _error(problem)
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

    return view


def images_server(images: list[Image], load_tool_images: LoadImages) -> Any:
    """The in-segment MCP server that hands the model the turn's images: the user's
    (``view_user_images``, only when the turn has some) and its tools' (#2320)."""
    tools = [view_tool_images_tool(load_tool_images)]
    if images:
        tools.append(_user_images_tool(images))
    return create_sdk_mcp_server(IMAGES_SERVER, tools=tools)


def _user_images_tool(images: list[Image]) -> Any:
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

    return view


# The plugin's own text for a call a human did not approve (its _workflow.py, decide(False)).
PLUGIN_REJECTION = "A human reviewer rejected this action. Do not retry it."


def with_refusals(
    injected: dict[str, Any],
    refusals: dict[str, str],
    replaced: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The results to deliver, each refused one followed by why it was not run (#2392),
    and each the agent refused before it parked told only the agent's reason in place of
    the rejection, since no person rejected it (#4442). Only the plugin's own rejection is
    changed, and it stays an error."""
    out = dict(injected)
    texts = {call_id: f"{PLUGIN_REJECTION}\n\n{why}" for call_id, why in refusals.items()}
    texts.update(replaced or {})
    for call_id, text in texts.items():
        result = out.get(call_id)
        if isinstance(result, ToolOutcome) and result.content == PLUGIN_REJECTION:
            out[call_id] = replace(result, content=text)
        elif isinstance(result, dict) and result.get("content") == PLUGIN_REJECTION:
            out[call_id] = {**result, "content": text}
    return out


class HeldText:
    """A segment's live output with its ``text`` events held back until the next other
    event, or until the attempt ends: then they go out in order (``flush``), unless the
    attempt was refused and they are dropped (``drop``, #2945). A refused attempt's
    text is the refusal notice, with nothing after it."""

    def __init__(self, publish: Callable[[dict[str, Any]], None]) -> None:
        self._publish = publish
        self._held: list[dict[str, Any]] = []

    def __call__(self, event: dict[str, Any]) -> None:
        if event.get("type") == "text":
            self._held.append(event)
            return
        self.flush()
        self._publish(event)

    def flush(self) -> None:
        held, self._held = self._held, []
        for event in held:
            self._publish(event)

    def drop(self) -> None:
        self._held = []

    def mark_notice(self) -> None:
        """Marks what is held as a refused credential's notice (#3703): still published,
        as the panel's reason the turn failed, but ``notice``, so a retain never keeps it
        as the assistant's (memoryActivities.ts ``transcriptOf``)."""
        self._held = [{**event, "notice": True} for event in self._held]


#: The plugin's own server (its ``_runner.PREFIX``): calls the workflow runs and publishes.
DURABLE_PREFIX = "mcp__durable__"
#: Claude Code's subagent tool, under its current and older names (agent harness/ownPlugin.ts).
AGENT_TOOLS = frozenset({"Agent", "Task"})
#: The subagent an ``Agent`` call runs when its input names none (Claude Code's default).
DEFAULT_SUBAGENT = "general-purpose"


#: A PreToolUse hook's answer for one call's input: ``{}`` to let it run, or a deny.
CallJudge = Callable[[Any], Awaitable[dict[str, Any]]]
#: Asks the gate about a call (tool_use id, tool, input), waiting while it is parked:
#: ``{"go": bool, "reason": str | None}`` (workflow.py ``builtin_call``).
ParkCall = Callable[[str, str, Any], Awaitable[dict[str, Any]]]
ReleaseCall = Callable[[str], Awaitable[None]]


def segment_gate(client: Client, workflow_id: str, run_id: str) -> tuple[ParkCall, ReleaseCall]:
    """The hook's two Updates of its own session's run (Task 6 re-review R4): ask about a
    call (``builtin_call``) and, when the segment ends before it is decided, release it
    (``builtin_release``). A later run knows neither call, so it would refuse both anyway."""
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)

    async def park(call: str, tool: str, tool_input: Any) -> dict[str, Any]:
        answer: dict[str, Any] = await handle.execute_update(
            BUILTIN_CALL_UPDATE,
            {"call_id": call, "tool": tool, "input": tool_input},
            result_type=dict,
        )
        return answer

    async def release(call: str) -> None:
        await handle.execute_update(BUILTIN_RELEASE_UPDATE, {"call_id": call})

    return park, release


class SegmentCalls:
    """The calls Claude Code runs inside a segment, published as they start and end, as
    a classic turn's feed shows them (sdkEvents.ts): ``Skill``, ``Agent``, a subagent's
    own calls and the images tools. The plugin publishes only the calls the workflow runs
    (its own server's and ``tool_activities``), and of the segment itself only the main
    agent's text, so a turn spent in a skill or a subagent showed nothing until it ended.

    A subagent's call names the ``Agent`` call that runs it as ``parent``. The hooks do
    not say which that is (#4184): a subagent's ``SubagentStart`` names its ``agent_id``
    and type, so it is the one open ``Agent`` call of that ``subagent_type``, or the one
    open ``Agent`` call at all; of several alike none is named, never the wrong one. A
    claimed subagent whose ``Agent`` call has ended names none either, not another one.

    An attempt that fails or answers with an error leaves the calls it opened with no end:
    ``close_open`` ends each as ``failed``, so the panel shows none of them running past it
    (#4184)."""

    def __init__(self, activities: list[str], judge: CallJudge | None = None) -> None:
        self._activities = activities
        # Asked of each Skill, Agent or Task call before it runs (#4757, ``CallAccess``): a
        # deny it answers is the hook's answer, and the call ends at once as failed, since
        # Claude Code runs no PostToolUse for a call a hook refused.
        self._judge = judge
        # Open Agent calls: their subagent_type, until a SubagentStart claims one.
        self._agents: dict[str, str] = {}
        # A subagent's agent_id -> the Agent call that runs it.
        self._owners: dict[str, str] = {}
        # Every call opened and not yet ended: id -> name.
        self._open: dict[str, str] = {}

    def _workflows(self, name: str) -> bool:
        """The plugin's ``decides_alone``: a call the workflow runs and publishes itself."""
        tool = name.split("(", 1)[0].strip()
        return name.startswith(DURABLE_PREFIX) or any(
            fnmatch.fnmatchcase(tool, p) for p in self._activities if p
        )

    def _parent(self, agent_id: str) -> str | None:
        if agent_id in self._owners:
            # Claimed: its own Agent call, or none once that has ended, never another's.
            owner = self._owners[agent_id]
            return owner if owner in self._agents else None
        return next(iter(self._agents)) if len(self._agents) == 1 else None

    async def _started(self, data: Any, tool_use_id: str | None, _context: Any) -> Any:
        name = str(data.get("tool_name") or "")
        call = str(data.get("tool_use_id") or tool_use_id or "")
        if not call or self._workflows(name):
            if not call and self._judge is not None and name in access.CALL_TOOLS:
                return await self._judge(data)  # judged whatever Claude Code sends
            return {}
        event: dict[str, Any] = {
            "type": "tool_call",
            "id": call,
            "name": name,
            "input": data.get("tool_input") or {},
        }
        agent = data.get("agent_id")
        if agent:
            parent = self._parent(str(agent))
            if parent is not None:
                event["parent"] = parent
        elif name in AGENT_TOOLS:
            kind = (data.get("tool_input") or {}).get("subagent_type")
            self._agents[call] = str(kind) if kind else DEFAULT_SUBAGENT
        self._open[call] = name
        emit(event)
        if self._judge is not None and name in access.CALL_TOOLS:
            decision = await self._judge(data)
            if decision:
                self._agents.pop(call, None)
                self._open.pop(call, None)
                emit({"type": "tool_result", "id": call, "name": name, "status": "failed"})
                return decision
        return {}

    async def _subagent_started(self, data: Any, _tool_use_id: str | None, _context: Any) -> Any:
        agent, kind = data.get("agent_id"), data.get("agent_type")
        if not agent:
            return {}
        claimed = set(self._owners.values())
        alike = [
            c for c, k in self._agents.items() if same_subagent(k, str(kind)) and c not in claimed
        ]
        if len(alike) == 1:
            self._owners[str(agent)] = alike[0]
        return {}

    async def _ended(self, data: Any, tool_use_id: str | None, _context: Any) -> Any:
        name = str(data.get("tool_name") or "")
        call = str(data.get("tool_use_id") or tool_use_id or "")
        if not call or self._workflows(name):
            return {}
        self._agents.pop(call, None)
        self._open.pop(call, None)
        failed = data.get("hook_event_name") == "PostToolUseFailure"
        emit(
            {
                "type": "tool_result",
                "id": call,
                "name": name,
                "status": "failed" if failed else "done",
            }
        )
        return {}

    def close_open(self, publish: Callable[[dict[str, Any]], None]) -> None:
        """Ends every call still open as ``failed``: its attempt ended with an error, raised or
        returned (the budget cap among them), and a retry or the next credential opens its
        calls again under new ids."""
        opened, self._open = self._open, {}
        self._agents.clear()
        self._owners.clear()
        for call, name in opened.items():
            publish({"type": "tool_result", "id": call, "name": name, "status": "failed"})

    def hooks(self) -> dict[str, list[HookMatcher]]:
        return {
            # Longer than anything the hook waits for (re-review R2): Claude Code's default is
            # 60 s, and a hook it times out lets the call go on to the permission check, which
            # allows Skill. The hook's own deadline on the gate (PARK_DEADLINE_S) comes first.
            "PreToolUse": [HookMatcher(hooks=[self._started], timeout=HOOK_TIMEOUT_S)],
            "PostToolUse": [HookMatcher(hooks=[self._ended])],
            "PostToolUseFailure": [HookMatcher(hooks=[self._ended])],
            "SubagentStart": [HookMatcher(hooks=[self._subagent_started])],
        }


def same_subagent(given: str, started: str) -> bool:
    """Whether an ``Agent`` call's ``subagent_type`` names the type its ``SubagentStart``
    reports: equal, or one the plugin-qualified form of the other (``scadbuddy:model-author``
    and ``model-author``), since which form the hook reports is not measured here. Two
    qualified types must be equal: ``a:reviewer`` is not ``b:reviewer``."""
    if given == started:
        return True
    if ":" in given and ":" in started:
        return False
    return given.endswith(f":{started}") or started.endswith(f":{given}")


def merged_hooks(
    given: dict[str, list[HookMatcher]] | None, ours: dict[str, list[HookMatcher]]
) -> dict[str, list[HookMatcher]]:
    """``ours`` added to hooks already in ``extra_options`` per event (#4184), never in
    their place: a pin or another option that sets hooks keeps them."""
    out = {event: list(matchers) for event, matchers in (given or {}).items()}
    for event, matchers in ours.items():
        out[event] = [*out.get(event, []), *matchers]
    return out


#: The own plugin's name as Claude Code gives it (a folder with no manifest is named after
#: itself, the image's /app/plugins/scadbuddy; agent plugins/packages/vet.ts OWN_PLUGIN_NAME).
OWN_PLUGIN_NAME = "scadbuddy"
#: Where a filtered copy of the own plugin is kept (``own_plugin_copy``).
OWN_PLUGIN_ROOT = os.path.join(tempfile.gettempdir(), "scadbuddy-own-plugin")


def frontmatter_name(path: Path) -> str | None:
    """The ``name:`` of a markdown file's frontmatter, or None (no frontmatter, no name, or
    a file that cannot be read)."""
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except OSError, UnicodeDecodeError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name" and not line.startswith(" "):
            return value.strip().strip("'\"") or None
    return None


def _own_skills(plugin_dir: str) -> dict[str, Path]:
    root = Path(plugin_dir) / "skills"
    if not root.is_dir():
        return {}
    return {d.name: d for d in sorted(root.iterdir()) if (d / "SKILL.md").is_file()}


def _agent_files(*dirs: str | None) -> dict[str, Path]:
    """Each subagent file's stem to the file, of every ``*.md`` in ``dirs``."""
    found: dict[str, Path] = {}
    for d in dirs:
        if d is not None and Path(d).is_dir():
            for f in sorted(Path(d).glob("*.md")):
                found.setdefault(f.stem, f)
    return found


def own_call_plugin(plugin_dir: str, agents_dir: str | None) -> access.CallPlugin:
    """The own plugin as the call rule sees it (agent turnRunner.ts ``callPlugin`` of
    ``itemsOfPlugin``): its skills by folder, its subagents by file stem (the image keeps
    them outside the plugin, ``agents_dir``), and each frontmatter ``name`` that differs as
    an alias, whole, as the classic call rule has it (``PackageItems.declared``)."""
    items: list[str] = []
    aliases: dict[str, list[str]] = {}

    def alias(key: str, item: str) -> None:
        aliases.setdefault(key, [])
        if item not in aliases[key]:
            aliases[key].append(item)

    for name, folder in _own_skills(plugin_dir).items():
        items.append(f"skill:{name}")
        declared = frontmatter_name(folder / "SKILL.md")
        if declared is not None and declared != name:
            alias(f"skill:{declared}", f"skill:{name}")
    for stem, file in _agent_files(os.path.join(plugin_dir, "agents"), agents_dir).items():
        items.append(f"subagent:{stem}")
        declared = frontmatter_name(file)
        if declared is not None and declared != stem:
            alias(f"subagent:{declared}", f"subagent:{stem}")
    return access.CallPlugin(
        OWN_PLUGIN_NAME,
        access.OWN_PLUGIN_SOURCE,
        tuple(items),
        {k: tuple(v) for k, v in aliases.items()},
    )


def _off_names(off: frozenset[str], kind: str) -> set[str]:
    prefix = f"{kind}:"
    return {i[len(prefix) :] for i in off if i.startswith(prefix)}


#: A copy no segment used for this long is swept, as the agent sweeps its package copies
#: (turnPackages.ts PACKAGE_COPY_KEPT_MS); a half-made one after TMP_KEPT_S.
COPY_KEPT_S = 7 * 24 * 3600
TMP_KEPT_S = 3600
_COPY_NAME = re.compile(r"[0-9a-f]{64}")


def sweep_own_plugin_copies(root: str, now: float | None = None) -> list[str]:
    """Removes the own plugin's copies under ``root`` no segment used for ``COPY_KEPT_S``
    (``own_plugin_copy`` touches the one it hands out), and ``.tmp-``/``.sweep-`` folders
    a worker left half-made, after ``TMP_KEPT_S``. Each is moved aside first, then removed."""
    at = time.time() if now is None else now
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return []
    removed: list[str] = []
    for name in names:
        if _COPY_NAME.fullmatch(name):
            keep = COPY_KEPT_S
        elif name.startswith((".tmp-", ".sweep-")):
            keep = TMP_KEPT_S
        else:
            continue
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path) or at - os.lstat(path).st_mtime < keep:
                continue
            aside = os.path.join(root, f".sweep-{uuid.uuid4().hex}")
            os.rename(path, aside)
        except OSError:
            continue
        shutil.rmtree(aside, ignore_errors=True)
        removed.append(name)
    return removed


def own_plugin_copy(plugin_dir: str, off: frozenset[str], root: str) -> str:
    """The own plugin as a segment loads it (#4757): ``plugin_dir`` when nothing in it is off;
    else a copy (links followed, ``symlinks=False``) without ``skills/<name>/`` for each
    ``skill:<name>`` and ``agents/<file>.md`` for each ``subagent:<name>`` (by file stem or
    frontmatter ``name``), at ``root/<hash hex>/<plugin folder name>``: Claude Code names a
    plugin with no manifest after its folder, so the copy keeps the name its skills are
    called by (``scadbuddy:print``). A copy already there is kept only once it hashes to its
    name; one that does not is replaced. Raises OSError or PackageContentError when no copy
    can be made: the caller then loads none."""
    skills = _off_names(off, "skill")
    agents = _off_names(off, "subagent")
    held_skills = [d for n, d in _own_skills(plugin_dir).items() if n in skills]
    held_agents = [
        f
        for stem, f in _agent_files(os.path.join(plugin_dir, "agents")).items()
        if stem in agents or frontmatter_name(f) in agents
    ]
    if not held_skills and not held_agents:
        return plugin_dir
    base = os.path.basename(os.path.normpath(plugin_dir))
    os.makedirs(root, exist_ok=True)
    sweep_own_plugin_copies(root)
    tmp = os.path.join(root, f".tmp-{uuid.uuid4().hex}")
    try:
        made = os.path.join(tmp, base)
        shutil.copytree(plugin_dir, made, symlinks=False)
        for d in held_skills:
            shutil.rmtree(os.path.join(made, "skills", d.name))
        for f in held_agents:
            os.remove(os.path.join(made, "agents", f.name))
        digest = hash_tree(made)
        hex_ = digest.removeprefix("sha256:")
        target = os.path.join(root, hex_)
        kept = os.path.join(target, base)

        def matches() -> bool:
            try:
                return hash_tree(kept) == digest
            except OSError, PackageContentError:
                return False

        if matches():
            os.utime(target)  # used: kept by the sweep
            return kept
        if os.path.lexists(target):
            # Not what its name says: moved aside whole, then replaced.
            aside = os.path.join(root, f".sweep-{uuid.uuid4().hex}")
            os.rename(target, aside)
            shutil.rmtree(aside, ignore_errors=True)
        try:
            os.rename(tmp, target)
        except OSError:
            # Another segment placed it first: kept only once it hashes to its name.
            if not matches():
                raise
        return kept
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def call_plugins_of(
    own: access.CallPlugin | None, loaded: Sequence[TurnPackage]
) -> list[access.CallPlugin]:
    """The segment's plugins as the call rule sees them, in the classic turn's order
    (turnRunner.ts ``callPlugins``): each loaded package, then the own plugin while loaded."""
    return [
        *(
            access.CallPlugin(
                p.name,
                f"package:{p.name}",
                tuple(p.items),
                {k: tuple(v) for k, v in p.aliases.items()},
            )
            for p in loaded
        ),
        *([own] if own is not None else []),
    ]


def call_builtins_of(loaded: Sequence[TurnPackage]) -> dict[str, list[str]]:
    """Skill, Agent and Task as the loaded allowed packages name them (the agent's
    ``named_builtins``, or ``builtin_tools`` from an older listing): each, the packages."""
    out: dict[str, list[str]] = {}
    for p in loaded:
        if not p.allowed:
            continue
        for named in sorted(set(p.named_builtins) | set(p.builtin_tools)):
            if named in access.CALL_TOOLS:
                out.setdefault(named, []).append(p.name)
    return out


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


#: The PreToolUse matcher's timeout: past the segment's own hour, so the hook's deadline on
#: the gate (``PARK_DEADLINE_S``) and the activity's timeout both come first.
HOOK_TIMEOUT_S = SEGMENT_TIMEOUT.total_seconds() + 10 * 60


class CallAccess:
    """Skill, Agent and Task calls judged by access as each is made (#4757, spec §4.4): the
    segment's plugins' ``ai_access`` rows read per call, so an item switched off mid-segment
    is refused from its next call, in the words a classic turn uses (access.py
    ``off_item_reason``). A read that fails refuses the call (fail closed). Asked by
    ``SegmentCalls``'s PreToolUse hook, so the panel sees the refused call end. The plugin
    keeps a hook's decision for these tools: its ``_guard_hooks`` drops one only for its own
    server's calls and ``tool_activities``."""

    def __init__(
        self,
        connect: Connect,
        plugins: list[access.CallPlugin],
        builtins: Mapping[str, Sequence[str]] | None = None,
        park: ParkCall | None = None,
        release: ReleaseCall | None = None,
    ) -> None:
        self._connect = connect
        # Ends a call's gate entry when its segment ends first (re-review R5).
        self._release = release
        # Calls asked of the gate and not yet answered.
        self._waiting: set[str] = set()
        self._plugins = plugins
        # An outward one parks at the workflow's gate, as an engine-parked built-in does.
        self._park = park
        # Skill, Agent or Task as an allowed package's built-in, with the packages naming it
        # (Task 5 review M4): each call also takes that built-in's access (access.py
        # ``builtin_access``), as a classic turn's seam reads it (agent access/live.ts).
        self._builtins = dict(builtins or {})

    async def check(self, data: Any) -> dict[str, Any]:
        tool = str(data.get("tool_name") or "")
        packages = list(self._builtins.get(tool, ()))
        if tool not in access.CALL_TOOLS or not (self._plugins or packages):
            return {}
        sources = list(
            dict.fromkeys([*(p.source for p in self._plugins), *(f"package:{n}" for n in packages)])
        )
        try:
            async with self._connect() as conn:
                rows = await access.read_rows(conn, sources)
        except Exception as err:
            logger.warning("tool access could not be read for %s: %s", tool, type(err).__name__)
            return _deny(f"{tool} is refused: its Settings → Tools & plugins entry cannot be read")
        off = access.off_call_item(tool, data.get("tool_input"), self._plugins, rows)
        if off is not None:
            return _deny(access.off_item_reason(off))
        if not packages:
            return {}
        tier = access.builtin_access(tool, packages, rows)
        if tier == "off":
            return _deny(access.TOOL_OFF.format(tool))
        if tier in ("read", "write"):
            return {}
        # Outward, as a classic turn's seam parks it: asked of a person at the gate, and run
        # only once approved. With no gate to ask, refused (fail closed).
        call = str(data.get("tool_use_id") or "")
        if self._park is None or not call:
            return _deny(f"{tool} needs approval, and it could not be asked for")
        self._waiting.add(call)
        try:
            # The hook's own deadline (re-review R2): a gate that never answers refuses it.
            answer = await asyncio.wait_for(
                self._park(call, tool, data.get("tool_input") or {}), PARK_DEADLINE_S
            )
        except TimeoutError:
            logger.warning("%s was not decided by the hook's deadline", tool)
            await self._release_one(call)
            return _deny(f"{tool} needs approval, and it was not decided in time")
        except Exception as err:
            logger.warning("%s could not be parked: %s", tool, type(err).__name__)
            await self._release_one(call)
            return _deny(f"{tool} needs approval, and it could not be asked for")
        self._waiting.discard(call)
        if answer.get("go") is True:
            return {}
        return _deny(str(answer.get("reason") or f"{tool} was not approved"))

    async def _release_one(self, call: str) -> None:
        if call not in self._waiting:
            return
        self._waiting.discard(call)
        if self._release is None:
            return
        try:
            await self._release(call)
        except Exception as err:
            # Its timer still ends it; the call itself never runs (its hook denied it or ended).
            logger.warning("a waiting call could not be released: %s", type(err).__name__)

    async def release_waiting(self) -> None:
        """Releases every call still waiting at the gate: its segment has ended (R5)."""
        for call in list(self._waiting):
            await self._release_one(call)


class ScadBuddyRunner:
    def __init__(
        self,
        connect: Connect,
        kek: Kek,
        *,
        cwd: str,
        plugin_dir: str | None = None,
        package_root: str | None = None,
        subagent_url: str | None = None,
        agents_dir: str | None = None,
        make_runner: MakeRunner = ClaudeAgentSdkRunner,
        own_plugin_root: str = OWN_PLUGIN_ROOT,
    ) -> None:
        self._connect = connect
        self._kek = kek
        self._cwd = cwd
        self._plugin_dir = plugin_dir
        self._package_root = package_root
        self._subagent_url = subagent_url
        # The refused stored URL last warned of: warned once, not on every segment.
        self._refused_url: str | None = None
        # Read once, at the first segment that gives subagents: the files are the image's.
        self._agents_dir = agents_dir
        self._agents: dict[str, AgentDefinition] | None = None
        self._make = make_runner
        self._own_plugin_root = own_plugin_root

    def __repr__(self) -> str:
        return f"ScadBuddyRunner(kek={self._kek.id})"

    async def _settings(self, session: str) -> TurnSettings:
        """What Settings says about this segment, read as it starts, as a classic turn reads it,
        and what the chat itself says over it (a Workflow agent's, ai_sessions.agent_*)."""
        async with self._connect() as conn:
            cur = await conn.execute(
                "SELECT key, value FROM ai_settings WHERE key = ANY(%s)",
                ([LONG_EDGE_KEY, MODEL_KEY],),
            )
            # psycopg hands jsonb back decoded: a stored string is already a str.
            values = dict(await cur.fetchall())
            # The own plugin's switch (#2171): its ai_access '*' row, on unless off.
            plugin_rows = await access.read_rows(conn, [access.OWN_PLUGIN_SOURCE])
            cur = await conn.execute(
                "SELECT agent_model, agent_effort, agent_type FROM ai_sessions WHERE id = %s",
                (session,),
            )
            own = await cur.fetchone() or (None, None, None)
        edge = values.get(LONG_EDGE_KEY)
        if isinstance(edge, bool) or not isinstance(edge, int):
            edge = DEFAULT_LONG_EDGE
        model = own[0] or values.get(MODEL_KEY)
        return TurnSettings(
            long_edge=min(max(edge, MIN_LONG_EDGE), MAX_LONG_EDGE),
            model=model if isinstance(model, str) and model else None,
            own_plugin=access.source_enabled(access.OWN_PLUGIN_SOURCE, plugin_rows),
            own_off=frozenset(
                r.item for r in plugin_rows if r.access == "off" and r.item != access.SOURCE_ITEM
            ),
            effort=own[1],
            agent_type=own[2],
        )

    async def _subagent_tools(
        self, session: str, extra: dict[str, Any], off: frozenset[str] = frozenset()
    ) -> None:
        """ScadBuddy's subagents (#2320, subagents.py), each with the agent's subagent
        server inline (the URL Settings names, else this worker's) and a token for this
        session's running turn and owner; none switched off (#4757: ``off``, by file stem or
        frontmatter ``name``)."""
        if self._subagent_url is None or self._agents_dir is None:
            return
        async with self._connect() as conn:
            cur = await conn.execute(
                "SELECT owner_kind, owner_id, durable_turn_id FROM ai_sessions"
                " WHERE id = %s AND mode = 'durable'",
                (session,),
            )
            row = await cur.fetchone()
            cur = await conn.execute(
                "SELECT value FROM ai_settings WHERE key = %s", (SUBAGENTS_KEY,)
            )
            setting = await cur.fetchone()
        if row is None or row[2] is None:
            return
        stored = setting[0] if setting else None
        url = stored.get("url") if isinstance(stored, dict) else None
        if not _http_url(url):
            # The agent's route stores only an http(s) URL; a row written any other way
            # does not reach the subagents' definitions (#2883). Said, so an operator
            # sees why the setting has no effect; the URL itself is never logged (#3054).
            if isinstance(url, str) and url and url != self._refused_url:
                self._refused_url = url
                logger.warning(
                    "the stored subagent server URL is not an http(s) URL with a host (%s);"
                    " using the worker's own",
                    _url_problem(url),
                )
            url = self._subagent_url
        else:
            self._refused_url = None
        token = subagents.token_for(self._kek, session, str(row[2]), row[0], row[1])
        extra["agents"] = subagents.with_server(self._defined(True, off), url, token)
        # Pre-approved for the subagents that have the server; the main agent has none.
        extra["allowed_tools"] = [*extra.get("allowed_tools", []), f"mcp__{subagents.SERVER}"]

    def _packages(self, workflow_id: str, context: SegmentContext) -> list[str]:
        """The turn's plugin packages (#2320) that hash to their pins on the shared volume,
        as a classic turn loads only a cached copy that does (plugins/packages/install.ts).
        One that does not is left out, as the agent leaves out one it cannot load."""
        return [d for _, d in self._loaded_packages(workflow_id, context)]

    def _defined(self, loaded: bool, off: frozenset[str]) -> dict[str, AgentDefinition]:
        """ScadBuddy's subagent definitions this segment may run, by frontmatter ``name``:
        none while the own plugin is not loaded (switched off, or no copy), and none switched
        off (``off``, by file stem or frontmatter ``name``, #4757)."""
        if not loaded or self._agents_dir is None:
            return {}
        if self._agents is None:
            self._agents = subagents.load_agents(self._agents_dir)
        names = _off_names(off, "subagent")
        stems = {frontmatter_name(f): stem for stem, f in _agent_files(self._agents_dir).items()}
        return {
            name: agent
            for name, agent in self._agents.items()
            if name not in names and stems.get(name) not in names
        }

    def _loaded_packages(
        self, workflow_id: str, context: SegmentContext
    ) -> list[tuple[TurnPackage, str]]:
        dirs: list[tuple[TurnPackage, str]] = []
        for package in context.packages:
            shared = _SHARED_NAME.fullmatch(package.dir)
            named = shared is not None and shared.group("name") in (None, package.name)
            if self._package_root is None or not named:
                logger.warning("segment of %s: package %s not loaded", workflow_id, package.name)
                continue
            path = os.path.join(self._package_root, package.dir)
            try:
                matches = hash_tree(path) == package.content_hash
            # PEP 758 (3.14): both types, unparenthesised; not Python 2's `except E, name`.
            except OSError, PackageContentError:
                matches = False
            if not matches:
                logger.warning(
                    "segment of %s: package %s does not hash to its pin", workflow_id, package.name
                )
                continue
            if declares_remote_servers(path):
                # Claude Code would reach it itself, past the forwarder's egress check
                # (harness/run.ts refuses the same); the agent shares a copy without them.
                logger.warning(
                    "segment of %s: package %s declares an http MCP server",
                    workflow_id,
                    package.name,
                )
                continue
            dirs.append((package, path))
        return dirs

    def _own_plugin(self, workflow_id: str, settings: TurnSettings) -> str | None:
        """The own plugin's folder for this segment, or None: not configured, switched off,
        or something in it is off and no copy without it could be made (#4757; never loaded
        with an off item in it)."""
        if not self._plugin_dir or not settings.own_plugin:
            return None
        try:
            return own_plugin_copy(self._plugin_dir, settings.own_off, self._own_plugin_root)
        except (OSError, PackageContentError) as err:
            logger.warning(
                "segment of %s: the own plugin is not loaded: no copy without what is off (%s)",
                workflow_id,
                type(err).__name__,
            )
            # Said to the session as a classic turn says it (turnRunner.ts plugin_unavailable).
            emit(
                {
                    "type": "plugin_unavailable",
                    "message": "ScadBuddy's own plugin was not loaded: no copy without what is"
                    f" switched off could be made ({type(err).__name__})",
                }
            )
            return None

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

    async def _on_credentials(
        self,
        session: str,
        workflow_id: str,
        inp: SegmentInput,
        attempt: int,
        credentials: list[Credential],
        left: float,
        extra: dict[str, Any],
        settings: TurnSettings,
        calls: SegmentCalls | None = None,
    ) -> SegmentOutput:
        """Runs the segment on each usable credential in priority order, moving on only
        when the failure was the credential's (``fallback.credentials_fault``), as a classic
        turn does; each credential at most once per attempt. What a failed one spent is
        counted, and the next runs on what is left."""
        for i, credential in enumerate(credentials):
            runner = self._make(
                cwd=self._cwd,
                env={**worker_env_blanked(), **ENGINE_ENV, **credential_env(credential)},
                max_budget_usd=left,
                extra_options=extra,
                **({"model": settings.model} if settings.model else {}),
            )
            # A credential Claude Code refuses answers as the assistant ("You've hit your
            # session limit") before its ResultError, and the plugin publishes that text
            # (#2945): held until the attempt is known not to be refused, while another
            # credential could still take the segment over.
            # The last credential's is held too: refused, its notice is published marked
            # (#3703) rather than dropped, since nothing takes the segment over.
            publish = _EMIT.get()
            held = HeldText(publish) if publish is not None else None
            token = _EMIT.set(held) if held is not None else None
            try:
                out = await runner.run(inp, attempt)
                if out.is_error and calls is not None and publish is not None:
                    # An attempt that answers with an error has ended too: so have its calls.
                    # Once only: `_report` below never raises, and a later close finds none open.
                    calls.close_open(publish)
                # The endpoint answered: the key works, whatever became of the segment.
                await self._report(session, credential, "used")
                if out.is_error and out.error and _BUDGET_REACHED.match(out.error):
                    # The plugin answers a segment that reached its cap with the error's
                    # text only: no subtype, and no cost. Claude Code stops only once the
                    # cost reached the cap, so at least what was left was spent; charged
                    # as 0, the next send would find the budget unspent and run again.
                    # BUDGET_EXHAUSTED ends the turn as the budget's, not as a failure,
                    # so the panel offers the raise.
                    return replace(out, error=BUDGET_EXHAUSTED, cost_usd=max(out.cost_usd, left))
                return out
            except Exception as err:
                if calls is not None and publish is not None:
                    # Straight to the feed, past `held`: its text is the attempt's to keep or drop.
                    calls.close_open(publish)
                # Never the text: like the codec's sealed failures, it is the session's content.
                logger.warning(
                    "segment of %s failed (attempt %d, %s): %s, %d bytes of text",
                    workflow_id,
                    attempt,
                    credential.label,
                    type(err).__name__,
                    len(error_text(err).encode()),
                )
                await self._report_failure(session, credential, err)
                if _BUDGET_REACHED.match(str(err)):
                    # Ahead of credentials_fault on purpose (#3569): a cap is never the
                    # credential's fault, so a capped segment must not go on to the next.
                    # The cap raised rather than returned: today the plugin returns it,
                    # but a raised one is the same cap, charged and ended the same way
                    # as the returned one above, never a failure (#3356).
                    return SegmentOutput(
                        session_id=inp.session_id,
                        is_error=True,
                        error=BUDGET_EXHAUSTED,
                        cost_usd=max(_cost(err), left),
                    )
                if credentials_fault(err) and i + 1 < len(credentials):
                    if held is not None:
                        # Mostly the refusal notice. A fault mid-segment also drops real
                        # text since the last tool call, which the rerun on the next
                        # credential writes again: dropped, never flushed.
                        held.drop()
                    cost = _cost(err)
                    if cost > 0:
                        left -= cost
                        async with self._connect() as conn:
                            await conn.execute(
                                "UPDATE ai_sessions SET cost_usd = cost_usd + %s,"
                                " updated_at = now() WHERE id = %s",
                                (cost, session),
                            )
                    if left <= 0:
                        # Spent by the refused attempt: no other credential runs past it.
                        return SegmentOutput(
                            session_id=inp.session_id, is_error=True, error=BUDGET_EXHAUSTED
                        )
                    logger.warning(
                        "segment of %s continues on %s", workflow_id, credentials[i + 1].label
                    )
                    continue
                if credentials_fault(err) and held is not None:
                    held.mark_notice()
                # Every credential refused it (#3705): no retry could change that, and each
                # one ran the refused keys again and published the notice again. Final, as a
                # request too large is; a transient failure (5xx, network) is still retried.
                if not final_failure(err) and not credentials_fault(err):
                    raise bounded_failure(err) from None
                # Ends the turn with the reason, as the plugin's own final errors do.
                return SegmentOutput(
                    session_id=inp.session_id,
                    is_error=True,
                    error=bounded(str(err)),
                    cost_usd=_cost(err),
                )
            except BaseException:
                # Cancelled (an interrupt, a worker shutting down): its calls end too.
                if calls is not None and publish is not None:
                    calls.close_open(publish)
                raise
            finally:
                if token is not None:
                    _EMIT.reset(token)
                if held is not None:
                    # Not a leak: a failure that is not the credential's ends the turn on
                    # this credential, so what it said is the turn's and is published; the
                    # last credential's refusal notice goes out marked (mark_notice).
                    held.flush()
        raise AssertionError("unreachable: the last credential returns or raises")

    async def _report_failure(self, session: str, credential: Credential, err: Exception) -> None:
        """What a failed attempt says of its key, as a classic turn reports it
        (fallback.ts): a rate limit cools the key down, a refusal or an endpoint failure
        is its last error, and any other answer from the endpoint (a capped or refused
        request) means the key works. A failure that never reached the endpoint (no
        ``ResultError``) says nothing of the key."""
        if not isinstance(err, ResultError):
            return
        reason = err.result or str(err)
        verdict = classify(err)
        if verdict == "rate_limited":
            await self._report(session, credential, "cooling_down", reason)
        elif verdict in ("permanent", "transient"):
            # A refusal is not disabled here: a classic turn disables a key only once its
            # probe confirms the refusal, and a durable segment runs no probe.
            await self._report(session, credential, "error", reason)
        else:
            await self._report(session, credential, "used")

    async def _report(
        self, session: str, credential: Credential, health: Health, reason: str = ""
    ) -> None:
        """Records the key's health (#2320), so Settings and the next turn see what this
        segment met; never fails the segment, as CredentialPool.report never fails a turn."""
        now = datetime.now(UTC)
        until = (
            cooldown_until(now, rate_limit_reset(reason, now)) if health == "cooling_down" else None
        )
        try:
            async with self._connect() as conn:
                change = await record_health(conn, credential, health, reason=reason, until=until)
                await audit_health(
                    conn, credential, change, reason=reason, until=until, session_id=session
                )
        except Exception as err:
            logger.warning(
                "credentials: could not record %s for %s: %s",
                health,
                credential.label,
                type(err).__name__,
            )

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
        if context.refusals or context.replaced:
            # A call refused at the gate: the model is told why after the plugin's fixed
            # rejection, as a classic turn tells it (#2392), or, refused by the agent before
            # it parked, only why (#4442). The decision stays the plugin's.
            inp = replace(
                inp, injected=with_refusals(inp.injected, context.refusals, context.replaced)
            )
        if context.disabled_tools:
            # Settings → Tools & plugins turned them off (#2088): Claude is not offered them.
            off = set(context.disabled_tools)
            inp = replace(inp, tools=[t for t in inp.tools if t.name not in off])
        settings = await self._settings(session)
        images = await self._images(session, context)
        problem = image_problem(images, settings.long_edge) if images else None
        if problem is not None:
            return SegmentOutput(session_id=inp.session_id, is_error=True, error=problem)
        # Every segment: a tool result of any segment may name images (#2320).
        extra["mcp_servers"] = {
            IMAGES_SERVER: images_server(images, tool_images_loader(self._connect, session))
        }
        extra["allowed_tools"] = [VIEW_TOOL_IMAGES_TOOL, *([VIEW_IMAGES_TOOL] if images else [])]
        own_dir = self._own_plugin(workflow_id, settings)
        if own_dir is not None:
            # The own plugin's subagents, as a classic turn has them only with the plugin.
            await self._subagent_tools(session, extra, settings.own_off)
        if settings.effort:
            extra["effort"] = settings.effort
        if settings.agent_type and settings.agent_type in self._defined(
            own_dir is not None, settings.own_off
        ):
            # The chat's turns run as that subagent (Claude Code's --agent), as a classic turn's do.
            extra["extra_args"] = {"agent": settings.agent_type}
        plugins = [{"type": "local", "path": own_dir}] if own_dir is not None else []
        loaded = self._loaded_packages(workflow_id, context)
        plugins += [{"type": "local", "path": d} for _, d in loaded]
        if plugins:
            extra["plugins"] = plugins
        # The loaded plugins as the call rule sees them (#4757): each Skill or Agent call is
        # refused while the item it names is off, read as it is made.
        packages = [p for p, _ in loaded]
        call_plugins = call_plugins_of(
            own_call_plugin(self._plugin_dir, self._agents_dir)
            if own_dir is not None and self._plugin_dir is not None
            else None,
            packages,
        )
        park, release = segment_gate(
            activity.client(), workflow_id, activity.info().workflow_run_id or ""
        )
        gate = CallAccess(self._connect, call_plugins, call_builtins_of(packages), park, release)
        calls = SegmentCalls(list(inp.tool_activities), gate.check)
        extra["hooks"] = merged_hooks(extra.get("hooks"), calls.hooks())
        try:
            out = await self._on_credentials(
                session,
                workflow_id,
                inp,
                attempt,
                credentials,
                budget - spent,
                extra,
                settings,
                calls,
            )
        finally:
            # A call still waiting has lost its hook with the segment: end its entry (R5).
            await asyncio.shield(gate.release_waiting())
        if out.cost_usd > 0 or not out.is_error:
            async with self._connect() as conn:
                await conn.execute(
                    "UPDATE ai_sessions SET cost_usd = cost_usd + %s, turns = turns + 1,"
                    " updated_at = now() WHERE id = %s",
                    (out.cost_usd, session),
                )
        return out
