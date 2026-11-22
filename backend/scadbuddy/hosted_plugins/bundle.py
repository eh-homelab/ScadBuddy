"""A hosted plugin's publish (spec 2026-11-21 §6.3 steps 1-3): the draft's checks, the
generated ``.claude-plugin/plugin.json``, the deterministic zip and its content hash.

The checks are the structural ones of ``agent/src/plugins/packages/vet.ts``: each
``skills/<skill>/`` has a ``SKILL.md`` whose frontmatter names it and describes it, each
``agents/`` file is Markdown with a ``name`` and a ``description``, each ``commands/`` file
is Markdown, and every Markdown file is UTF-8 with frontmatter in the plain form ``vet.ts``
takes (``frontmatter()``: one block of plain ``key:`` lines at column 0, no anchors,
aliases, tags or repeated keys), no ``hooks``, ``mcpServers`` or ``permissionMode`` key
(§6.8: a hosted plugin holds no hooks and no MCP servers) and no dynamic context injection.
The parse here is not a YAML parser: the agent still runs ``vetPackage`` at every load,
so a gap here fails closed there.

The content hash is ``hash.ts``'s ``hashOfFiles`` over the unpacked tree, so the agent
checks a hosted pin as it checks a git one; ``tests/hosted_plugins/test_hash_vectors.py``
pins the two together.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from collections.abc import Mapping
from typing import Any

from scadbuddy.hosted_plugins.rules import (
    MANIFEST_PATH,
    PathRefusedError,
    check_executable,
    check_path,
    sort_key,
)

#: A draft or a tree: each path's bytes and executable bit.
Files = Mapping[str, tuple[bytes, bool]]

#: Every entry's time: the earliest a zip can hold (MS-DOS dates start in 1980).
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_UNIX = 3

#: The frontmatter keys a hosted plugin may not set (``vet.ts`` header).
REFUSED_KEYS = ("hooks", "mcpServers", "permissionMode")

# ``vet.ts`` FM_CLI, FM_OPEN, FM_LINES and TOP_LEVEL_LINE, as Claude Code cuts the block.
_FM_CLI = re.compile(r"^---\s*\n([\s\S]*?)---\s*\n?")
_FM_OPEN = re.compile(r"^---\s*\n")
_FM_LINES = re.compile(r"^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)")
_TOP_LEVEL_LINE = re.compile(r"^(?:$|#|-(?:[ \t]|$)|[A-Za-z0-9_][A-Za-z0-9_.-]*:(?:[ \t]|$))")
_KEY_LINE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:[ \t]+(.*))?$")
# ``vet.ts`` INJECTION_INLINE and INJECTION_BLOCK.
_INJECTION_INLINE = re.compile(r"(^|\s)!`", re.MULTILINE)
_INJECTION_BLOCK = re.compile(r"(```|~~~)!")


class FrontmatterError(ValueError):
    pass


def _js_trim(text: str) -> str:
    """JavaScript's ``trim``: Unicode white space and the byte-order mark."""
    return text.strip().strip("\ufeff").strip()


def _scalar(inline: str, more: list[str]) -> str | None:
    """A key's value as a string, or None when it is not one (a list, a mapping)."""
    value = re.sub(r"[ \t]+#.*$", "", inline).strip()
    if value[:1] in ("&", "*", "!"):
        raise FrontmatterError(
            "frontmatter uses a YAML form a plugin may not use (an anchor, alias or tag);"
            ' write plain "key: value" lines'
        )
    if value[:1] in ("|", ">"):
        lines = [line.strip() for line in more]
        return ("\n" if value[0] == "|" else " ").join(lines).strip() or None
    if more and any(line.lstrip().startswith("- ") or line == "-" for line in more):
        return None
    if value[:1] in ("[", "{"):
        return None
    if value[:1] == '"':
        if len(value) < 2 or not value.endswith('"'):
            raise FrontmatterError("frontmatter has an unclosed quote")
        try:
            loaded = json.loads(value)
        except ValueError:
            raise FrontmatterError("frontmatter has a quoted value it cannot read") from None
        return loaded if isinstance(loaded, str) else None
    if value[:1] == "'":
        if len(value) < 2 or not value.endswith("'"):
            raise FrontmatterError("frontmatter has an unclosed quote")
        return value[1:-1].replace("''", "'")
    joined = " ".join([value, *(line.strip() for line in more)]).strip()
    return joined or None


def frontmatter(text: str) -> dict[str, str | None] | None:
    """The frontmatter's top-level keys, each with its value when it is a string; None when
    Claude Code would see no frontmatter. `FrontmatterError` for a form ``vet.ts`` refuses."""
    body = text[1:] if text.startswith("﻿") else text
    match = _FM_CLI.match(body)
    if match is None:
        if _FM_OPEN.match(body):
            raise FrontmatterError('frontmatter has an opening "---" with no closing "---"')
        return None
    block = match.group(1)
    lined = _FM_LINES.match(body)
    lined_block = lined.group(1) if lined else None
    if (_js_trim(block) or _js_trim(lined_block or "")) and (
        lined_block is None or _js_trim(lined_block) != _js_trim(block)
    ):
        raise FrontmatterError('frontmatter has a "---" that is not on a line of its own')
    keys: dict[str, str | None] = {}
    current: str | None = None
    inline = ""
    more: list[str] = []

    def close() -> None:
        if current is not None:
            keys[current] = _scalar(inline, more)

    for line in re.split(r"\r?\n", block):
        if line.startswith(" ") or (line.startswith("-") and current is not None):
            if current is not None:
                more.append(line)
            continue
        if not _TOP_LEVEL_LINE.match(line):
            raise FrontmatterError(
                f"frontmatter uses a YAML form a plugin may not use (the line"
                f' {json.dumps(line[:40])}); write plain "key: value" lines'
            )
        if line == "" or line.startswith("#"):
            continue
        key = _KEY_LINE.match(line)
        if key is None:
            raise FrontmatterError("frontmatter has a list item with no key above it")
        close()
        current, inline, more = key.group(1), key.group(2) or "", []
        if current in keys:
            raise FrontmatterError(f'frontmatter sets "{current}" twice')
    close()
    return keys


def _markdown(path: str, data: bytes, problems: list[str]) -> dict[str, str | None] | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        problems.append(f"{path} is not valid UTF-8")
        return None
    if _INJECTION_INLINE.search(text) or _INJECTION_BLOCK.search(text):
        problems.append(f"{path}: runs a shell command through dynamic context injection (!`...`)")
    try:
        fm = frontmatter(text)
    except FrontmatterError as err:
        problems.append(f"{path}: {err}")
        return None
    for key in REFUSED_KEYS:
        if fm is not None and key in fm:
            problems.append(f'{path}: frontmatter "{key}" is not allowed in a hosted plugin')
    return fm


def _needs(path: str, fm: dict[str, str | None] | None, keys: tuple[str, ...]) -> list[str]:
    if fm is None:
        return [f"{path} has no frontmatter; it needs {' and '.join(keys)}"]
    return [f'{path}: frontmatter needs a "{k}" text' for k in keys if not fm.get(k)]


def problems(files: Files) -> list[str]:
    """Everything the publish refuses in the draft ``files``; empty when it may publish."""
    found: list[str] = []
    if not files:
        return ["the draft is empty: add a skill, an agent or a command"]
    skills: set[str] = set()
    for path in sorted(files, key=sort_key):
        data, executable = files[path]
        try:
            check_path(path)
            check_executable(path, executable=executable)
        except PathRefusedError as err:
            found.append(f"{path}: {err}")
            continue
        top, *rest = path.split("/")
        markdown = path.lower().endswith(".md")
        if top == "skills":
            if len(rest) < 2:
                found.append(f"{path}: a skill's files go in skills/<skill>/")
                continue
            skills.add(rest[0])
        elif not markdown:
            found.append(f"{path}: {top}/ holds Markdown (.md) files only")
            continue
        if not markdown:
            continue
        sub: list[str] = []
        fm = _markdown(path, data, sub)
        found.extend(sub)
        needs_names = top == "agents" or (top == "skills" and rest[1:] == ["SKILL.md"])
        if needs_names and not sub:
            found.extend(_needs(path, fm, ("name", "description")))
    for skill in sorted(skills, key=sort_key):
        if f"skills/{skill}/SKILL.md" not in files:
            found.append(f"skills/{skill}/ has no skills/{skill}/SKILL.md")
    return list(dict.fromkeys(found))


def plugin_json(name: str, description: str, version: int) -> bytes:
    """The generated ``.claude-plugin/plugin.json`` of version ``version``."""
    manifest = {"name": name, "description": description, "version": f"{version}.0.0"}
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def published_tree(
    files: Files, *, name: str, description: str, version: int
) -> dict[str, tuple[bytes, bool]]:
    """The version's tree: the draft and its generated manifest."""
    return {**files, MANIFEST_PATH: (plugin_json(name, description, version), False)}


def file_list(files: Files) -> dict[str, dict[str, Any]]:
    """``hash.ts`` ``FileList``: each path's sha256, size and executable bit, in its order."""
    return {
        path: {
            "sha256": hashlib.sha256(files[path][0]).hexdigest(),
            "size": len(files[path][0]),
            "executable": files[path][1],
        }
        for path in sorted(files, key=sort_key)
    }


def content_hash(listed: Mapping[str, Mapping[str, Any]]) -> str:
    """``hash.ts`` ``hashOfFiles``: the SHA-256 of ``<path>\\0<x|->\\0<sha256>\\n`` per file."""
    text = "".join(
        f"{path}\0{'x' if listed[path]['executable'] else '-'}\0{listed[path]['sha256']}\n"
        for path in sorted(listed, key=sort_key)
    )
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_zip(files: Files) -> bytes:
    """The tree as a zip whose bytes depend on the tree only: sorted paths, one fixed time,
    mode 0644 or 0755 on a regular file, no folder entries and no extra fields."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for path in sorted(files, key=sort_key):
            data, executable = files[path]
            info = zipfile.ZipInfo(path, date_time=ZIP_DATE_TIME)
            info.create_system = _UNIX
            info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info.extra = b""
            zf.writestr(info, data, compresslevel=9)
    return out.getvalue()


def diff_files(
    before: Mapping[str, Mapping[str, Any]], after: Mapping[str, Mapping[str, Any]]
) -> dict[str, list[str]]:
    """``hash.ts`` ``diffFiles``: what ``after`` adds, removes and changes (bytes or mode)."""
    added = [p for p in sorted(after, key=sort_key) if p not in before]
    changed = [
        p
        for p in sorted(after, key=sort_key)
        if p in before
        and (
            before[p]["sha256"] != after[p]["sha256"]
            or before[p]["executable"] != after[p]["executable"]
        )
    ]
    removed = [p for p in sorted(before, key=sort_key) if p not in after]
    return {"added": added, "removed": removed, "changed": changed}
