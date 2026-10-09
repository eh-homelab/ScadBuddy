"""The agent's tools as durable tools (spec 2026-10-01 §6.3, plan 5c Ruling 4).

``agent/dist/tools.json`` (the agent's build writes it from ALL_TOOLS and the
durable-only answer tools) is read once, when the worker starts, from
``SCADBUDDY_DURABLE_TOOLS_JSON``. Each entry becomes an ``activity_as_tool`` on
``agent-tools``, where the agent service serves it under its name; the stub here
names the activity and never runs. A call parks at the session's gate when the tool
has a ``hitl`` kind (``needs_approval``).

The workflow sandbox imports this module through, so the manifest the worker loaded is
the one every DurableSession declares.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from temporalio import activity
from temporalio.claude_agent_sdk import DurableTool, activity_as_tool
from temporalio.common import RetryPolicy

from scadbuddy_durable.session.models import TOOLS_QUEUE

Hitl = Literal["approval", "answer"]


@dataclass(frozen=True)
class ManifestEntry:
    name: str
    description: str
    input_schema: dict[str, Any]
    tier: str
    hitl: Hitl | None


_MANIFEST: tuple[ManifestEntry, ...] = ()


def parse_manifest(data: Any) -> tuple[ManifestEntry, ...]:
    if not isinstance(data, list):
        raise ValueError("the tool manifest is not a list")
    out: list[ManifestEntry] = []
    for e in data:
        hitl = e.get("hitl")
        if hitl not in (None, "approval", "answer"):
            raise ValueError(f"tool {e.get('name')!r} has an unknown hitl {hitl!r}")
        out.append(
            ManifestEntry(
                name=str(e["name"]),
                description=str(e["description"]),
                input_schema=dict(e["input_schema"]),
                tier=str(e["tier"]),
                hitl=hitl,
            )
        )
    return tuple(out)


def load_manifest(path: str | Path) -> tuple[ManifestEntry, ...]:
    """Reads the manifest and makes it every DurableSession's (the worker, at start)."""
    global _MANIFEST
    _MANIFEST = parse_manifest(json.loads(Path(path).read_text(encoding="utf-8")))
    return _MANIFEST


def use_manifest(entries: Sequence[ManifestEntry]) -> None:
    """Tests: the manifest a worker in this process declares."""
    global _MANIFEST
    _MANIFEST = tuple(entries)


def manifest() -> tuple[ManifestEntry, ...]:
    return _MANIFEST


def _stub(name: str) -> Callable[..., Any]:
    async def remote(args: dict[str, Any]) -> Any:
        raise RuntimeError(f"{name} runs on {TOOLS_QUEUE}, in the agent service, never here")

    return activity.defn(name=name)(remote)


# The agent-tools activity fails non-retryably on a tool's own error, which reaches
# Claude as the result. A lost worker is retried for a tool with no outward effect; an
# outward (gated) call runs at most once, so an approval never runs its effect twice.
_RETRY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=["ToolError"])
_ONCE = RetryPolicy(maximum_attempts=1)


def durable_tools(entries: Sequence[ManifestEntry]) -> list[DurableTool]:
    return [
        activity_as_tool(
            _stub(e.name),
            name=e.name,
            description=e.description,
            input_schema=e.input_schema,
            needs_approval=e.hitl is not None,
            task_queue=TOOLS_QUEUE,
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=_ONCE if e.hitl == "approval" else _RETRY,
        )
        for e in entries
    ]
