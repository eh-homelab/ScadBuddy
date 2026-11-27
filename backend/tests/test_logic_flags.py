"""Workflow logic flag 2 (`scadbuddy/workflows/logic_flags.py`, #8313)."""

from __future__ import annotations

import ast
from pathlib import Path

import scadbuddy

SOURCE = Path(scadbuddy.__file__).parent


def test_every_workflow_worker_runs_single_batch() -> None:
    """A ``Worker`` that runs workflows without `single_batch` would start them on
    temporalio 1.37's legacy activation order, and the tests (whose workers get the flag
    from conftest) would not see it."""
    unwrapped = []
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        wrapped = {
            id(arg)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "single_batch"
            for arg in node.args
        }
        unwrapped += [
            f"{path.relative_to(SOURCE)}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "Worker"
            and any(k.arg == "workflows" for k in node.keywords)
            and id(node) not in wrapped
        ]
    assert unwrapped == []
