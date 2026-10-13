"""The agent's tools a flow may call, at their tiers now (#2704), as the API reads them.

The agent resolves a tool's tier for its own turns with `effectiveTool`
(agent/src/tools/registry.ts): the tool's definition, Settings' raised tiers and its
disabled tools. The API asks the agent sidecar in its own pod for that same answer
(`GET /internal/flows/tools` on the pod's loopback, agent/src/routes/flowTools.ts) when it
checks a grant's tools (a run's start, a grant's approval) and lists the editor's, and a
run starts with it as its tiers (`FlowStart.tool_tiers`), which each call's own answer
from the agent (`flow_tool_tier`, workflows/flow_tools.py) may only raise.

Ruling (#2704): the loopback route, not an activity. The API needs the answer inside a
request, with no workflow to run an activity from; the agent is a sidecar of every API
pod (clusters `applications/scadbuddy/scadbuddy.yaml`), so loopback reaches the agent
beside it. The `projects` worker has no agent beside it, so a run asks through its
activity instead. When the agent does not answer, every caller refuses: there is no
file or copy to fall back to.
"""

from __future__ import annotations

import httpx

#: Lowest first (agent/src/harness/permissions.ts RISK_TIERS).
RISK_TIERS = ("read", "write", "outward")
FLOW_TOOLS_PATH = "/internal/flows/tools"
_TIMEOUT = httpx.Timeout(5.0)


class AgentToolsUnavailableError(Exception):
    """The agent did not say what its tools are."""


class AgentTools:
    """The agent sidecar's answer, read on each call: Settings may change it any time."""

    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._url = base_url.rstrip("/") + FLOW_TOOLS_PATH
        self._transport = transport

    async def tool_tiers(self) -> dict[str, str]:
        """Each tool a flow may call and is not disabled, at its effective tier."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                response = await client.get(self._url)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as err:
            raise AgentToolsUnavailableError(f"the agent did not answer: {err}") from None
        tools = body.get("tools") if isinstance(body, dict) else None
        if not isinstance(tools, list):
            raise AgentToolsUnavailableError("the agent's answer lists no tools")
        tiers: dict[str, str] = {}
        for entry in tools:
            if not isinstance(entry, dict):
                raise AgentToolsUnavailableError("the agent's answer is not a tool list")
            name, tier, disabled = entry.get("name"), entry.get("tier"), entry.get("disabled")
            if (
                not isinstance(name, str)
                or tier not in RISK_TIERS
                or not isinstance(disabled, bool)
            ):
                raise AgentToolsUnavailableError("the agent's answer is not a tool list")
            if not disabled:
                tiers[name] = tier
        return tiers
