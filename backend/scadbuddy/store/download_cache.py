"""Files downloaded from Bambuddy, kept by the SHA-256 of their bytes (#2788).

One directory of whole files, each named by its digest, on the API's data volume and
so shared by its replicas: a library file one read downloaded is here for the next
read of the same bytes, and a blob one replica stored or fetched for the next fetch.
Render workers run template code, so they are never given this directory: anything
that can write it could fill the volume (#2788).

Content addressing is what makes sharing safe without an index:

* An entry is only ever renamed in after its digest was checked against its name, so
  a changed file is a new name, not a stale entry.
* Every read uses bytes whose digest it checked: a private copy of the entry
  (:meth:`DownloadCache.private_copy`), made before the first byte is read, so nothing
  that can write the shared directory can change them after the check. An entry that
  is not its name is dropped and read as a miss: a fault in the cache stays in the
  cache, never reported as a bad blob or a bad library file.
* Two processes filling the same entry each write a file of their own beside it and
  rename it in; the second rename replaces identical bytes.
* Eviction (:func:`prune`) drops the least recently used entries past a byte cap. An
  entry used within ``min_age`` is never dropped, so a read that is under way keeps
  its file; past the cap that only means the directory runs over it for a while.

The same pruning keeps the library's derived caches (``library-views/``,
``library-filaments/``, ``library-objects/``) to a count; they hold what was read
from a download, this holds the download.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from scadbuddy.core.tracing import span

logger = logging.getLogger(__name__)

#: Where the API keeps the downloads when ``SCADBUDDY_DOWNLOAD_CACHE_DIR`` is not set,
#: under the data volume's cache.
DOWNLOADS_DIRNAME = "downloads"
#: An unfinished fill older than this is a process that died mid-download: removed.
PARTIAL_MAX_AGE = 3600.0
#: Free space a fill or a private copy leaves on its volume: the cache is never what
#: fills it (the library spool keeps the same reserve, #2788).
RESERVE_BYTES = 1024**3
#: How much a fill writes between two looks at the volume's free space.
_ROOM_CHECK_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1024 * 1024


def is_sha256(value: str | None) -> bool:
    return value is not None and _SHA256.fullmatch(value) is not None


def prune(
    directory: Path,
    *,
    keep: int | None = None,
    max_bytes: int | None = None,
    min_age: float = 0,
) -> None:
    """Drop the least recently used entries of ``directory`` (by mtime: a hit touches its
    entry, :func:`touch`) until at most ``keep`` remain and they hold at most
    ``max_bytes``. An entry used within ``min_age`` seconds stays whatever the bounds.
    Dot-files are fills in progress and are left, unless older than
    :data:`PARTIAL_MAX_AGE`."""
    now = time.time()
    used: list[tuple[float, int, Path]] = []
    try:
        listing = list(directory.iterdir())
    except FileNotFoundError:
        return
    for entry in listing:
        try:
            stat = entry.stat()
        except FileNotFoundError:
            continue  # another prune removed it between the listing and the stat
        if entry.name.startswith("."):
            if now - stat.st_mtime > PARTIAL_MAX_AGE:
                # A file still open on NFS is a `.nfsXXXX` silly-rename that cannot be
                # removed (EBUSY): it goes once closed, so a prune never fails on it.
                with contextlib.suppress(OSError):
                    entry.unlink(missing_ok=True)
            continue
        used.append((stat.st_mtime, stat.st_size, entry))
    used.sort(key=lambda item: item[0], reverse=True)
    total = 0
    for index, (mtime, size, entry) in enumerate(used):
        total += size
        over = (keep is not None and index >= keep) or (max_bytes is not None and total > max_bytes)
        if over and now - mtime >= min_age:
            with contextlib.suppress(OSError):
                entry.unlink(missing_ok=True)


def touch(path: Path) -> None:
    """Mark an entry used, so :func:`prune` keeps it. One pruned meanwhile is not."""
    with contextlib.suppress(FileNotFoundError):
        os.utime(path)


def store_bytes(path: Path, data: bytes, *, keep: int) -> None:
    """Write ``path`` aside and rename it in, then keep its directory to ``keep``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    partial.write_bytes(data)
    partial.replace(path)
    prune(path.parent, keep=keep)


class TooLargeError(Exception):
    """A fill past :attr:`DownloadCache.max_entry_bytes`: not kept, whatever its source
    said its size was."""


class NoRoomError(TooLargeError):
    """A fill that would take the volume into :attr:`DownloadCache.reserve_bytes`: not
    kept either, and handled where a fill too large is."""


def _free(fd: int) -> int:
    stat = os.fstatvfs(fd)
    return stat.f_bavail * stat.f_frsize


class Fill:
    """One download being written into the cache: :meth:`write` its bytes, then
    :meth:`commit`. The file is the cache's own until then, under a name no reader
    looks for; one that is never committed is removed when the fill closes."""

    def __init__(self, cache: DownloadCache, sha256: str, out: BinaryIO, partial: Path) -> None:
        self.cache = cache
        self.sha256 = sha256
        self.file = out
        self.partial = partial
        self._digest = hashlib.sha256()
        self._size = 0
        self._room_checked_at: int | None = None
        self.committed = False

    def write(self, chunk: bytes) -> int:
        """Counted against :attr:`DownloadCache.max_entry_bytes` as it is written, so a
        size the source understated cannot fill the volume; and stopped as
        :class:`NoRoomError` before it eats the volume's reserve."""
        self._size += len(chunk)
        if self._size > self.cache.max_entry_bytes:
            raise TooLargeError
        checked = self._room_checked_at
        if checked is None or self._size - checked >= _ROOM_CHECK_BYTES:
            self._room_checked_at = self._size
            if not self.cache.has_room(self.file.fileno(), _ROOM_CHECK_BYTES):
                raise NoRoomError
        self._digest.update(chunk)
        return self.file.write(chunk)

    def tell(self) -> int:
        return self.file.tell()

    def matches(self) -> bool:
        return self._digest.hexdigest() == self.sha256

    def commit(self) -> bool:
        """Rename the download in if its bytes are the ones named; False (and nothing
        kept) when they are not, e.g. a file Bambuddy changed after stating its hash."""
        self.file.flush()
        if not self.matches():
            logger.info(
                "a download was not the sha256 it was kept under; not kept",
                extra={"sha256": self.sha256},
            )
            return False
        # On disk before it has a name a reader finds: after a crash an entry is whole,
        # never a renamed file whose last blocks were never written.
        os.fsync(self.file.fileno())
        self.partial.replace(self.cache.path(self.sha256))
        self.committed = True
        self.cache.prune()
        return True


@dataclass
class DownloadCache:
    root: Path
    #: The cap ``prune`` keeps the directory to (SCADBUDDY_DOWNLOAD_CACHE_MAX_BYTES).
    max_bytes: int
    #: Never evict an entry used this recently: a read of it may be under way.
    min_age: float = 600.0
    #: Free space no fill or private copy takes (:data:`RESERVE_BYTES`).
    reserve_bytes: int = RESERVE_BYTES

    @property
    def max_entry_bytes(self) -> int:
        """The largest file kept: a quarter of the cap, so one file never evicts most of
        the others."""
        return self.max_bytes // 4

    def path(self, sha256: str) -> Path:
        return self.root / sha256

    def has_room(self, fd: int, size: int) -> bool:
        """Whether ``size`` more bytes fit on ``fd``'s volume above the reserve."""
        return _free(fd) - size >= self.reserve_bytes

    def open(self, sha256: str | None, *, what: str) -> BinaryIO | None:
        """The entry for ``sha256``, open at its start and marked used; None on a miss.
        A span says which, so a slow read shows in a trace as a miss, not a gap."""
        with span("download_cache.lookup", attributes={"scadbuddy.cache.what": what}) as current:
            if not is_sha256(sha256):
                current.set_attribute("scadbuddy.cache.hit", False)
                return None
            assert sha256 is not None
            path = self.path(sha256)
            try:
                handle = path.open("rb")
            except FileNotFoundError:
                current.set_attribute("scadbuddy.cache.hit", False)
                return None
            touch(path)
            current.set_attribute("scadbuddy.cache.hit", True)
            current.set_attribute("scadbuddy.cache.bytes", os.fstat(handle.fileno()).st_size)
            return handle

    def copy_verified(self, sha256: str | None, out: BinaryIO, *, what: str) -> bool:
        """Copy the entry for ``sha256`` into ``out``, a file of the caller's own, checking
        its digest as it goes; True, ``out`` at its start, when it was there and whole.
        What a caller then reads is its private copy of checked bytes, so nothing that
        can write the shared directory can change them after the check. An entry whose
        bytes are not its name is dropped and reads as a miss."""
        handle = self.open(sha256, what=what)
        if handle is None:
            return False
        assert sha256 is not None
        if not self.has_room(out.fileno(), os.fstat(handle.fileno()).st_size):
            handle.close()
            return False
        digest = hashlib.sha256()
        with handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
                out.write(chunk)
        out.flush()
        out.seek(0)
        if digest.hexdigest() != sha256:
            self.discard(sha256)
            out.truncate()
            return False
        return True

    def private_copy(self, sha256: str | None, directory: Path, *, what: str) -> BinaryIO | None:
        """:meth:`copy_verified` into a file of its own under ``directory`` that has no
        name, open at its start; the caller closes it. None on a miss, an entry that was
        not its name, no room, or a volume that failed: a fault here is only ever a
        miss, so the caller downloads as if there were no cache."""
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Dot-named in case it is ever given a name: then it is a fill to `prune`.
            out = tempfile.TemporaryFile("w+b", dir=directory, prefix=".")  # noqa: SIM115
        except OSError:
            logger.warning("could not make a private copy", extra={"sha256": sha256}, exc_info=True)
            return None
        try:
            if self.copy_verified(sha256, out, what=what):
                return out
        except OSError:
            logger.warning(
                "could not read a cached download", extra={"sha256": sha256}, exc_info=True
            )
        out.close()
        return None

    def has(self, sha256: str) -> bool:
        return self.path(sha256).is_file()

    def discard(self, sha256: str) -> None:
        """Drop an entry whose bytes did not read back as named."""
        self.path(sha256).unlink(missing_ok=True)
        logger.warning(
            "dropped a download cache entry that was not its sha256", extra={"sha256": sha256}
        )

    @contextlib.contextmanager
    def fill(self, sha256: str) -> Iterator[Fill]:
        """A :class:`Fill` for ``sha256``, its file removed on exit unless committed."""
        self.root.mkdir(parents=True, exist_ok=True)
        partial = self.root / f".{sha256}.{uuid.uuid4().hex}"
        with partial.open("w+b") as out:
            fill = Fill(self, sha256, out, partial)
            try:
                yield fill
            finally:
                if not fill.committed:
                    partial.unlink(missing_ok=True)

    def put(self, sha256: str, data: bytes) -> None:
        """Keep ``data`` under ``sha256`` if it is that hash and small enough."""
        if not is_sha256(sha256) or self.has(sha256) or len(data) > self.max_entry_bytes:
            return
        try:
            with self.fill(sha256) as fill:
                for start in range(0, len(data), _CHUNK):
                    fill.write(data[start : start + _CHUNK])
                fill.commit()
        except NoRoomError:
            pass

    def keep_file(self, sha256: str, source: BinaryIO) -> None:
        """Keep a copy of ``source``, the caller's own file, under ``sha256`` if it is that
        hash and small enough; ``source`` is left at its start. The caller goes on reading
        its own file, never the entry: what is in the directory is only ever a copy."""
        if not is_sha256(sha256) or self.has(sha256):
            return
        # Its size first: a file past the limit is not written out to find that out.
        size = source.seek(0, os.SEEK_END)
        source.seek(0)
        if size > self.max_entry_bytes:
            return
        try:
            with self.fill(sha256) as fill:
                while chunk := source.read(_CHUNK):
                    fill.write(chunk)
                fill.commit()
        except TooLargeError:
            pass
        finally:
            source.seek(0)

    def prune(self) -> None:
        prune(self.root, max_bytes=self.max_bytes, min_age=self.min_age)
