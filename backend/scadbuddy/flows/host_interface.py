"""The host functions and result types a flow script sees, for the editor's completion
and hover (#2487). Rendered on each call by the harness from `HOST_FUNCTIONS`, the
list `check_script`'s type check is built over, so the two never drift."""

from __future__ import annotations

import ast
import copy
from collections.abc import Mapping

from pydantic import BaseModel
from temporal_agent_harness.harness.code_mode.stubs import render_host_interface

from scadbuddy.workflows.flow_tools import HOST_FUNCTIONS


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
    """A tool `tool(name, args)` may call, with its tier as the agent resolves it now."""

    name: str
    tier: str


class HostInterface(BaseModel):
    functions: list[HostFunction]
    types: list[HostType]
    tools: list[ManifestTool]
    #: Why `tools` is empty when the agent did not list its tools (#2704); None otherwise.
    tools_unavailable: str | None = None


def host_interface(
    tool_tiers: Mapping[str, str], *, tools_unavailable: str | None = None
) -> HostInterface:
    """The host functions and types, and the agent's tools at `tool_tiers`."""
    source = render_host_interface(HOST_FUNCTIONS)
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
    tools = [ManifestTool(name=name, tier=tier) for name, tier in sorted(tool_tiers.items())]
    return HostInterface(
        functions=functions, types=types, tools=tools, tools_unavailable=tools_unavailable
    )
