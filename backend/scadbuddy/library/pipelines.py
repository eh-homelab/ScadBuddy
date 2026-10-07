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


#: A pipeline source longer than this (in characters) is not parsed for its
#: `INPUTS_VERSION`: the catalogue parses every template's pipeline on each listing, and a
#: real pipeline is a few KiB.
INPUTS_VERSION_PARSE_LIMIT = 256 * 1024


def inputs_version_of(source: str) -> int:
    """The top-level ``INPUTS_VERSION`` (§8.2), found by parsing: the API process never
    runs template code (§9). The last top-level assignment wins, plain or annotated, as
    it does when the module runs; anything but a literal int there is version 0.

    Never raises: one template's pipeline file must not break the catalogue. Past
    `INPUTS_VERSION_PARSE_LIMIT`, and on whatever ``ast.parse`` raises for hostile
    source (measured on 3.12: ``MemoryError`` for a parser stack overflow and
    ``RecursionError`` during ast construction; ``ValueError`` for a NUL byte), it is 0."""
    if len(source) > INPUTS_VERSION_PARSE_LIMIT:
        return 0
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return 0
    value: ast.expr | None = None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "INPUTS_VERSION" for t in node.targets)
        ) or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "INPUTS_VERSION"
            and node.value is not None
        ):
            value = node.value
    if isinstance(value, ast.Constant) and type(value.value) is int:
        return value.value
    return 0
