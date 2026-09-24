from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.core.config import Config
from scadbuddy.render.runner import OpenSCADError, run_openscad
from scadbuddy.render.schema import build_schema

logger = logging.getLogger(__name__)

NUL = b"\x00"

Severity = Literal["error", "warning", "trace"]

# OpenSCAD prefixes every diagnostic and appends its location, e.g.
#   ERROR: Parser error: syntax error in file model.scad, line 3
#   WARNING: Can't find include file 'lib.scad'. in file model.scad, line 1
#   TRACE: called by 'assert' in file model.scad, line 2
# Lines without a prefix ("Can't parse file 'model.scad'!") carry no location and
# stay in the log tail rather than becoming a diagnostic.
_DIAGNOSTIC_RE = re.compile(
    r"^(?P<severity>ERROR|WARNING|TRACE):\s+(?P<message>.+?)"
    r"(?:\s+in file (?P<file>.+?), line (?P<line>\d+))?\s*$"
)


class NotOpenSCADError(ValueError):
    """The upload is not something OpenSCAD will parse."""

    def __init__(self, message: str, log_tail: list[str] | None = None) -> None:
        super().__init__(message)
        self.log_tail = log_tail or []


class Diagnostic(BaseModel):
    """One OpenSCAD message, with the line it points at when it names one."""

    severity: Severity
    message: str
    line: int | None = None
    file: str | None = None


class SourceCheck(BaseModel):
    ok: bool
    checked: bool = Field(description="False when no openscad binary was available to ask")
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    log_tail: list[str] = Field(default_factory=list)
    parameters: int | None = Field(
        default=None, description="Customizer parameters derived, when the source got that far"
    )

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]


def decode_source(raw: bytes) -> str:
    """Reject anything that is not UTF-8 text before it reaches OpenSCAD."""
    if NUL in raw:
        raise NotOpenSCADError("the upload is binary, not an OpenSCAD source file")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise NotOpenSCADError("the upload is not valid UTF-8 text") from error


def parse_diagnostics(log: list[str]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for line in log:
        match = _DIAGNOSTIC_RE.match(line.strip())
        if match is None:
            continue
        raw_line = match["line"]
        diagnostics.append(
            Diagnostic(
                severity=match["severity"].lower(),  # type: ignore[arg-type]
                message=match["message"],
                line=int(raw_line) if raw_line else None,
                file=match["file"],
            )
        )
    return diagnostics


async def check_source(source: str, *, config: Config) -> SourceCheck:
    """Parse-check the source and derive its customizer schema, without saving anything.

    ``-o <file>.param`` is the customizer-parameter export: it parses the file and
    evaluates its top-level scope, but renders no geometry, so it is cheap enough to
    run on every edit and is the same invocation the schema is really built from.
    Checking with a different one would let a source pass the check and still fail to
    produce a schema.

    The exit code is not on its own the signal — a failed top-level ``assert`` prints
    ``ERROR:`` and still exits 0 — so an ERROR diagnostic fails the check too.
    """
    if shutil.which(config.openscad) is None:
        logger.warning("openscad is not on PATH; the parse check cannot run")
        return SourceCheck(ok=True, checked=False)

    derivation: Diagnostic | None = None
    parameters: int | None = None
    with tempfile.TemporaryDirectory(prefix="scadbuddy-check-") as tmp:
        scad_path = Path(tmp) / "model.scad"
        scad_path.write_text(source, encoding="utf-8")
        param_path = Path(tmp) / "model.param"
        try:
            output = await run_openscad(
                ["-o", str(param_path), scad_path.name], cwd=scad_path.parent, config=config
            )
        except OpenSCADError as error:
            log_tail = error.log_tail
            returncode = error.returncode
        else:
            log_tail = output.log_tail
            returncode = output.returncode
            try:
                schema = build_schema(json.loads(param_path.read_text(encoding="utf-8")), source)
            except (OSError, ValueError) as error:
                # It parsed, but the customizer schema cannot be built from it — the
                # model would save and then open with no parameter panel.
                derivation = Diagnostic(
                    severity="error",
                    message=f"the customizer schema could not be derived: {error}",
                )
            else:
                parameters = len(schema.parameters)

    diagnostics = parse_diagnostics(log_tail)
    if derivation is not None:
        diagnostics.append(derivation)
    ok = returncode == 0 and not any(d.severity == "error" for d in diagnostics)
    return SourceCheck(
        ok=ok,
        checked=True,
        diagnostics=diagnostics,
        log_tail=log_tail,
        parameters=parameters if ok else None,
    )
