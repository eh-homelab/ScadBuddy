"""File writes that a concurrent reader never sees half-done."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, data: bytes) -> None:
    """Swap ``data`` in at ``path``: a reader sees the old file or the new one, never
    a torn one. The temp file is in the same directory, so ``os.replace`` stays on one
    filesystem and stays atomic. :class:`FileNotFoundError` when the directory is gone.
    """
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=path.suffix)
    try:
        with os.fdopen(handle, "wb") as writer:
            writer.write(data)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise
