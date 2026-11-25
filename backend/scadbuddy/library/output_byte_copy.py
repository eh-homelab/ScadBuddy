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
blob (a live writer writes the file first, then the blob). An equal blob that the row
does not hold yet (an earlier run put it) is touched, so a sweep keeps it until the
batch holds it. Each blob the copy put, and every blob of a row not copied before, is
read back from the backend (`OutputBytes.verify`, Ruling R3). Then the row names the
keys, in batches of `BATCH_BYTES` outputs, one transaction each
(`OutputRows.write_byte_copy`).

When the model key of a row copied before takes other bytes, what readers derived from
the old blob goes at once, in a transaction of its own (`OutputRows.drop_derived`), and
a derive write that read the old blob writes nothing after it. A row not copied before
keeps its derived rows: they came from the file that the copy put.

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
check that fails is ``failed`` and leaves the row's ``bytes_at`` NULL. A missing file
never drops a key: when the row names a blob for that role (or a ``files/`` name that
went), the key and its hold stay, and the file is ``damaged``. A stat that fails with
anything but a missing file makes the run ``incomplete``. So does a volume that is not
there (phase 1's rule): an outputs folder that is missing or lists no slug folder while
rows exist makes the run write nothing, and an output whose folder is missing gets no
write. Before any upload, the
bytes and blobs still to copy are checked against the store's caps; past one, the run
is ``refused`` and uploads nothing. On `bambuddy` (spec §6.4), and with no content store
at all, every run is refused. On the `local` store the API passes the outputs' own
content store (#7257, plan D7), so the run copies there.

A run has ``finished_at`` only when nothing was refused, failed or incomplete and it was
not stopped. One replica copies at a time, under a session advisory lock on a
connection of its own. Every blocking
call (a stat, a hash, a read, an SQL statement) runs in a thread.
"""

from __future__ import annotations

import asyncio
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

import psycopg
from psycopg.conninfo import make_conninfo

from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.library.output_bytes import (
    FILES_DIR,
    MODEL_NAME,
    PREVIEW_NAME,
    THUMBNAIL_NAME,
    OutputBytes,
    OutputFileNameError,
    put_names,
)
from scadbuddy.library.output_rows import BYTES_LOCK as BYTES_LOCK
from scadbuddy.library.output_rows import (
    CUT_OVER_NOTHING_TO_COPY,
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

#: The lock connection's ``application_name``, as a pool's name is its connections'.
LOCK_APPLICATION = "scadbuddy-output-byte-copy"
#: Seconds to connect for the lock: a database that cannot be reached fails the run.
LOCK_CONNECT_TIMEOUT = 10

#: Outputs per transaction: prod commits take 1-2 s, so one commit per output is too slow.
BATCH_BYTES = 50

REFUSED_NO_CONTENT = "there is no content store; output bytes stay on the share"
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
    #: The outputs whose row the run wrote with a blob it put or a key the row did not
    #: name before.
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
    #: A stat failed, an output's folder is missing, or the outputs folder is missing or
    #: lists no slug folder: the run may have missed bytes.
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
    #: The outputs whose folder is missing: they get no write.
    missing: list[str] = field(default_factory=list)


def _root_lists_outputs(root: Path) -> bool:
    """Whether the outputs folder lists a slug folder: a missing one, or one that reads
    empty, is a volume not there, not every output gone (phase 1's rule). A listing that
    fails with anything but a missing folder raises (`OSError`). Blocking."""
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return False
    return any(entry.is_dir() for entry in entries)


def _sizes(directory: Path) -> list[int]:
    """The sizes of the folder's byte files, for the quota; none when the output holds a
    ``files/`` entry it may not hold, since it is then not copied. Blocking."""
    sizes: list[int] = []
    for name in (MODEL_NAME, PREVIEW_NAME, THUMBNAIL_NAME):
        try:
            sizes.append((directory / name).stat().st_size)
        except FileNotFoundError:
            continue
    try:
        names = put_names(directory)
    except OutputFileNameError:
        return []
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
            if not stat.S_ISDIR(directory.stat().st_mode):
                raise NotADirectoryError(str(directory))
            now = bytes_stamp(directory)
            if bytes_at is not None and stamp == now:
                result.skipped += 1
                continue
            if bytes_at is None:
                sizes = _sizes(directory)
                plan.needed_bytes += sum(sizes)
                plan.needed_count += len(sizes)
        except FileNotFoundError:
            plan.missing.append(output_id)
            continue
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
    conninfo: str,
    stop: threading.Event | None = None,
) -> ByteCopyResult | None:
    """One run of the byte copy; ``None`` when another replica runs one. ``stop`` ends
    it between outputs: it writes the batch so far and leaves its run unfinished.

    The lock is a session lock on a connection of its own (``conninfo``), held for the
    whole run (up to `workflows.housekeeping.OUTPUT_BYTES_TIMEOUT`, 30 minutes, in the
    sweep; with no bound at a start). It is not taken from the API's pool, so the pool's
    other users keep every connection during a run. It is autocommit, so it is never
    idle in a transaction and holds no row lock. Closing the connection ends its
    session, which releases the lock, also when the run fails.

    After the cut-over mark (#7257, plan D4) it does nothing and returns ``None``. It
    checks the mark before the lock and again once it holds the lock, as the mark may
    be written between the two."""
    if await asyncio.to_thread(rows.cut_over):
        logger.info(CUT_OVER_NOTHING_TO_COPY)
        return None
    own = make_conninfo(
        conninfo,
        application_name=LOCK_APPLICATION,
        connect_timeout=LOCK_CONNECT_TIMEOUT,
        **TCP_KEEPALIVE,
    )
    async with await psycopg.AsyncConnection.connect(own, autocommit=True) as lock:
        cursor = await lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (BYTES_LOCK,)
        )
        got = await cursor.fetchone()
        if got is None or not got[0]:
            logger.info("another replica copies the outputs' bytes; this one does not")
            return None
        # Again under the lock: a byte run that ended meanwhile may have written the mark.
        if await asyncio.to_thread(rows.cut_over):
            logger.info(CUT_OVER_NOTHING_TO_COPY)
            return None
        return await _Copy(store, rows, content, stop).run()


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
        #: The outputs the copy put a blob for or named a new key of: `ByteCopyResult.copied`
        #: counts those of them it wrote.
        self.copied: set[str] = set()

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
                result.refused = REFUSED_NO_CONTENT
                return result
            if self.content.name == "bambuddy":
                result.refused = REFUSED_BAMBUDDY
                return result
            content = self.content
            blobs = OutputBytes(content)
            states = await asyncio.to_thread(rows.byte_states)
            if states and not await asyncio.to_thread(
                _root_lists_outputs, self.store.paths.outputs
            ):
                result.incomplete = True
                logger.warning(
                    "the outputs folder lists no output; the byte copy wrote nothing",
                    extra={"rows": len(states)},
                )
                return result
            plan = await asyncio.to_thread(_plan, self.store, states, result, self.stop)
            # A row whose folder is missing: the volume may be only partly there. A row a
            # delete took meanwhile does not count (rows go before files).
            missing = await asyncio.to_thread(rows.existing, plan.missing)
            if missing:
                result.incomplete = True
                logger.warning(
                    "an output's folder is missing; the byte copy left its row as it was",
                    extra={"rows": len(missing)},
                )
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
        self.result.copied += len(written & self.copied)
        for key, previous in replaced:
            await blobs.release_replaced(key, previous)

    async def _verify(self, blobs: OutputBytes, key: str, role: str) -> None:
        if not await blobs.verify(key):
            raise _FailedError(role, NOT_WHOLE)
        self.result.verified += 1

    async def _reuse(self, blobs: OutputBytes, key: str, sha: str, role: str) -> None:
        """Keep an equal blob that the row does not hold yet (an earlier run put it, and
        nothing holds it): touch it, so a sweep on another replica keeps it through its
        grace until the batch holds it. A sweep that removed it first fails the output,
        and its row keeps ``bytes_at`` NULL."""
        ref = await blobs.touch(key)
        if ref is None or ref.sha256 != sha:
            raise _FailedError(role, "the blob went before the copy held it")

    async def _role(
        self,
        blobs: OutputBytes,
        key: str,
        path: Path,
        slug: str,
        role: str,
        check: bool,
        held: bool,
    ) -> tuple[bool, str | None] | None:
        """Copy one byte file to ``key``: ``None`` when the file is missing, else whether
        the copy moved the key to another object, and the sha256 the key named before
        (``None``: no blob). ``held``: the row holds ``key`` already."""
        ref = await asyncio.to_thread(blobs.ref, key)
        expected = None if ref is None else ref.sha256
        try:
            sha, _ = await asyncio.to_thread(_hash_file, path)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _FailedError(role, str(error)) from error
        moved = False
        if sha == expected and not held:
            await self._reuse(blobs, key, sha, role)
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
        return moved, expected

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
            if read != key:
                await self._reuse(blobs, key, sha, "thumbnail")
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
        kept = tuple(await asyncio.to_thread(self.rows.files, output_id))
        before = row.blobs.keys() + [item.blob_key for item in kept]
        # The keys the row names, and so holds: the others a run reuses are touched.
        held = set(before)
        damaged: list[dict[str, Any]] = []
        keys: dict[str, str | None] = {}
        #: Whether the copy put a blob for the output.
        put = False
        try:
            for role in _REQUIRED:
                key = output_blob_key(output_id, role)
                name = _ROLE_FILES[role]
                copied = await self._role(
                    blobs, key, directory / name, work.slug, role, check, key in held
                )
                if copied is None:
                    damaged.append({"role": role, "error": f"no {name}"})
                    # A key the row names keeps its blob and its hold: a file that went
                    # does not make the blob wrong.
                    keys[role] = row.blobs.model if role == "model" else row.blobs.preview
                    continue
                keys[role] = key
                moved, expected = copied
                put = put or moved
                if role == "model" and moved and expected is not None and not check:
                    # Readers read the blob of ``expected``: what they derived from it
                    # goes now, in a transaction of its own, so a batch that a live
                    # writer makes the copy skip cannot keep it. A row not copied before
                    # had its rows derived from the file, which the copy put.
                    await asyncio.to_thread(self.rows.drop_derived, output_id)
            thumbnail, swap = await self._thumbnail(blobs, work, row.blobs.thumbnail, check)
            put = put or swap is not None
            files: list[OutputFileBlob] = []
            gone = [item.name for item in kept if item.name not in names]
            if gone:
                # A file the row names went: keep every file key the row names (a file
                # that went does not make its blob wrong), and add no name until the
                # folder is whole again.
                damaged.append(
                    {"role": "files", "error": f"no {FILES_DIR}/{gone[0]}", "names": gone}
                )
                names = []
                files = list(kept)
            for name in names:
                key = output_file_key(output_id, name)
                copied = await self._role(
                    blobs,
                    key,
                    directory / FILES_DIR / name,
                    work.slug,
                    "files",
                    check,
                    key in held,
                )
                ref = await asyncio.to_thread(blobs.ref, key)
                if copied is None or ref is None:
                    raise _FailedError("files", f"{FILES_DIR}/{name} went while it was copied")
                put = put or copied[0]
                files.append(OutputFileBlob(name=name, blob_key=key, size=ref.size))
        except _FailedError as failed:
            result.failed.append(
                {"output_id": output_id, "role": failed.role, "error": failed.error}
            )
            return None
        result.damaged.extend({"output_id": output_id, **entry} for entry in damaged)
        new = OutputBlobs(
            model=keys["model"],
            preview=keys["preview"],
            thumbnail=thumbnail,
            files=tuple(files),
        )
        if put or new.keys() != before:
            self.copied.add(output_id)
        return ByteWrite(
            output_id,
            new,
            work.stamp,
            damaged,
            guarded=True,
            bytes_at=row.bytes_at,
            thumbnail_read=row.blobs.thumbnail,
            thumbnail_swap=swap,
        )
