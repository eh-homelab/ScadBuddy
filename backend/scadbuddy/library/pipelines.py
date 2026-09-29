"""What a template's pipeline is, read without running it (spec 2026-09-27 §5, §8)."""

from __future__ import annotations

import ast
import hashlib

#: `pipeline.api` majors this host runs: the current one and, once there is one, the
#: previous (§8.1).
PIPELINE_API_SUPPORTED: tuple[int, ...] = (1,)

#: The file name the built-in pipeline's frames carry.
DEFAULT_PIPELINE_FILE = "<default pipeline>"

#: §5.3: today's behaviour, including §6.4's `plates = N` inside `pack`.
DEFAULT_PIPELINE_SOURCE = """\
async def run(ctx, inputs):
    part = await ctx.render("model.scad", **inputs.get("params", {}))
    await ctx.output(plates=await ctx.pack([part]), name=inputs.get("name"))
"""


def pipeline_version_of(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def inputs_version_of(source: str) -> int:
    """The top-level ``INPUTS_VERSION = <int>`` (§8.2), found by parsing: the API
    process never runs template code (§9)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "INPUTS_VERSION" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int
        ):
            return node.value.value
    return 0
