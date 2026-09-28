"""OpenSCAD's log, read as structured diagnostics.

One parser for every OpenSCAD run: the editor's parse check reads it off the log
tail of a ``.param`` export (``library/scad.py``), and a render reads it off the
whole log as the runner drains it, so a job and ``GET /models/{slug}/diagnostics``
report the same records the editor's markers are drawn from (#252).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel

Severity = Literal["error", "warning", "trace"]

#: A render that floods its log (a warning inside a loop) keeps this many; the rest
#: are counted, not kept. The raw log tail is still there for anything else.
MAX_DIAGNOSTICS = 200

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


class Diagnostic(BaseModel):
    """One OpenSCAD message, with the line it points at when it names one."""

    severity: Severity
    message: str
    line: int | None = None
    file: str | None = None


def _relative(raw: str, roots: Sequence[Path]) -> str:
    """``raw`` relative to the first of ``roots`` it is under, or as printed.

    For a render the roots are the model's directory and each library checkout on
    ``OPENSCADPATH``, so a file reads ``model.scad`` or ``BOSL2/std.scad`` rather
    than a path into the data volume.
    """
    path = PurePosixPath(raw)
    if not path.is_absolute():
        return raw
    for root in roots:
        try:
            return str(path.relative_to(PurePosixPath(root)))
        except ValueError:
            continue
    return raw


def parse_line(line: str, roots: Sequence[Path] = ()) -> Diagnostic | None:
    match = _DIAGNOSTIC_RE.match(line.strip())
    if match is None:
        return None
    raw_line, file = match["line"], match["file"]
    return Diagnostic(
        severity=match["severity"].lower(),  # type: ignore[arg-type]
        message=match["message"],
        line=int(raw_line) if raw_line else None,
        file=_relative(file, roots) if file is not None else None,
    )


def parse_diagnostics(log: Iterable[str], roots: Sequence[Path] = ()) -> list[Diagnostic]:
    return [diagnostic for line in log if (diagnostic := parse_line(line, roots)) is not None]


class DiagnosticCollector:
    """Fed one log line at a time as the runner drains OpenSCAD's output, so a
    parser error -- the first thing OpenSCAD prints -- survives a long log that has
    pushed it out of the tail."""

    def __init__(self, roots: Sequence[Path] = ()) -> None:
        self.roots = tuple(roots)
        self.diagnostics: list[Diagnostic] = []
        #: Diagnostics seen past :data:`MAX_DIAGNOSTICS`.
        self.dropped = 0

    def feed(self, line: str) -> None:
        diagnostic = parse_line(line, self.roots)
        if diagnostic is None:
            return
        if len(self.diagnostics) >= MAX_DIAGNOSTICS:
            self.dropped += 1
            return
        self.diagnostics.append(diagnostic)
