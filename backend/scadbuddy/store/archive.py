"""A directory as one blob: how a piece, a snapshot or a font family travels through a
remote store (spec 2026-09-27 §6.3). Deviation from §6.3's `piece-….3mf`: a phase-1
piece is a directory (model 3MF, GLB, plates, `piece.json`), so its blob is a zip of
that directory; a per-colour-objects 3MF Part arrives with phase 5's manifests."""

from __future__ import annotations

import io
import os
import shutil
import uuid
import zipfile
from pathlib import Path

#: The sha256 of the blob a cached directory was last published as or fetched from.
MARKER = ".blob-sha256"
_EPOCH = (1980, 1, 1, 0, 0, 0)


def pack_dir(directory: Path) -> bytes:
    """Every regular file under ``directory`` except dot-named ones, in name order with
    fixed timestamps, so the same content always packs to the same bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            rel = path.relative_to(directory)
            if path.is_symlink() or not path.is_file() or any(p.startswith(".") for p in rel.parts):
                continue
            info = zipfile.ZipInfo(rel.as_posix(), date_time=_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def unpack_dir(data: bytes, directory: Path, *, sha256: str | None = None) -> None:
    """Replace ``directory`` with the archive's content, atomically; refuse any entry
    that would land outside it."""
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = directory.with_name(f".{directory.name}.{uuid.uuid4().hex}")
    staging.mkdir()
    root = staging.resolve()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                target = (staging / info.filename).resolve()
                if info.filename.startswith("/") or not target.is_relative_to(root):
                    raise ValueError(
                        f"refused an archive entry outside its directory: {info.filename!r}"
                    )
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
        if sha256 is not None:
            (staging / MARKER).write_text(sha256, encoding="ascii")
        if directory.exists():
            shutil.rmtree(directory)
        os.replace(staging, directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def read_marker(directory: Path) -> str | None:
    try:
        return (directory / MARKER).read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def write_marker(directory: Path, sha256: str) -> None:
    staging = directory / f"{MARKER}.{uuid.uuid4().hex}"
    staging.write_text(sha256, encoding="ascii")
    os.replace(staging, directory / MARKER)
