"""Pipeline source run inside the workflow sandbox (spec 2026-09-27 §3.4 step 2).

Compiled under the template file's own name, so a traceback frame carries
``pipeline/pipeline.py`` and its line. Nothing here reads a file: the source
came out of `load_pipeline`, and so out of the workflow history."""

from __future__ import annotations

from typing import Any

PIPELINE_MODULE_NAME = "scadbuddy_pipeline"


class PipelineContractError(Exception):
    """The source ran, but is not a pipeline (§5.2)."""


def load_pipeline_module(source: str, filename: str) -> dict[str, Any]:
    # dont_inherit: this module's `from __future__` flags are not the template's.
    code = compile(source, filename, "exec", dont_inherit=True)
    namespace: dict[str, Any] = {"__name__": PIPELINE_MODULE_NAME, "__file__": filename}
    exec(code, namespace)
    if not callable(namespace.get("run")):
        raise PipelineContractError(f"{filename} defines no run(ctx, inputs)")
    return namespace


def pipeline_error(error: BaseException, filename: str) -> str:
    """The job's `error`: where in the template it happened, then what. The innermost
    frame in ``filename`` wins; walking frames never reads a file (no linecache)."""
    line: int | None = None
    if isinstance(error, SyntaxError) and error.filename == filename:
        line = error.lineno
    tb = error.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == filename:
            line = tb.tb_lineno
        tb = tb.tb_next
    where = filename if line is None else f"{filename}:{line}"
    message = error.msg if isinstance(error, SyntaxError) else str(error)
    return f"{where}: {type(error).__name__}: {message}"
