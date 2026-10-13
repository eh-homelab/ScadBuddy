"""A plugin package's content hash: agent/src/plugins/packages/hash.ts ``hashTree`` in Python.

A durable turn's packages are copied by the agent to the payload volume both sidecars
mount (agent/src/temporal/turnPackages.ts, #2320). The volume is shared, so the segment
runner hashes each copy again before it loads it, as the agent hashes its own cache
before a classic turn loads it, and loads only one that hashes to its pin.

The hash is the SHA-256 of a canonical file list: one line per regular file, sorted by
path, ``<path>\\0<x|->\\0<sha256 of the bytes>\\n``, where ``x`` marks an executable
file. Anything that is not a regular file or a directory (a symlink above all) is
refused, as are packages over the same limits.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

MAX_FILES = 2000
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024


class PackageContentError(Exception):
    pass


def _walk(root: Path, rel: str, out: list[tuple[str, Path]]) -> None:
    with os.scandir(root / rel if rel else root) as entries:
        for entry in entries:
            rel_path = f"{rel}/{entry.name}" if rel else entry.name
            if entry.is_dir(follow_symlinks=False):
                _walk(root, rel_path, out)
            elif entry.is_file(follow_symlinks=False):
                out.append((rel_path, Path(entry.path)))
                if len(out) > MAX_FILES:
                    raise PackageContentError(f"the package has more than {MAX_FILES} files")
            else:
                raise PackageContentError(f"{rel_path} is not a regular file")


def hash_tree(root: str | os.PathLike[str]) -> str:
    """``sha256:<hex>`` of every file under ``root``; raises PackageContentError."""
    found: list[tuple[str, Path]] = []
    _walk(Path(root), "", found)
    # hash.ts sorts by UTF-16 code units; package paths are ASCII in practice, and any
    # path where the two orders differ only fails the comparison, never passes it.
    lines: list[str] = []
    total = 0
    for rel, path in sorted(found, key=lambda f: f[0]):
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise PackageContentError(f"{rel} changed while it was read")
        if st.st_size > MAX_FILE_BYTES:
            raise PackageContentError(f"{rel} is larger than {MAX_FILE_BYTES} bytes")
        total += st.st_size
        if total > MAX_TOTAL_BYTES:
            raise PackageContentError(f"the package is larger than {MAX_TOTAL_BYTES} bytes")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{rel}\0{'x' if st.st_mode & 0o111 else '-'}\0{digest}\n")
    return "sha256:" + hashlib.sha256("".join(lines).encode()).hexdigest()
