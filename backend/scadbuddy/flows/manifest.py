"""The agent's tools a flow may call with `tool(name, args)` (#1057, plan
2026-10-09-durable-phase-6-flows.md Task D2).

``agent/dist/tools.json`` (the agent's build writes it from ALL_TOOLS, as
`agent-durable` reads it) is read once, when the API starts, from
``SCADBUDDY_FLOW_TOOLS_JSON``. A run takes a copy of it at its start (`FlowStart.tool_tiers`),
so what `tool(...)` does is in the run's history, never in the worker's state: a run
replays the same on any worker, whatever manifest that worker's image carries.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: A tool's name to its tier (`read`, `write` or `outward`).
_TIERS: dict[str, str] = {}


def parse_manifest(data: Any) -> dict[str, str]:
    if not isinstance(data, list):
        raise ValueError("the tool manifest is not a list")
    return {str(entry["name"]): str(entry["tier"]) for entry in data}


def load_manifest(path: str | Path) -> dict[str, str]:
    """Read the manifest and make it every flow's on this worker."""
    use_manifest(parse_manifest(json.loads(Path(path).read_text(encoding="utf-8"))))
    return dict(_TIERS)


def use_manifest(tiers: Mapping[str, str]) -> None:
    """The manifest a worker in this process serves (tests set their own)."""
    _TIERS.clear()
    _TIERS.update(tiers)


def tier_of(name: str) -> str | None:
    """The tool's tier, or None for a name the manifest does not list."""
    return _TIERS.get(name)


def tiers() -> dict[str, str]:
    """A copy of the manifest, for a run's start."""
    return dict(_TIERS)


#: Lowest first: an override only raises (agent/src/harness/permissions.ts `raiseTier`).
RISK_TIERS = ("read", "write", "outward")
#: Where Settings keeps the raised tiers of ScadBuddy's own tools (agent
#: `plugins/builtInTools.ts`, `builtin_tools.<set>` in `ai_settings`).
OVERRIDES_SETTING = "builtin_tools.scadbuddy"


def raise_tiers(tiers: Mapping[str, str], overrides: Any) -> dict[str, str]:
    """`tiers` with Settings' stored overrides applied as the agent applies them
    (`effectiveTool`): a known tool's tier only goes up; anything else is ignored."""
    out = dict(tiers)
    raised = overrides.get("tool_tiers") if isinstance(overrides, dict) else None
    if not isinstance(raised, dict):
        return out
    for name, tier in raised.items():
        own = out.get(name)
        if (
            own in RISK_TIERS
            and tier in RISK_TIERS
            and RISK_TIERS.index(tier) > RISK_TIERS.index(own)
        ):
            out[name] = tier
    return out
