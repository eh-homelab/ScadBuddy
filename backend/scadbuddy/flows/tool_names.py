"""The agent tools a flow script names by a literal (#6365).

`tool(name, args)` and `tool_result(name, args)` take any string, so the type check
cannot say whether the agent has the tool. A run that calls one the agent does not have
fails at that call, after asking again for 14 s in case an older agent build answered
(`workflows/flow_tools.py` UNREGISTERED_WAITS). That wait cannot cover a tool that is
not deployed at all: makerworld-import version 2 was registered on 2026-11-13 naming
`resolve_makerworld_url`, which first reached prod on 2026-11-15 at 01:22, and its run
at 00:57 failed on it. So a registration checks the names it can see: a literal first
argument that names one of ScadBuddy's own tools. A name built at run time cannot be
checked, and a turn tool (`<server>__<tool>`, a plugin's or http_request) depends on
plugins and Settings that may change before a run, so neither is refused here.
"""

from __future__ import annotations

import ast
from collections.abc import Container

from scadbuddy.flows.typecheck import ScriptProblem

#: The host functions whose first argument is an agent tool's name
#: (`workflows/flow_tools.py` `tool`, `tool_result`).
TOOL_FUNCTIONS = frozenset({"tool", "tool_result"})
#: A turn tool's name, `<server>__<tool>` (`workflows/flow_tools.py` TURN_TOOL_SEPARATOR).
TURN_TOOL_SEPARATOR = "__"


def unknown_tools(script: str, known: Container[str]) -> list[ScriptProblem]:
    """A problem, by line, for each literal tool name in `script` that is not in `known`.
    A script that does not parse has none here: the type check reports it."""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return []
    problems: list[ScriptProblem] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in TOOL_FUNCTIONS
        ):
            continue
        named = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "name":
                named = keyword.value
        if not (isinstance(named, ast.Constant) and isinstance(named.value, str)):
            continue
        name = named.value
        if TURN_TOOL_SEPARATOR in name or name in known:
            continue
        problems.append(
            ScriptProblem(
                line=node.lineno,
                message=(
                    f"there is no tool {name!r}: ScadBuddy's assistant does not have it"
                    " (misspelt, or not deployed yet)"
                ),
            )
        )
    return sorted(problems, key=lambda p: p.line or 0)
