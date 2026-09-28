from __future__ import annotations

import os
import re
import shutil
from contextlib import suppress
from pathlib import Path

_KEY = re.compile(r"[A-Za-z0-9._-]{1,128}")


class LocalBlobStore:
    backend = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        # fullmatch: `$` would also accept a key ending in a newline.
        if not _KEY.fullmatch(key) or key in (".", ".."):
            raise ValueError(f"not a blob key: {key!r}")
        return self.root / key

    def dir_for(self, key: str) -> Path:
        path = self._path(key)
        path.mkdir(parents=True, exist_ok=True)
        os.utime(path, None)
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_dir()

    def remove(self, key: str) -> None:
        """Already gone is fine; any other failure raises, so a sweep never counts a
        blob still on disk as removed."""
        with suppress(FileNotFoundError):
            shutil.rmtree(self._path(key))

    def keys(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def touched_at(self, key: str) -> float:
        return self._path(key).stat().st_mtime
