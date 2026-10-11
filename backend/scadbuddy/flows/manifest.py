"""The agent's tools a flow may call with `tool(name, args)` (#1057, plan
2026-10-09-durable-phase-6-flows.md Task D2).

``agent/dist/tools.json`` (the agent's build writes it from ALL_TOOLS, as
`agent-durable` reads it) is read once, when the `projects` worker starts, from
``SCADBUDDY_FLOW_TOOLS_JSON``. The workflow sandbox imports this module through, so
every ProjectWorkflow on the worker sees the manifest it loaded.
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
