"""What an ``openscad`` run may touch on the server's filesystem (#994).

A model's source is untrusted, and every ``openscad`` call — the editor's parse check,
the schema export, a render — goes through :func:`scadbuddy.render.runner.run_openscad`,
which applies both halves of this:

- :func:`refuse_escaping_includes` refuses, before the run, an ``include``/``use``
  target that is absolute or has a ``..`` component, in the model's file or any of its
  own files that file includes. OpenSCAD's messages for such a target differ by whether
  the file exists and whether it is readable, so the run itself would be an oracle for
  the container's filesystem. The refusal says the same thing whatever is there, and
  nothing is looked up to say it.
- :func:`sandboxed` runs the binary under :mod:`scadbuddy.render.sandbox`'s Landlock
  ruleset: the system's own files, the model's directory, the libraries it pins and the
  fonts on the data volume are readable, and only the output's directory and the font
  caches writable. That is what stops a path computed at run time (``import()``,
  ``surface()``), and a symbolic link that leads out of the model's directory.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

from scadbuddy.core.config import Config
from scadbuddy.core.fontconfig import cache_dir, fonts_dir
from scadbuddy.render import sandbox

logger = logging.getLogger(__name__)

# OpenSCAD's lexer: `include[ \t\r\n]*"<"` then `[^\t\r\n>]*` up to `>`. Matched over
# the whole text, comments and strings included, and without asking whether the
# keyword is part of a longer identifier: a superset of what OpenSCAD reads, so no
# disagreement with its lexer can let a statement through. A match OpenSCAD would
# not read only matters if its target is absolute or climbs, which ordinary
# comparisons (`a_use < b && c > d`) do not.
_STATEMENT = re.compile(r"(include|use)[ \t\r\n]*<([^\t\r\n>]*)>")
#: Files of the model's own directory followed, at most.
MAX_FILES = 64
#: A followed file larger than this is not read.
MAX_FILE_BYTES = 2 * 1024 * 1024

#: The system's files ``openscad`` needs: its shared libraries, its own resources
#: (``/usr/local/share/openscad``), the fonts and their config and cache.
SYSTEM_READ = (
    "/usr",
    "/lib",
    "/lib64",
    "/bin",
    "/etc/fonts",
    "/etc/ld.so.cache",
    "/var/cache/fontconfig",
    "/dev/urandom",
)
SYSTEM_WRITE = ("/dev/null",)


@dataclass(frozen=True)
class EscapingInclude:
    kind: str
    target: str
    file: str
    line: int

    def log_line(self) -> str:
        """As OpenSCAD words a diagnostic, so the parser reads the line it points at."""
        return (
            f"ERROR: {self.kind} <{self.target}> is outside the model's directory and its "
            f"libraries; name a file in either without '..' or a leading '/' "
            f"in file {self.file}, line {self.line}"
        )


@dataclass(frozen=True)
class UncheckedFile:
    """A model file the check could not read in full, refused rather than trusted."""

    target: str
    file: str
    line: int

    def log_line(self) -> str:
        return (
            f"ERROR: {self.target} is too large, or one file too many, to check for "
            f"includes outside the model's directory in file {self.file}, line {self.line}"
        )


def escapes(target: str) -> bool:
    path = PurePosixPath(target)
    return path.is_absolute() or ".." in path.parts


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def escaping_includes(root: Path, entry: str) -> list[EscapingInclude | UncheckedFile]:
    """Every ``include``/``use`` in ``entry`` (relative to ``root``) and in the files of
    ``root`` it reaches whose target is absolute or has a ``..`` component.

    A file is followed only when its real path is inside ``root``: this runs in the
    backend's own process, unconfined, so a symbolic link out of the model's directory
    is never read here (the sandbox refuses it to ``openscad``). A model file that is
    reached but cannot be read in full — past :data:`MAX_FILES`, over
    :data:`MAX_FILE_BYTES`, unreadable — is refused too: an unread file could hold
    anything.
    """
    real_root = root.resolve()
    found: list[EscapingInclude | UncheckedFile] = []
    queue = [(root / entry, entry, 0)]
    seen: set[Path] = set()
    while queue:
        current, statement_file, statement_line = queue.pop()
        try:
            real = current.resolve()
        except OSError:
            continue
        if real in seen or not _within(real, real_root) or not real.is_file():
            continue
        seen.add(real)
        name = str(real.relative_to(real_root))
        try:
            if len(seen) > MAX_FILES or real.stat().st_size > MAX_FILE_BYTES:
                raise OSError("too large")
            text = real.read_text(encoding="utf-8", errors="replace")
        except OSError:
            found.append(UncheckedFile(name, statement_file, statement_line))
            continue
        for match in _STATEMENT.finditer(text):
            kind, target = match[1], match[2]
            line = text.count("\n", 0, match.start(2)) + 1
            if escapes(target):
                found.append(EscapingInclude(kind, target, name, line))
            else:
                # Relative to the file as named and as resolved: whichever OpenSCAD
                # uses for a linked file, both are looked at.
                queue.append((current.parent / target, name, line))
                queue.append((real.parent / target, name, line))
    return found


@cache
def landlock_abi() -> int:
    abi = sandbox.abi_version()
    if abi < 1:
        logger.warning(
            "Landlock is not available on this kernel: openscad runs without a "
            "filesystem sandbox, so a model can read any file this process can (#994)"
        )
    return abi


def _outputs(args: Sequence[str]) -> list[Path]:
    return [Path(args[i + 1]).parent for i, arg in enumerate(args[:-1]) if arg == "-o"]


def sandboxed(
    binary: str, args: Sequence[str], *, cwd: Path, config: Config, env: dict[str, str]
) -> list[str]:
    """The command line that runs ``binary`` with ``args`` under the sandbox, or plainly
    when the kernel cannot provide one."""
    if landlock_abi() < 1:
        return [binary, *args]
    resolved = shutil.which(binary) or binary
    executable = os.path.realpath(resolved)
    read = [
        *SYSTEM_READ,
        os.path.dirname(executable),
        str(cwd.resolve()),
        *(str(path) for path in config.library_path),
        str(fonts_dir(config.data_dir)),
    ]
    write = [
        *SYSTEM_WRITE,
        *(str((cwd / path).resolve()) for path in _outputs(args)),
        str(cache_dir(config.data_dir)),
    ]
    home = env.get("HOME")
    if home:
        cache_home = env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
        # Only fontconfig's own cache; created so the rule has something to name.
        fontconfig_cache = os.path.join(cache_home, "fontconfig")
        with suppress(OSError):
            os.makedirs(fontconfig_cache, exist_ok=True)
        write.append(fontconfig_cache)
    return sandbox.command([executable, *args], read=read, write=write)
