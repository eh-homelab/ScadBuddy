"""Claim checks for request bytes too large for a workflow's history (#1054).

A route puts a request's large part (a model's source, its thumbnail) here and sends
the operation its name instead; the run reads it back. Temporal limits a payload to
2 MB and warns from 512 KB, and a source may be 1M characters, a thumbnail 10 MB.

A claim is named by the sha256 of its bytes, so a repeated request carries the same
name and reaches the same operation key. The route releases a request's claims once
its answer is final (``api/operations.py``): only one its own put created, and only
while it is still the file that put wrote, so bytes another request put before or
since stay for that request's run. The housekeeping sweep removes the rest once
nothing has put them for ``CLAIM_MAX_AGE``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

#: Far past any run's timeout: a claim swept under a running operation fails it.
CLAIM_MAX_AGE = timedelta(days=1)

_NAME = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Held:
    """What one put left: the claim's name, the file it wrote (its inode and mtime: an
    inode number freed by a later put may be reused), and whether that file created the
    claim (none held those bytes before)."""

    name: str
    inode: int
    mtime_ns: int
    created: bool


class ClaimStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a claim")
        return self.root / name

    def put(self, data: bytes) -> str:
        """Write ``data`` and return its name."""
        return self.hold(data).name

    def hold(self, data: bytes) -> Held:
        """Write ``data`` as a fresh file, even when the claim is held already: the bytes
        are the same, the new file renews it against the sweep and tells a release that
        another put came since, and a sweep that removed it meanwhile cannot fail the
        put."""
        name = hashlib.sha256(data).hexdigest()
        path = self.root / name
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".claim-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                written = os.fstat(handle.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                os.replace(tmp, path)
                return Held(name, written.st_ino, written.st_mtime_ns, created=False)
            os.unlink(tmp)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return Held(name, written.st_ino, written.st_mtime_ns, created=True)

    def get(self, name: str) -> bytes:
        try:
            return self._path(name).read_bytes()
        except FileNotFoundError:
            raise LookupError(f"claim {name} is gone") from None

    def release(self, held: Held) -> None:
        """Remove the claim ``held`` created, unless a put has written it since; a claim
        already gone is no error. It is moved aside first, and put back when what was
        moved is not the file ``held`` wrote."""
        if not held.created:
            return
        path = self._path(held.name)
        aside = self.root / f".released-{uuid.uuid4().hex}"
        try:
            os.rename(path, aside)
        except FileNotFoundError:
            return
        moved = aside.stat()
        if (moved.st_ino, moved.st_mtime_ns) != (held.inode, held.mtime_ns):
            with contextlib.suppress(FileExistsError):
                os.link(aside, path)
        aside.unlink()

    def sweep(self, max_age: timedelta = CLAIM_MAX_AGE) -> int:
        """Remove the claims (and a crashed put's temporary files) older than ``max_age``.

        Each is moved aside before it is removed, and kept when what was moved is not
        what was found old: a put between the two renewed it (review 3c 1.4)."""
        cutoff = time.time() - max_age.total_seconds()
        removed = 0
        try:
            entries = list(self.root.iterdir())
        except FileNotFoundError:
            return 0
        for path in entries:
            aside = self.root / f".swept-{uuid.uuid4().hex}"
            try:
                found = path.stat()
                if found.st_mtime >= cutoff:
                    continue
                os.rename(path, aside)
            except FileNotFoundError:
                continue
            moved = aside.stat()
            if moved.st_ino == found.st_ino and moved.st_mtime < cutoff:
                removed += 1
            else:
                # Renewed: back where it was, unless a later put already put it there.
                with contextlib.suppress(FileExistsError):
                    os.link(aside, path)
            aside.unlink()
        return removed
