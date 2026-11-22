"""What a hosted plugin's draft may hold (spec 2026-11-21 §6.2): its name, its paths and
its caps.

A path is checked at every write: UTF-8, relative, ``/``-separated, no empty segment, no
segment that starts with ``.`` (so no ``..``, no hidden file, no ``.claude-plugin/`` or
``.mcp.json``), no backslash or control character, and under ``skills/``, ``agents/`` or
``commands/``. ``.claude-plugin/plugin.json`` is generated at publish. A draft holds no
links: a file is the bytes a ``PUT`` sent. Only a file under ``skills/`` may be executable
(§6.8: a skill may hold scripts). A path that is a folder of another, or the same as
another but for case, is refused, so the unpacked tree is the same on every file system.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: The caps (spec §6.2, "my call, change them in review").
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 500
MAX_TOTAL_BYTES = 20 * 1024 * 1024
#: The longest path in UTF-8 bytes, and the longest segment (a file name on most systems).
MAX_PATH_BYTES = 1024
MAX_SEGMENT_BYTES = 255

TOP_LEVEL = ("skills", "agents", "commands")
MANIFEST_PATH = ".claude-plugin/plugin.json"

#: ``agent/src/plugins/registry.ts`` ``PLUGIN_NAME_RE``: what the package vetting takes
#: without an allowance (``vet.ts``), so a hosted plugin's name never needs one.
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")
#: ``registry.ts`` ``RESERVED_PLUGIN_NAMES``; keep the two lists the same.
RESERVED_NAMES = frozenset(
    {
        "scadbuddy",
        "claude",
        "anthropic",
        "plugin",
        "workspace",
        "computer-use",
        "claude-in-chrome",
        "claude-device",
        "hearthbot",
        "remote-devices",
        "webagent",
        "claude-code-remote",
        "ide",
    }
)
#: ``agent/src/plugins/packages/builtins.ts`` ``BUILT_INS``: the plugins in the agent image.
BUILT_IN_NAMES = frozenset({"scadbuddy"})

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PathRefusedError(ValueError):
    """A path, or an executable bit, the rules refuse; the message says why."""


def name_problem(name: str) -> str | None:
    """Why ``name`` cannot be a hosted plugin's, or None."""
    if not NAME_RE.match(name) or "--" in name:
        return (
            f'"{name}" is not 2-32 lower-case letters, digits and single hyphens,'
            " starting with a letter and ending with a letter or digit"
        )
    if name in RESERVED_NAMES:
        return f'"{name}" is reserved: it may clash with ScadBuddy\'s own plugin or tools'
    return None


def check_path(path: str) -> str:
    """``path`` when a draft may hold it; else `PathRefusedError`."""
    if path == "":
        raise PathRefusedError("the path is empty")
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        raise PathRefusedError("the path is not valid UTF-8") from None
    if len(encoded) > MAX_PATH_BYTES:
        raise PathRefusedError(f"the path is too long: at most {MAX_PATH_BYTES} bytes")
    if "\\" in path:
        raise PathRefusedError("the path holds a backslash; separate folders with /")
    if _CONTROL.search(path):
        raise PathRefusedError("the path holds a control character")
    if path.startswith("/"):
        raise PathRefusedError("the path must be relative to the plugin's root")
    if path == MANIFEST_PATH:
        raise PathRefusedError(f"{MANIFEST_PATH} is generated when the plugin is published")
    segments = path.split("/")
    for segment in segments:
        if segment == "":
            raise PathRefusedError("the path has an empty segment")
        if segment == "..":
            raise PathRefusedError('the path holds ".."')
        if segment.startswith("."):
            raise PathRefusedError(f'"{segment}" starts with "."; no hidden files or folders')
        if len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES:
            raise PathRefusedError(
                f"a segment of the path is too long: at most {MAX_SEGMENT_BYTES} bytes"
            )
    if len(segments) < 2 or segments[0] not in TOP_LEVEL:
        raise PathRefusedError("a file must be under skills/, agents/ or commands/")
    return path


def check_executable(path: str, *, executable: bool) -> None:
    if executable and not path.startswith("skills/"):
        raise PathRefusedError("only a file under skills/ may be executable")


def clash(path: str, existing: Iterable[str]) -> str | None:
    """Why ``path`` cannot sit beside ``existing`` in one tree, or None. ``path`` itself
    in ``existing`` is an overwrite, not a clash."""
    files: set[str] = set()
    folders: set[str] = set()
    folded: dict[str, str] = {}
    for other in existing:
        if other == path:
            continue
        files.add(other)
        parts = other.split("/")
        for i in range(1, len(parts)):
            folders.add("/".join(parts[:i]))
        for node in (other, *("/".join(parts[:i]) for i in range(1, len(parts)))):
            folded.setdefault(node.casefold(), node)
    parts = path.split("/")
    if path in folders:
        return f"{path} is a folder of another file"
    for i in range(1, len(parts)):
        prefix = "/".join(parts[:i])
        if prefix in files:
            return f"{prefix} is a file, so it cannot hold {path}"
    for i in range(1, len(parts) + 1):
        node = "/".join(parts[:i])
        seen = folded.get(node.casefold())
        if seen is not None and seen != node:
            return f"{node} differs from {seen} only in case"
    return None


def sort_key(path: str) -> bytes:
    """JavaScript's default sort, by UTF-16 code unit (``hash.ts`` sorts so): big-endian
    UTF-16 bytes compare as the code units do."""
    return path.encode("utf-16-be", "surrogatepass")
