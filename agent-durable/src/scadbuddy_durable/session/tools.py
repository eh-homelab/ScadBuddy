"""The agent's tools as durable tools (spec 2026-10-01 §6.3, plan 5c Ruling 4).

``agent/dist/tools.json`` (the agent's build writes it from ALL_TOOLS and the
durable-only answer tools) is read once, when the worker starts, from
``SCADBUDDY_DURABLE_TOOLS_JSON``. Each entry becomes an ``activity_as_tool`` on
``agent-tools``, where the agent service serves it under its name; the stub here
names the activity and never runs. A call parks at the session's gate when the tool
has a ``hitl`` kind (``needs_approval``), or when Settings → Plugins raised it to
``outward`` (#2088): ``apply_overrides``, each turn, from what ``gate_settings`` read.

The workflow sandbox imports this module through, so the manifest the worker loaded is
the one every DurableSession declares.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from temporalio import activity, workflow
from temporalio.claude_agent_sdk import DurableTool, activity_as_tool
from temporalio.common import RetryPolicy

from scadbuddy_durable.session.models import TOOLS_QUEUE, TurnTool

Hitl = Literal["approval", "answer"]

# agent/src/harness/permissions.ts RISK_TIERS, least to most.
RISK_TIERS = ("read", "write", "outward")
# agent/src/plugins/builtInTools.ts builtInToolsSetting(OWN_PLUGIN_NAME): the overrides
# of the tools a durable session calls.
OVERRIDES_SETTING = "builtin_tools.scadbuddy"

log = logging.getLogger(__name__)


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
        name, tier = str(e["name"]), str(e["tier"])
        if tier not in RISK_TIERS:
            # Said once, as the manifest is the worker's for its life, and the worker
            # still starts: such a tool is labelled outward, as an unlisted one is
            # (#3245, #3581). The tool, never the value.
            log.warning("tool %s has no known tier in the manifest: labelled outward", name)
            tier = "outward"
        out.append(
            ManifestEntry(
                name=name,
                description=str(e["description"]),
                input_schema=dict(e["input_schema"]),
                tier=tier,
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


def sanitise_overrides(
    entries: Sequence[ManifestEntry], stored: Any
) -> tuple[dict[str, str], list[str]]:
    """What Settings stored for the set, as builtInTools.ts ``sanitise`` keeps it:
    TIGHTEN ONLY. A tier counts only where it raises a known tool above its own tier, and
    a disabled tool only where it is known, so a stale or hand-edited row never lowers or
    invents anything. The answer tools are durable-only, in no Settings set."""
    if not isinstance(stored, dict):
        return {}, []
    own = {e.name: e.tier for e in entries if e.hitl != "answer"}

    def rank(tier: str) -> int:
        return RISK_TIERS.index(tier) if tier in RISK_TIERS else -1

    tiers: dict[str, str] = {}
    raw_tiers = stored.get("tool_tiers")
    if isinstance(raw_tiers, dict):
        for name, tier in raw_tiers.items():
            if name in own and tier in RISK_TIERS and rank(tier) > rank(own[name]):
                tiers[name] = tier
    raw_disabled = stored.get("disabled_tools")
    disabled = (
        sorted({t for t in raw_disabled if isinstance(t, str) and t in own})
        if isinstance(raw_disabled, list)
        else []
    )
    return tiers, disabled


def apply_overrides(
    tools: Mapping[str, DurableTool],
    entries: Sequence[ManifestEntry],
    tool_tiers: Mapping[str, str],
    disabled: Collection[str],
) -> None:
    """Sets each tool's gate from its effective tier, as effectiveTool (registry.ts) has
    it: a tool raised to ``outward`` parks for approval and runs at most once, like any
    outward tool. A disabled tool never parks: its call goes straight to agent-tools,
    which refuses it naming Settings → Plugins. Computed from the manifest every time,
    so an override taken away is gone on the next turn."""
    for e in entries:
        tool = tools.get(e.name)
        if tool is None:
            continue
        raised = tool_tiers.get(e.name) == "outward" and e.tier != "outward"
        tool.needs_approval = e.name not in disabled and (e.hitl is not None or raised)
        tool.retry_policy = _ONCE if e.hitl == "approval" or raised else _RETRY


# agent/src/temporal/turnTools.ts: the activity a turn tool's call runs as.
TURN_TOOL_PREFIX = "turn:"
# How long a call pinned to a replica may take from scheduled to done.
PINNED_DEADLINE = timedelta(minutes=11)


def turn_tools(entries: Sequence[TurnTool]) -> list[DurableTool]:
    """A turn's other tools as durable tools on agent-tools (#2320): each call is the
    activity ``turn:<name>``, which the agent serves with its ``default`` activity. One
    that may be outward parks (the workflow asks ``turn_tool_tier`` first, so a call whose
    input is read or write goes straight through) and runs at most once.

    A tool the SDK refuses (a name over 50 characters, such as
    ``plugin_playwright_playwright__browser_console_messages``) is left out and logged:
    raised, it failed every workflow task of the session, which then retried for ever."""
    built: list[DurableTool] = []
    for e in entries:
        try:
            built.append(
                activity_as_tool(
                    _stub(f"{TURN_TOOL_PREFIX}{e.name}"),
                    name=e.name,
                    description=e.description,
                    input_schema=e.input_schema,
                    needs_approval=e.may_park,
                    task_queue=e.task_queue or TOOLS_QUEUE,
                    start_to_close_timeout=timedelta(minutes=10),
                    # Pinned to a replica: if it is gone nothing polls its queue, and the
                    # call fails here rather than waiting for ever.
                    schedule_to_close_timeout=PINNED_DEADLINE if e.task_queue else None,
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=_ONCE if e.may_park else _RETRY,
                )
            )
        except ValueError as error:
            # The listing (turnTools.ts) already leaves these out and tells the user;
            # this only keeps a name it missed from failing the session.
            if workflow.in_workflow():
                workflow.logger.warning("turn tool left out: %s", error)
    return built


def offer_turn_tools(
    offered: dict[str, DurableTool],
    held: dict[str, DurableTool],
    previous: Collection[str],
    entries: Sequence[TurnTool],
) -> set[str]:
    """Replaces last turn's turn tools with this turn's in both the agent's tool table
    (``offered``, the plugin's own, which each segment declares from) and the workflow's
    (``held``). The manifest's own tools are never touched: a turn tool's name always
    has ``__`` in it, a manifest tool's never. Returns the names now offered."""
    for name in previous:
        offered.pop(name, None)
        held.pop(name, None)
    names: set[str] = set()
    for tool in turn_tools([e for e in entries if "__" in e.name and e.name not in offered]):
        offered[tool.name] = tool
        held[tool.name] = tool
        names.add(tool.name)
    return names
