"""The host functions and result types a flow script sees, for the editor's completion
and hover (#2487). Rendered on each call by the harness from `SCRIPT_TOOLS`, the list
`check_script`'s type check is built over, so the two never drift."""

from __future__ import annotations

import ast
import copy
from collections.abc import Mapping

from pydantic import BaseModel
from temporal_agent_harness.harness.code_mode.stubs import render_host_interface

from scadbuddy.flows.manifest import tiers
from scadbuddy.workflows.flow_tools import SCRIPT_TOOLS


class HostFunction(BaseModel):
    name: str
    #: `async def name(...) -> Result`, as the type check's stubs declare it.
    signature: str
    parameters: list[str]
    doc: str


class HostType(BaseModel):
    """A result shape (a `TypedDict` in the stubs)."""

    name: str
    source: str


class ManifestTool(BaseModel):
    """A tool `tool(name, args)` may call, with its tier."""

    name: str
    tier: str


class HostInterface(BaseModel):
    functions: list[HostFunction]
    types: list[HostType]
    tools: list[ManifestTool]


def host_interface(tool_tiers: Mapping[str, str] | None = None) -> HostInterface:
    """The host functions and types, and the manifest's tools at `tool_tiers` (the
    manifest's own tiers when None)."""
    source = render_host_interface(SCRIPT_TOOLS)
    functions: list[HostFunction] = []
    types: list[HostType] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.AsyncFunctionDef):
            stub = copy.copy(node)
            stub.body = []
            # `async def f(...) -> R:` with no body; unparse adds the colon.
            signature = ast.unparse(stub).strip().removesuffix(":")
            functions.append(
                HostFunction(
                    name=node.name,
                    signature=signature,
                    parameters=[a.arg for a in node.args.args],
                    doc=ast.get_docstring(node) or "",
                )
            )
        elif isinstance(node, ast.ClassDef):
            types.append(
                HostType(name=node.name, source=ast.get_source_segment(source, node) or "")
            )
    tiered = tiers() if tool_tiers is None else tool_tiers
    tools = [ManifestTool(name=name, tier=tier) for name, tier in sorted(tiered.items())]
    return HostInterface(functions=functions, types=types, tools=tools)
