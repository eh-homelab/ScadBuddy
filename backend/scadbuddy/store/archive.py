"""A directory as one blob: how a piece, a snapshot or a font family travels through a
remote store (spec 2026-09-27 §6.3). Deviation from §6.3's `piece-….3mf`: a phase-1
piece is a directory (model 3MF, GLB, plates, `piece.json`), so its blob is a zip of
that directory; a per-colour-objects 3MF Part arrives with phase 5's manifests."""

from __future__ import annotations

import errno
import io
import os
import shutil
import uuid
import zipfile
from pathlib import Path

#: The sha256 of the blob a cached directory holds exactly: written only by `unpack_dir`
#: and by a successful publish, and removed before a stage writes into the directory.
MARKER = ".blob-sha256"
#: How many times a swap is retried when another unpack of the same directory lands
#: between moving the old one aside and moving the new one in.
_SWAP_ATTEMPTS = 8
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
    """Replace ``directory`` with the archive's content; refuse any entry that would land
    outside it. The old directory is moved aside (a dot-name) before the new one moves
    in and is removed after, so two unpacks of one directory never fail each other and
    a reader never finds a half-removed tree."""
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
        _swap_in(staging, directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _swap_in(staging: Path, directory: Path) -> None:
    aside: list[Path] = []
    try:
        for _ in range(_SWAP_ATTEMPTS):
            old = directory.with_name(f".{directory.name}.old.{uuid.uuid4().hex}")
            try:
                os.rename(directory, old)
                aside.append(old)
            except FileNotFoundError:
                pass  # nothing there, or another unpack moved it aside first
            try:
                os.replace(staging, directory)
                return
            except OSError as error:
                # Another unpack's directory moved in meanwhile: move it aside too.
                if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
        raise OSError(errno.EBUSY, f"could not swap in {directory} after {_SWAP_ATTEMPTS} tries")
    finally:
        for old in aside:
            shutil.rmtree(old, ignore_errors=True)


def read_marker(directory: Path) -> str | None:
    try:
        return (directory / MARKER).read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def clear_marker(directory: Path) -> None:
    """The directory no longer holds exactly a published blob: a stage writes into it."""
    (directory / MARKER).unlink(missing_ok=True)


def write_marker(directory: Path, sha256: str) -> None:
    staging = directory / f"{MARKER}.{uuid.uuid4().hex}"
    staging.write_text(sha256, encoding="ascii")
    os.replace(staging, directory / MARKER)
