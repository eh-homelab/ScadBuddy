"""Every ALL_TOOLS entry as an activity_as_tool stub (spec §6.3). The TypeScript
agent-tools worker serves each activity by name; _remote never runs here."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.claude_agent_sdk import DurableTool, activity_as_tool

TOOLS_MANIFEST_ENV = "SCADBUDDY_AGENT_TOOLS_MANIFEST"
TOOL_QUEUE = "agent-tools"


@dataclass(frozen=True)
class ManifestEntry:
    name: str
    description: str
    input_schema: dict[str, Any]
    tier: str


def load_manifest(path: str) -> list[ManifestEntry]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [ManifestEntry(e["name"], e["description"], e["input_schema"], e["tier"]) for e in raw]


def _stub(name: str) -> Any:
    async def _remote(args: dict[str, Any]) -> Any:
        raise RuntimeError(f"{name} is served by the agent-tools worker, not agent-durable")

    _remote.__name__ = f"remote_{name}"
    return activity.defn(name=name)(_remote)


def durable_tools(entries: list[ManifestEntry]) -> list[DurableTool]:
    return [
        activity_as_tool(
            _stub(e.name),
            name=e.name,
            description=e.description,
            input_schema=e.input_schema,
            needs_approval=e.tier == "outward",
            task_queue=TOOL_QUEUE,
            # Renders and print starts answer within their own deadlines (§4.2); a tool
            # that takes longer answers with an operation the model follows.
            start_to_close_timeout=timedelta(minutes=2),
        )
        for e in entries
    ]


def tiers(entries: list[ManifestEntry]) -> dict[str, str]:
    return {e.name: e.tier for e in entries}


_PATH = os.environ.get(TOOLS_MANIFEST_ENV, "/app/agent-durable/tools.json")
# Loaded once per process (the workflow imports it passed through the sandbox). A
# missing manifest leaves no tools; worker.main() refuses to start with none.
_ENTRIES = load_manifest(_PATH) if os.path.exists(_PATH) else []
TOOLS = durable_tools(_ENTRIES)
TIERS = tiers(_ENTRIES)
