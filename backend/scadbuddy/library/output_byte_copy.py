"""The byte copy of the saved outputs' files into the blob store (#7256, outputs-in-
Postgres spec 2026-11-23 §4.5 phase 2, §6.3-§6.5), run at every API start and in every
housekeeping sweep (`workflows/housekeeping.py` `OUTPUT_BYTES_SWEEP`).

It reconciles; it does not only fill. Each run walks the ``outputs`` rows. A row whose
``bytes_at`` is set and whose stamp of the byte files (`bytes_stamp`) is the folder's is
skipped. For every other row, each byte file (``model.3mf``, ``preview.glb``,
``thumbnail.png`` and each ``files/`` entry) is compared with the blob its key names:
the index's sha256 is read first, then the file is hashed. An equal file is not
uploaded. A different one is put with a compare-and-swap on that sha256
(`OutputBytes.replace_file`), so a live writer that moved the key meanwhile keeps its
blob (a live writer writes the file first, then the blob). Each blob the copy put, and
every blob of a row not copied before, is read back from the backend
(`OutputBytes.verify`, Ruling R3). Then the row names the keys, in batches of
`BATCH_BYTES` outputs, one transaction each (`OutputRows.write_byte_copy`).

The thumbnail is the exception to the plain compare-and-swap: a thumbnail upload is
blob first (`OutputStore.put_thumbnail`). So the copy puts a changed thumbnail file
under a staged key, and the thumbnail key takes it only in the batch's transaction,
under the output's thumbnail lock, and only while the row's thumbnail key, the key's
sha256 and the file are all still the ones the copy read (`ThumbnailSwap`). A key a put
wrote on a row not yet copied stays. The object a key gave up is released only after
the commit.

What the copy cannot copy is listed, never guessed: a missing ``model.3mf`` or
``preview.glb`` is ``damaged`` (in the run and in the row), a ``files/`` entry an output
may not hold makes the whole output ``damaged`` and it is not copied, and a read or a
check that fails is ``failed`` and leaves the row's ``bytes_at`` NULL. A stat that fails
with anything but a missing file makes the run ``incomplete``. Before any upload, the
bytes and blobs still to copy are checked against the store's caps; past one, the run
is ``refused`` and uploads nothing. On the `local` store (no content store, Ruling R1)
and on `bambuddy` (spec §6.4) every run is refused.

A run has ``finished_at`` only when nothing was refused, failed or incomplete and it was
not stopped. One replica copies at a time, under a session advisory lock. Every blocking
call (a stat, a hash, a read, an SQL statement) runs in a thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.library.output_bytes import (
    FILES_DIR,
    MODEL_NAME,
    PREVIEW_NAME,
    THUMBNAIL_NAME,
    OutputBytes,
    OutputFileNameError,
    put_names,
)
from scadbuddy.library.output_rows import (
    ByteWrite,
    OutputBlobs,
    OutputFileBlob,
    OutputRows,
    Role,
    ThumbnailSwap,
    output_blob_key,
    output_file_key,
    staged_thumbnail_key,
)
from scadbuddy.library.outputs import OutputStore
from scadbuddy.store.content import ContentStore

logger = logging.getLogger(__name__)

#: The advisory lock's name: ``pg_try_advisory_lock(hashtextextended(BYTES_LOCK, 0))``.
BYTES_LOCK = "scadbuddy:outputs:bytes"

#: Outputs per transaction: prod commits take 1-2 s, so one commit per output is too slow.
BATCH_BYTES = 50

REFUSED_LOCAL = "the local store has no blob store; output bytes stay on the share"
REFUSED_BAMBUDDY = "outputs do not go into Bambuddy's library"
#: A blob the backend does not hold whole when read back.
NOT_WHOLE = "the blob is not whole"

_ROLE_FILES: dict[Role, str] = {
    "model": MODEL_NAME,
    "preview": PREVIEW_NAME,
    "thumbnail": THUMBNAIL_NAME,
}
#: The byte roles every output has; a missing one is ``damaged``.
_REQUIRED: tuple[Role, ...] = ("model", "preview")
_CHUNK = 1 << 20


@dataclass
class ByteCopyResult:
    copied: int = 0
    verified: int = 0
    skipped: int = 0
    #: ``{output_id, role, error}`` for each read or check that failed.
    failed: list[dict[str, Any]] = field(default_factory=list)
    #: ``{output_id, role, error}`` for a missing ``model.3mf`` or ``preview.glb``, and
    #: ``{output_id, role: "files", error, names}`` for ``files/`` entries an output may
    #: not hold.
    damaged: list[dict[str, Any]] = field(default_factory=list)
    #: Why the run copied nothing.
    refused: str | None = None
    #: A stat failed: the run may have missed bytes.
    incomplete: bool = False


def _stat_entry(path: Path, name: str) -> tuple[str, int | None, int | None]:
    try:
        found = path.stat()
    except FileNotFoundError:
        return name, None, None
    return name, found.st_size, found.st_mtime_ns


def bytes_stamp(directory: Path) -> str:
    """A stamp of the folder's byte files: the sha256 of the sorted name, size and
    mtime in nanoseconds of ``model.3mf``, ``preview.glb``, ``thumbnail.png`` and each
    ``files/`` entry, a missing one as absent. A stat or listing that fails with
    anything but a missing file raises (`OSError`). Blocking."""
    entries = [
        _stat_entry(directory / name, name) for name in (MODEL_NAME, PREVIEW_NAME, THUMBNAIL_NAME)
    ]
    try:
        names = sorted(entry.name for entry in os.scandir(directory / FILES_DIR))
    except (FileNotFoundError, NotADirectoryError):
        names = []
    for name in names:
        entries.append(_stat_entry(directory / FILES_DIR / name, f"{FILES_DIR}/{name}"))
    return hashlib.sha256(json.dumps(sorted(entries)).encode()).hexdigest()


def _identity(path: Path) -> tuple[int, int, int] | None:
    """The file's inode, mtime and size; ``None`` when it is gone. Blocking."""
    try:
        found = path.stat()
    except FileNotFoundError:
        return None
    return found.st_ino, found.st_mtime_ns, found.st_size


def _hash_file(path: Path) -> tuple[str, tuple[int, int, int]]:
    """The file's sha256 and its identity, both from one open descriptor. Blocking."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        found = os.fstat(file.fileno())
        if not stat.S_ISREG(found.st_mode):
            raise IsADirectoryError(f"{path} is not a regular file")
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest(), (found.st_ino, found.st_mtime_ns, found.st_size)


@dataclass
class _Work:
    output_id: str
    slug: str
    directory: Path
    stamp: str


@dataclass
class _Plan:
    work: list[_Work] = field(default_factory=list)
    needed_bytes: int = 0
    needed_count: int = 0


def _sizes(directory: Path) -> list[int]:
    """The sizes of the folder's byte files, for the quota; ``files/`` entries only when
    the output may hold them all (otherwise it is not copied). Blocking."""
    sizes: list[int] = []
    for name in (MODEL_NAME, PREVIEW_NAME, THUMBNAIL_NAME):
        try:
            sizes.append((directory / name).stat().st_size)
        except FileNotFoundError:
            continue
    try:
        names = put_names(directory)
    except OutputFileNameError:
        return sizes
    for name in names:
        try:
            sizes.append((directory / FILES_DIR / name).stat().st_size)
        except FileNotFoundError:
            continue
    return sizes


def _plan(
    store: OutputStore,
    states: dict[str, tuple[str, str | None, datetime | None]],
    result: ByteCopyResult,
    stop: threading.Event | None,
) -> _Plan:
    """The outputs to copy, with each folder's stamp taken before any read, and what the
    ones not yet in blobs need of the store. Blocking."""
    plan = _Plan()
    for output_id in sorted(states):
        if stop is not None and stop.is_set():
            break
        slug, stamp, bytes_at = states[output_id]
        directory = store.paths.output_dir(slug, output_id)
        try:
            now = bytes_stamp(directory)
            if bytes_at is not None and stamp == now:
                result.skipped += 1
                continue
            if bytes_at is None:
                sizes = _sizes(directory)
                plan.needed_bytes += sum(sizes)
                plan.needed_count += len(sizes)
        except OSError as error:
            result.incomplete = True
            logger.warning(
                "could not read an output's byte files",
                extra={"output_id": output_id, "error": type(error).__name__},
            )
            continue
        plan.work.append(_Work(output_id, slug, directory, now))
    return plan


def _refusal(content: ContentStore, needed_bytes: int, needed_count: int) -> str | None:
    """Why the store cannot take what the copy needs, naming the numbers, the cap's
    setting and the shortfall; ``None`` when it can. Blocking. A cap of 0 is no cap."""
    usage = content.usage()
    reasons: list[str] = []
    if content.max_total_bytes and usage.bytes + needed_bytes > content.max_total_bytes:
        short = usage.bytes + needed_bytes - content.max_total_bytes
        reasons.append(
            f"copying the outputs' bytes needs {needed_bytes} bytes; the store holds"
            f" {usage.bytes}, and SCADBUDDY_STORE_MAX_TOTAL_BYTES"
            f" ({content.max_total_bytes}) allows {content.max_total_bytes}: short by"
            f" {short} bytes"
        )
    if content.max_count and usage.count + needed_count > content.max_count:
        short = usage.count + needed_count - content.max_count
        reasons.append(
            f"copying the outputs' bytes needs {needed_count} blobs; the store holds"
            f" {usage.count}, and SCADBUDDY_STORE_MAX_COUNT ({content.max_count}) allows"
            f" {content.max_count}: short by {short} blobs"
        )
    return "; ".join(reasons) or None


async def copy_output_bytes(
    store: OutputStore,
    rows: OutputRows,
    content: ContentStore | None,
    pool: ConnectionPool[Connection[DictRow]],
    stop: threading.Event | None = None,
) -> ByteCopyResult | None:
    """One run of the byte copy; ``None`` when another replica runs one. ``stop`` ends
    it between outputs: it writes the batch so far and leaves its run unfinished.

    The lock holds one connection of ``pool`` for the whole run. It is autocommit, so it
    is never idle in a transaction and holds no row lock."""
    lock = await asyncio.to_thread(pool.getconn)
    try:
        got = await asyncio.to_thread(
            lambda: lock.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS got", (BYTES_LOCK,)
            ).fetchone()
        )
        if got is None or not got["got"]:
            logger.info("another replica copies the outputs' bytes; this one does not")
            return None
        try:
            return await _Copy(store, rows, content, stop).run()
        finally:
            try:
                await asyncio.to_thread(
                    lock.execute,
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (BYTES_LOCK,),
                )
            except Exception:
                # Closing the connection ends its session, which frees the lock; the pool
                # drops a closed connection.
                logger.warning("could not release the byte copy's lock; closed its connection")
                with contextlib.suppress(Exception):
                    lock.close()
    finally:
        await asyncio.to_thread(pool.putconn, lock)


class _FailedError(Exception):
    """One byte file of an output could not be copied: the output is ``failed``."""

    def __init__(self, role: str, error: str) -> None:
        super().__init__(error)
        self.role = role
        self.error = error


class _Copy:
    def __init__(
        self,
        store: OutputStore,
        rows: OutputRows,
        content: ContentStore | None,
        stop: threading.Event | None,
    ) -> None:
        self.store = store
        self.rows = rows
        self.content = content
        self.stop = stop
        self.result = ByteCopyResult()
        self.pending: list[ByteWrite] = []

    def _stopped(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    async def run(self) -> ByteCopyResult:
        rows, result = self.rows, self.result
        run_id = await asyncio.to_thread(rows.start_byte_run)
        needed_bytes = needed_count = 0
        #: Whether the run went to its end: not stopped, and nothing raised.
        ended = False
        try:
            if self.content is None:
                result.refused = REFUSED_LOCAL
                return result
            if self.content.name == "bambuddy":
                result.refused = REFUSED_BAMBUDDY
                return result
            content = self.content
            blobs = OutputBytes(content)
            states = await asyncio.to_thread(rows.byte_states)
            plan = await asyncio.to_thread(_plan, self.store, states, result, self.stop)
            needed_bytes, needed_count = plan.needed_bytes, plan.needed_count
            result.refused = await asyncio.to_thread(_refusal, content, needed_bytes, needed_count)
            if result.refused is not None:
                logger.warning("the byte copy is refused", extra={"refused": result.refused})
                return result
            for work in plan.work:
                if self._stopped():
                    break
                write = await self._copy_one(blobs, work)
                if write is not None:
                    self.pending.append(write)
                if len(self.pending) >= BATCH_BYTES:
                    await self._write(blobs)
            await self._write(blobs)
            ended = not self._stopped()
            return result
        finally:
            finished = ended and not (
                result.refused is not None or result.failed or result.incomplete
            )
            await asyncio.to_thread(
                rows.end_byte_run,
                run_id,
                finished=finished,
                incomplete=result.incomplete,
                refused=result.refused,
                copied=result.copied,
                verified=result.verified,
                skipped=result.skipped,
                needed_bytes=needed_bytes,
                needed_count=needed_count,
                failed=result.failed,
                damaged=result.damaged,
            )

    async def _write(self, blobs: OutputBytes) -> None:
        """Write the batch in one transaction, then release what its thumbnail swaps
        replaced. An output it does not write was deleted, or a live writer put its bytes
        meanwhile: the blobs the copy put for it stay unheld for the sweep."""
        if not self.pending:
            return
        batch, self.pending = self.pending, []
        written, replaced = await asyncio.to_thread(self.rows.write_byte_copy, batch)
        self.result.copied += sum(
            1 for write in batch if write.copied and write.output_id in written
        )
        for key, previous in replaced:
            await blobs.release_replaced(key, previous)

    async def _verify(self, blobs: OutputBytes, key: str, role: str) -> None:
        if not await blobs.verify(key):
            raise _FailedError(role, NOT_WHOLE)
        self.result.verified += 1

    async def _role(
        self, blobs: OutputBytes, key: str, path: Path, slug: str, role: str, check: bool
    ) -> bool | None:
        """Copy one byte file to ``key``: ``None`` when the file is missing, else whether
        the copy moved the key to another object."""
        ref = await asyncio.to_thread(blobs.ref, key)
        expected = None if ref is None else ref.sha256
        try:
            sha, _ = await asyncio.to_thread(_hash_file, path)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _FailedError(role, str(error)) from error
        moved = False
        if sha != expected:
            try:
                landed = await blobs.replace_file(key, path, slug=slug, expected=expected)
            except FileNotFoundError:
                return None
            except Exception as error:
                logger.warning(
                    "could not copy an output's byte file", extra={"key": key}, exc_info=True
                )
                raise _FailedError(role, str(error)) from error
            # None: a live writer moved the key since the read, and its blob stays.
            moved = landed is not None
        if moved or check:
            await self._verify(blobs, key, role)
        return moved

    async def _thumbnail(
        self, blobs: OutputBytes, work: _Work, read: str | None, check: bool
    ) -> tuple[str | None, ThumbnailSwap | None]:
        """The thumbnail key the row should name, and the swap that makes the key take a
        changed file. With no file, the key the row names now stays."""
        output_id, path = work.output_id, work.directory / THUMBNAIL_NAME
        key = output_blob_key(output_id, "thumbnail")
        ref = await asyncio.to_thread(blobs.ref, key)
        expected = None if ref is None else ref.sha256
        try:
            sha, identity = await asyncio.to_thread(_hash_file, path)
        except FileNotFoundError:
            return read, None
        except OSError as error:
            raise _FailedError("thumbnail", str(error)) from error
        if sha == expected:
            if check:
                await self._verify(blobs, key, "thumbnail")
            return key, None
        staged_key = staged_thumbnail_key(output_id, sha)
        try:
            staged = await blobs.put_file(staged_key, path, slug=work.slug)
        except FileNotFoundError:
            return read, None
        except Exception as error:
            logger.warning(
                "could not copy an output's thumbnail", extra={"key": key}, exc_info=True
            )
            raise _FailedError("thumbnail", str(error)) from error
        await self._verify(blobs, staged_key, "thumbnail")
        swap = ThumbnailSwap(
            staged=staged,
            expected=expected,
            slug=work.slug,
            unchanged=lambda: _identity(path) == identity,
        )
        return key, swap

    async def _copy_one(self, blobs: OutputBytes, work: _Work) -> ByteWrite | None:
        """One output's bytes, uploaded and checked; ``None`` when there is nothing to
        write (the row went, or a file failed)."""
        output_id, directory, result = work.output_id, work.directory, self.result
        try:
            names = await asyncio.to_thread(put_names, directory)
        except OutputFileNameError as error:
            entry = {"role": "files", "error": str(error), "names": list(error.names)}
            result.damaged.append({"output_id": output_id, **entry})
            row = await asyncio.to_thread(self.rows.get, output_id)
            if row is None:
                return None
            return ByteWrite(
                output_id,
                OutputBlobs(),
                work.stamp,
                [entry],
                guarded=True,
                bytes_at=row.bytes_at,
                copied=False,
            )
        except OSError as error:
            result.failed.append({"output_id": output_id, "role": "files", "error": str(error)})
            return None
        row = await asyncio.to_thread(self.rows.get, output_id)
        if row is None:
            return None
        # A row not copied before has every blob read back, uploaded or not.
        check = row.bytes_at is None
        damaged: list[dict[str, Any]] = []
        keys: dict[str, str | None] = {}
        model_moved = False
        try:
            for role in _REQUIRED:
                key = output_blob_key(output_id, role)
                name = _ROLE_FILES[role]
                moved = await self._role(blobs, key, directory / name, work.slug, role, check)
                if moved is None:
                    damaged.append({"role": role, "error": f"no {name}"})
                    keys[role] = None
                    continue
                keys[role] = key
                if role == "model":
                    model_moved = moved
            thumbnail, swap = await self._thumbnail(blobs, work, row.blobs.thumbnail, check)
            files: list[OutputFileBlob] = []
            for n, name in enumerate(names):
                key = output_file_key(output_id, n)
                moved = await self._role(
                    blobs, key, directory / FILES_DIR / name, work.slug, "files", check
                )
                ref = await asyncio.to_thread(blobs.ref, key)
                if moved is None or ref is None:
                    raise _FailedError("files", f"{FILES_DIR}/{name} went while it was copied")
                files.append(OutputFileBlob(name=name, blob_key=key, size=ref.size))
        except _FailedError as failed:
            result.failed.append(
                {"output_id": output_id, "role": failed.role, "error": failed.error}
            )
            return None
        result.damaged.extend({"output_id": output_id, **entry} for entry in damaged)
        return ByteWrite(
            output_id,
            OutputBlobs(
                model=keys["model"],
                preview=keys["preview"],
                thumbnail=thumbnail,
                files=tuple(files),
            ),
            work.stamp,
            damaged,
            guarded=True,
            bytes_at=row.bytes_at,
            thumbnail_read=row.blobs.thumbnail,
            thumbnail_swap=swap,
            model_moved=model_moved,
        )
