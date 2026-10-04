"""Claim checks for request bytes too large for a workflow's history (#1054).

A route puts a request's large part (a model's source, its thumbnail) here and sends
the operation its name instead; the run reads it back. Temporal limits a payload to
2 MB and warns from 512 KB, and a source may be 1M characters, a thumbnail 10 MB.

A claim is named by the sha256 of its bytes, so a repeated request carries the same
name and reaches the same operation key. Nothing removes a claim when its run ends --
another request may hold the same bytes -- so the housekeeping sweep removes those
nothing has put for ``CLAIM_MAX_AGE``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import time
from datetime import timedelta
from pathlib import Path

#: Far past any run's timeout: a claim swept under a running operation fails it.
CLAIM_MAX_AGE = timedelta(days=1)

_NAME = re.compile(r"[0-9a-f]{64}")


class ClaimStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a claim")
        return self.root / name

    def put(self, data: bytes) -> str:
        name = hashlib.sha256(data).hexdigest()
        path = self.root / name
        if path.exists():
            # Renewed: the sweep counts from the last request that needed it.
            os.utime(path)
            return name
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".claim-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return name

    def get(self, name: str) -> bytes:
        try:
            return self._path(name).read_bytes()
        except FileNotFoundError:
            raise LookupError(f"claim {name} is gone") from None

    def sweep(self, max_age: timedelta = CLAIM_MAX_AGE) -> int:
        """Remove the claims (and a crashed put's temporary files) older than ``max_age``."""
        cutoff = time.time() - max_age.total_seconds()
        removed = 0
        try:
            entries = list(self.root.iterdir())
        except FileNotFoundError:
            return 0
        for path in entries:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
        return removed
