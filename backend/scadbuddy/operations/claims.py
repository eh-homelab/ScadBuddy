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

A claim's name changes (a put links or replaces it, a release or the sweep removes it)
only under the store's lock, a file beside the root (`ClaimStore._locked`, #7947). So a
release or the sweep checks a claim and removes it with no put between the two, and
never moves a claim aside: a run reading the claim another put renewed never finds it
missing.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

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

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """The store's lock, held for one change of a claim's name (#7947). It is a file
        beside the root, not in it, so a listing of the claims is only claims; opened for
        writing, as an exclusive ``flock`` on NFS needs (the API's ``/data``)."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root.with_name(f"{self.root.name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

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
            with self._locked():
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

    def hold_file(self, path: Path, digest: str) -> Held:
        """``hold`` for bytes already written to ``path`` on this volume, whose sha256
        is ``digest``: a media upload is streamed, never read into memory. ``path`` is
        moved in, and is gone afterwards whether or not the claim was held already."""
        target = self._path(digest)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            written = path.stat()
            with self._locked():
                try:
                    os.link(path, target)
                except FileExistsError:
                    os.replace(path, target)
                    return Held(digest, written.st_ino, written.st_mtime_ns, created=False)
            path.unlink()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return Held(digest, written.st_ino, written.st_mtime_ns, created=True)

    def get(self, name: str) -> bytes:
        try:
            return self._path(name).read_bytes()
        except FileNotFoundError:
            raise LookupError(f"claim {name} is gone") from None

    def open(self, name: str) -> BinaryIO:
        """The claim ``name`` open for reading, for a run that streams it on rather than
        reading it whole. The open file outlives a release or sweep of the claim."""
        try:
            return self._path(name).open("rb")
        except FileNotFoundError:
            raise LookupError(f"claim {name} is gone") from None

    def link(self, name: str, target: Path) -> None:
        """Link the claim ``name`` at ``target`` on this volume, for a run that moves
        the bytes on (a media upload into ``media/``) while the claim stays."""
        try:
            os.link(self._path(name), target)
        except FileNotFoundError:
            raise LookupError(f"claim {name} is gone") from None

    def release(self, held: Held) -> None:
        """Remove the claim ``held`` created, unless a put has written it since; a claim
        already gone is no error. The check and the removal are one step under the lock
        (#7947): a claim another put renewed is left where it is, never moved, as a run of
        that put may be reading it (#7853)."""
        if not held.created:
            return
        path = self._path(held.name)
        with self._locked():
            try:
                found = os.stat(path)
            except FileNotFoundError:
                return
            if (found.st_ino, found.st_mtime_ns) == (held.inode, held.mtime_ns):
                path.unlink()

    def sweep(self, max_age: timedelta = CLAIM_MAX_AGE) -> int:
        """Remove the claims (and a crashed put's temporary files) older than ``max_age``.

        Each is checked and removed under the lock (#7947), so a put that renews it is
        either before the check, and the claim is kept, or after the removal, and writes
        it anew (review 3c 1.4)."""
        cutoff = time.time() - max_age.total_seconds()
        removed = 0
        try:
            entries = list(self.root.iterdir())
        except FileNotFoundError:
            return 0
        for path in entries:
            with self._locked():
                try:
                    if os.stat(path).st_mtime >= cutoff:
                        continue
                    path.unlink()
                except FileNotFoundError:
                    continue
            removed += 1
        return removed
