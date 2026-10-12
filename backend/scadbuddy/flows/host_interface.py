"""The host functions and result types a flow script sees, for the editor's completion
and hover (#2487). Rendered on each call by the harness from `SCRIPT_TOOLS`, the list
`check_script`'s type check is built over, so the two never drift."""

from __future__ import annotations

import ast

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


def host_interface() -> HostInterface:
    source = render_host_interface(SCRIPT_TOOLS)
    functions: list[HostFunction] = []
    types: list[HostType] = []
    lines = source.splitlines()
    for node in ast.parse(source).body:
        if isinstance(node, ast.AsyncFunctionDef):
            head = " ".join(lines[node.lineno - 1 : node.body[0].lineno - 1])
            signature = head.strip().removesuffix(":")
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
    tools = [ManifestTool(name=name, tier=tier) for name, tier in sorted(tiers().items())]
    return HostInterface(functions=functions, types=types, tools=tools)
