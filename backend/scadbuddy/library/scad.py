from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from scadbuddy.core.config import Config

# Re-exported: the editor's check and a render share one parser (#252).
from scadbuddy.render.diagnostics import Diagnostic as Diagnostic
from scadbuddy.render.diagnostics import parse_diagnostics as parse_diagnostics
from scadbuddy.render.runner import OpenSCADError, ProcessOutput, RenderTimeoutError, run_openscad
from scadbuddy.render.schema import CustomizerSchema, build_schema

logger = logging.getLogger(__name__)

NUL = b"\x00"

#: ScadBuddy's own sidecars in a model directory. Everything else beside the source is
#: something the source may `include`, `use`, `import` or `surface`, so it comes along
#: when a candidate is checked against that directory.
SIDECARS = frozenset({"model.scad", "model.json", "thumbnail.png", "README.md"})


class NotOpenSCADError(ValueError):
    """The upload is not something OpenSCAD will parse."""

    def __init__(self, message: str, log_tail: list[str] | None = None) -> None:
        super().__init__(message)
        self.log_tail = log_tail or []


class SourceCheck(BaseModel):
    ok: bool
    checked: bool = Field(description="False when no openscad binary was available to ask")
    timed_out: bool = Field(
        default=False, description="True when OpenSCAD was killed on the render timeout"
    )
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    log_tail: list[str] = Field(default_factory=list)
    parameters: int | None = Field(
        default=None, description="Customizer parameters derived, when the source got that far"
    )

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]


@dataclass(frozen=True)
class CheckedSource:
    """What one OpenSCAD run produced: the verdict, and the schema it was built from.

    The schema is carried rather than recomputed because deriving it IS the check —
    a caller that then wants to store it must not pay for a second subprocess.
    """

    check: SourceCheck
    schema: CustomizerSchema | None


def decode_source(raw: bytes) -> str:
    """Reject anything that is not UTF-8 text before it reaches OpenSCAD."""
    if NUL in raw:
        raise NotOpenSCADError("the upload is binary, not an OpenSCAD source file")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise NotOpenSCADError("the upload is not valid UTF-8 text") from error


def _stage(source: str, directory: Path, context: Path | None) -> Path:
    """Write the candidate source into a sandbox, beside whatever it may include.

    Without the context copy, `include <helper.scad>` cannot resolve — OpenSCAD looks
    beside the file it is given — and a missing include is only a WARNING, so the check
    would pass and hand back a schema missing everything the helper declared.
    """
    if context is not None and context.is_dir():
        for entry in context.iterdir():
            if entry.name in SIDECARS:
                continue
            # Links are copied as links, never followed (#994): this runs unconfined,
            # and the sandbox refuses openscad a link that leads out of the model.
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(entry, directory / entry.name, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, directory / entry.name, follow_symlinks=False)
    scad_path = directory / "model.scad"
    scad_path.write_text(source, encoding="utf-8")
    return scad_path


async def inspect_source(
    source: str,
    *,
    config: Config,
    limit: asyncio.Semaphore | None = None,
    context: Path | None = None,
) -> CheckedSource:
    """Parse-check the source and derive its customizer schema, without saving anything.

    ``-o <file>.param`` is the customizer-parameter export: it parses the file and
    evaluates its top-level scope, but renders no geometry, so it is cheap enough to
    run on every edit and is the same invocation the schema is really built from.
    Checking with a different one would let a source pass the check and still fail to
    produce a schema.

    The exit code is not on its own the signal — a failed top-level ``assert`` prints
    ``ERROR:`` and still exits 0 — so an ERROR diagnostic fails the check too.

    ``limit`` caps how many of these run at once. The editor checks on every pause in
    typing, from any number of tabs, and none of this goes through the render queue —
    without a cap the only bound on concurrent ``openscad`` processes is how fast
    people type.

    ``context`` is an existing model's directory, whose sibling files are copied in
    beside the candidate so ``include``/``use``/``surface`` resolve exactly as they
    will on render.
    """
    if shutil.which(config.openscad) is None:
        logger.warning("openscad is not on PATH; the parse check cannot run")
        return CheckedSource(check=SourceCheck(ok=True, checked=False), schema=None)

    derivation: Diagnostic | None = None
    schema: CustomizerSchema | None = None
    timed_out = False
    with tempfile.TemporaryDirectory(prefix="scadbuddy-check-") as tmp:
        # Off the loop: this copies the model's whole directory, and a check runs on
        # every keystroke pause, so on the loop it stalls every other request in the
        # pod — renders, probes, other users' checks — for the length of the copy.
        scad_path = await asyncio.to_thread(_stage, source, Path(tmp), context)
        param_path = Path(tmp) / "model.param"
        args = ["-o", str(param_path), scad_path.name]
        try:
            async with limit or nullcontext():
                output: ProcessOutput = await run_openscad(
                    args, cwd=scad_path.parent, config=config
                )
        except RenderTimeoutError as error:
            # A timeout is not a parse failure, and saying so is the difference between
            # "your model is broken" and "your model is slow".
            timed_out = True
            log_tail = error.log_tail
            returncode = error.returncode
            derivation = Diagnostic(
                severity="error",
                message=f"the check timed out after {config.render_timeout:g}s",
            )
        except OpenSCADError as error:
            log_tail = error.log_tail
            returncode = error.returncode
        else:
            log_tail = output.log_tail
            returncode = output.returncode
            try:
                schema = build_schema(json.loads(param_path.read_text(encoding="utf-8")), source)
            except (OSError, ValueError, KeyError, TypeError) as error:
                # It parsed, but the customizer schema cannot be built from it — the
                # model would save and then open with no parameter panel. KeyError and
                # TypeError are in the list because `build_schema` subscripts the
                # export's dicts directly: a `.param` entry without a `name`, or an
                # option without a `value`, raises neither OSError nor ValueError.
                derivation = Diagnostic(
                    severity="error",
                    message=f"the customizer schema could not be derived: {error}",
                )

    diagnostics = parse_diagnostics(log_tail)
    if derivation is not None:
        diagnostics.append(derivation)
    ok = returncode == 0 and not any(d.severity == "error" for d in diagnostics)
    return CheckedSource(
        check=SourceCheck(
            ok=ok,
            checked=True,
            timed_out=timed_out,
            diagnostics=diagnostics,
            log_tail=log_tail,
            parameters=len(schema.parameters) if ok and schema is not None else None,
        ),
        schema=schema if ok else None,
    )


async def check_source(
    source: str,
    *,
    config: Config,
    limit: asyncio.Semaphore | None = None,
    context: Path | None = None,
) -> SourceCheck:
    """The verdict alone, for callers with nothing to store."""
    return (await inspect_source(source, config=config, limit=limit, context=context)).check
