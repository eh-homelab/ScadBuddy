"""ScadBuddy's tools for a durable segment's subagents (#2320).

The Temporal plugin denies every durable tool to a subagent (``MAIN_AGENT_ONLY``): a
subagent cannot pause the segment, so it cannot run an activity or wait for a human. A
classic turn's subagents have ScadBuddy's tools through the in-process ``scadbuddy``
server, so each segment gives its subagents the agent's loopback MCP server under that
name (agent/src/routes/subagentMcp.ts): the subagents' tool names,
``mcp__scadbuddy__<tool>``, are a classic turn's, and the agent decides every call from
the session as it is then (owner, turn, tiers, Settings), refusing an outward one.

The server is the subagents' own (#2562): each of ScadBuddy's subagent definitions
(plugins/scadbuddy/agents/*.md, read from ``SCADBUDDY_DURABLE_AGENTS_DIR``) is given
to Claude Code with the server inline (``AgentDefinition.mcpServers``), which Claude Code
connects for that subagent only, so the main agent is never offered a second copy of
every tool. A plugin's own subagents cannot carry an MCP server
(https://code.claude.com/docs/en/sub-agents), so the image keeps the definitions out of
the plugin directory and passes them here instead.

The server's token is minted here for the segment, from a key both sidecars derive from
the KEK (agent/src/temporal/subagentToken.ts, the same format). It goes only into the
engine's options, never into a Temporal payload, and it names the session's turn and
owner: the agent refuses it once the turn is over or the session changed hands.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from claude_agent_sdk import AgentDefinition

from scadbuddy_durable.secrets import Kek

SERVER = "scadbuddy"
PREFIX = f"mcp__{SERVER}__"
# agent/src/main.ts PORT and routes/subagentMcp.ts SUBAGENT_MCP_PATH (tests/test_subagents.py
# reads both): outside /api/v1/ai/, the prefix the ingress routes to the agent.
DEFAULT_URL = "http://127.0.0.1:8081/internal/durable/subagent-mcp"
# A segment's activity runs at most 10 minutes (the plugin's segment_timeout).
TOKEN_TTL_S = 15 * 60
_INFO = b"scadbuddy:durable:subagent_mcp:v1"


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


def token_for(kek: Kek, session: str, turn: str, kind: str, owner_id: str) -> str:
    return mint(subagent_key(kek), session, turn, kind, owner_id, int(time.time()) + TOKEN_TTL_S)


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """A subagent file's frontmatter and body, in the forms our own files use: ``key:
    value`` lines, and a ``key:`` followed by ``  - item`` lines."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("no frontmatter")
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    fields: dict[str, Any] = {}
    key: str | None = None
    for line in lines[1:end]:
        if line.startswith("  - ") and key is not None:
            fields.setdefault(key, []).append(line[4:].strip())
        elif ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            key = key.strip()
            if value.strip():
                fields[key] = value.strip()
        elif line.strip():
            raise ValueError(f"unsupported frontmatter line: {line!r}")
    return fields, "\n".join(lines[end + 1 :]).strip()


def _names(value: Any) -> list[str] | None:
    if value is None:
        return None
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(i).strip() for i in items if str(i).strip()]


def load_agents(agents_dir: str) -> dict[str, AgentDefinition]:
    """ScadBuddy's subagent definitions, as the plugin's ``agents/*.md`` declare them."""
    agents: dict[str, AgentDefinition] = {}
    for path in sorted(Path(agents_dir).glob("*.md")):
        fields, prompt = _frontmatter(path.read_text())
        turns = fields.get("maxTurns")
        agents[str(fields["name"])] = AgentDefinition(
            description=str(fields["description"]),
            prompt=prompt,
            tools=_names(fields.get("tools")),
            disallowedTools=_names(fields.get("disallowedTools")),
            model=fields.get("model"),
            skills=_names(fields.get("skills")),
            maxTurns=int(turns) if turns is not None else None,
        )
    return agents


def with_server(
    agents: dict[str, AgentDefinition], url: str, token: str
) -> dict[str, AgentDefinition]:
    """The definitions, each with the loopback server inline, for that subagent only."""
    server: dict[str, Any] = {SERVER: server_config(url, token)}
    return {name: replace(agent, mcpServers=[server]) for name, agent in agents.items()}
