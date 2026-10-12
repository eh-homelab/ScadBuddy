"""ScadBuddy's tools for a durable segment's subagents (#2320).

The Temporal plugin denies every durable tool to a subagent (``MAIN_AGENT_ONLY``): a
subagent cannot pause the segment, so it cannot run an activity or wait for a human. A
classic turn's subagents have ScadBuddy's tools through the in-process ``scadbuddy``
server, so each segment gives its Claude Code the agent's loopback MCP server under that
name (agent/src/routes/subagentMcp.ts): the subagents' tool names,
``mcp__scadbuddy__<tool>``, are a classic turn's, and the agent decides every call from
the session as it is then (owner, turn, tiers, Settings), refusing an outward one.

The server's token is minted here for the segment, from a key both sidecars derive from
the KEK (agent/src/temporal/subagentToken.ts, the same format). It goes only into the
engine's MCP config, never into a Temporal payload, and it names the session's turn and
owner: the agent refuses it once the turn is over or the session changed hands.

The main agent calls ScadBuddy's tools as durable tools (``mcp__durable__<tool>``), so
the ``mcp__scadbuddy__`` copies are refused to it by a PreToolUse hook: its calls must go
through the workflow's gate.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, cast

from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import HookContext, HookInput, SyncHookJSONOutput

from scadbuddy_durable.secrets import Kek

SERVER = "scadbuddy"
PREFIX = f"mcp__{SERVER}__"
# agent/src/main.ts PORT and routes/subagentMcp.ts SUBAGENT_MCP_PATH.
DEFAULT_URL = "http://127.0.0.1:8081/api/v1/ai/durable/subagent-mcp"
# A segment's activity runs at most 10 minutes (the plugin's segment_timeout).
TOKEN_TTL_S = 15 * 60
_INFO = b"scadbuddy:durable:subagent_mcp:v1"

MAIN_AGENT_USES_DURABLE = (
    "Not an error: this copy of the tool is for subagents. Call the same tool as "
    "mcp__durable__<tool> instead, which runs it through ScadBuddy's approvals."
)


def subagent_key(kek: Kek) -> bytes:
    """HKDF-SHA256 (RFC 5869) with an empty salt, as Node's ``hkdfSync`` derives it."""
    prk = hmac.new(b"\0" * 32, kek.key, hashlib.sha256).digest()
    return hmac.new(prk, _INFO + b"\x01", hashlib.sha256).digest()


def owner_digest(kind: str, owner_id: str) -> str:
    return hashlib.sha256(f"{kind}:{owner_id}".encode()).hexdigest()[:32]


def mint(key: bytes, session: str, turn: str, kind: str, owner_id: str, expires_at: int) -> str:
    body = f"{session}.{turn}.{owner_digest(kind, owner_id)}.{expires_at}"
    return f"sbsub.{body}.{hmac.new(key, body.encode(), hashlib.sha256).hexdigest()}"


def server_config(url: str, token: str) -> dict[str, Any]:
    return {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}


async def _main_agent_only_durable(
    hook_input: HookInput, _tool_use_id: str | None, _context: HookContext
) -> SyncHookJSONOutput:
    event = cast(dict[str, Any], hook_input)
    if event.get("agent_id") or not str(event.get("tool_name", "")).startswith(PREFIX):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": MAIN_AGENT_USES_DURABLE,
        }
    }


def hooks() -> dict[str, list[HookMatcher]]:
    """Keeps the main agent to its durable tools: the loopback copies are the subagents'."""
    return {"PreToolUse": [HookMatcher(matcher=f"^{PREFIX}", hooks=[_main_agent_only_durable])]}


def token_for(kek: Kek, session: str, turn: str, kind: str, owner_id: str) -> str:
    return mint(subagent_key(kek), session, turn, kind, owner_id, int(time.time()) + TOKEN_TTL_S)
