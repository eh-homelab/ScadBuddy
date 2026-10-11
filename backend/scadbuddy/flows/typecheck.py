"""A flow script's check, at registration and again before each run (spec 2026-10-01
§7.2, plan 2026-10-09-durable-phase-6-flows.md Task B2): its size, the harness's Code
Mode type check against today's host functions, and the literal ranges the type check
cannot see."""

from __future__ import annotations

import ast
import logging
import re

import pydantic_monty
from pydantic import BaseModel
from temporal_agent_harness.harness import agent

from scadbuddy.workflows.flow_tools import MAX_WAIT_S, MIN_WAIT_S, run_flow_tool

log = logging.getLogger(__name__)

MAX_SCRIPT_BYTES = 65_536
_LINE = re.compile(r"-->\s*main\.py:(\d+):\d+")


class ScriptProblem(BaseModel):
    """One thing wrong with a script; `line` is 1-based, None for the whole script."""

    line: int | None = None
    message: str


async def check_script(script: str) -> list[ScriptProblem]:
    """Every problem with `script`; empty when it may run."""
    if len(script.encode()) > MAX_SCRIPT_BYTES:
        return [ScriptProblem(message="script is over 64 KiB")]
    try:
        report = await agent.code_mode_type_check(run_flow_tool(), script)
    except pydantic_monty.MontyCrashedError:
        log.warning("the flow type checker crashed")
        return [ScriptProblem(message="the checker failed: try again")]
    if report is not None:
        return _problems(report)
    return _literal_ranges(script)


def _problems(report: str) -> list[ScriptProblem]:
    """The checker's report, one problem per diagnostic, each at its first line."""
    head = report.split(":", 1)[0]
    chunks = re.split(r"\n(?=error\[)", report)
    problems: list[ScriptProblem] = []
    for chunk in chunks:
        match = _LINE.search(chunk)
        text = chunk if chunk.startswith(head) else f"{head}: {chunk}"
        problems.append(ScriptProblem(line=int(match.group(1)) if match else None, message=text))
    return problems


def _literal_ranges(script: str) -> list[ScriptProblem]:
    """A `wait_for_human` whose `timeout_s` is a literal out of range. A computed one is
    checked when the call runs."""
    problems: list[ScriptProblem] = []
    try:
        tree = ast.parse(script)
    except SyntaxError:  # the type check reports syntax; this only reads literals
        return problems
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "wait_for_human"
        ):
            continue
        timeout = next((k.value for k in node.keywords if k.arg == "timeout_s"), None)
        if timeout is None and len(node.args) > 1:
            timeout = node.args[1]
        if (
            isinstance(timeout, ast.Constant)
            and isinstance(timeout.value, int)
            and not MIN_WAIT_S <= timeout.value <= MAX_WAIT_S
        ):
            problems.append(
                ScriptProblem(
                    line=node.lineno,
                    message=f"wait_for_human's timeout_s must be {MIN_WAIT_S} to"
                    f" {MAX_WAIT_S} seconds, not {timeout.value}",
                )
            )
    return problems
