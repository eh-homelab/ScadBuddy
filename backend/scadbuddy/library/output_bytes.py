"""`OutputBytes`: a saved output's bytes as blobs of kind ``output`` (#7256,
outputs-in-Postgres spec 2026-11-23 §4.3).

It wraps the API's `ContentStore`: keyed puts, a compare-and-swap for the byte copy,
streamed reads, and a temporary file for a reader that needs a path. The keys are
`output_rows.output_blob_key` and `output_file_key`; the row and its holds are
`OutputRows`' to write. The temporary files go under ``content.spool``, the pod's own
disk, and are removed when the reader is done, error or not.

A sync reader reached from async code (Ruling R6, revised) runs in a thread on the
temporary file: ``async with blobs.local_file(key, suffix) as path: await
asyncio.to_thread(reader, path)``. There is no blocking form: a thread that waits for the
loop's download deadlocks once its callers fill the default executor.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, aclosing
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

import aiohttp
from botocore.exceptions import ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError

from scadbuddy.library.output_rows import (
    OutputBlobs,
    OutputFileBlob,
    output_blob_key,
    output_file_key,
)
from scadbuddy.render.job_models import FILE_NAME_PATTERN
from scadbuddy.store.content import BlobData, BlobMissingError, BlobRef, BlobScope, ContentStore

#: The folder's byte files. `library/outputs.py` imports them from here.
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"
THUMBNAIL_NAME = "thumbnail.png"
FILES_DIR = "files"


class OutputFileNameError(ValueError):
    """A ``files/`` entry that is not a regular file or has a name an output may not
    hold (`FILE_NAME_PATTERN`). ``names`` lists every such entry, sorted."""

    def __init__(self, directory: Path, names: tuple[str, ...]) -> None:
        super().__init__(
            f"{directory / FILES_DIR} holds entries an output may not hold: {', '.join(names)}"
        )
        self.names = names


def usable(content: ContentStore | None) -> bool:
    """Whether output blobs go to ``content``: never on the `local` backend (no content
    store, Ruling R1) or on `bambuddy` (spec §6.4)."""
    return content is not None and content.name != "bambuddy"


@dataclass
class OutputStream:
    """A blob's bytes. Whoever takes one closes it (`aclose`, or ``async with``), read
    or not: an unread ``chunks`` generator never runs its own cleanup, so the backend's
    read (``_source``) is closed apart."""

    size: int
    #: ``None`` when the bytes come from a file, which has no recorded sha256.
    sha256: str | None
    chunks: AsyncGenerator[bytes]
    _source: AsyncGenerator[bytes] | None = field(default=None, repr=False)
    #: Called last by `aclose`: a file stream closes its file here.
    _on_close: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        try:
            await self.chunks.aclose()
        finally:
            try:
                if self._source is not None:
                    await self._source.aclose()
            finally:
                if self._on_close is not None:
                    await self._on_close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


#: A blob of at most this many bytes is checked whole before `OutputBytes.stream` returns.
_PREFETCH = 1 << 20
#: A file put of at most this many bytes is read whole in a thread, when the backend
#: names no `multipart_threshold` of its own (the S3 backend's default).
_WHOLE_PUT = 64 << 20

#: The store did not answer, or its answer broke off: botocore's connection errors
#: (`EndpointConnectionError`, `ConnectTimeoutError`) and HTTP client errors
#: (`ReadTimeoutError`, `ConnectionClosedError`, `ResponseStreamingError`), and aiohttp's
#: errors and timeouts while the body streams. A reader counts one before its first byte
#: as a lost blob and reads the file.
TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    BotoConnectionError,
    HTTPClientError,
    aiohttp.ClientError,
    TimeoutError,
)


def server_error(error: BaseException) -> bool:
    """An S3 answer of 5xx (``InternalError``, ``SlowDown`` and the like), after
    botocore's own retries: the store failed, not the request. A 4xx is the request's
    fault (a rotated key's 403) and is not one."""
    if not isinstance(error, ClientError):
        return False
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return isinstance(status, int) and status >= 500


async def in_thread_closing[T](call: Callable[[], T], close: Callable[[T], None]) -> T:
    """``call()`` in a thread. When the caller is cancelled while the thread runs,
    ``close`` runs on the thread's result once it returns, so an opened file is never
    left for the garbage collector."""
    made = asyncio.ensure_future(asyncio.to_thread(call))
    try:
        return await asyncio.shield(made)
    except BaseException:

        def closed(done: asyncio.Future[T]) -> None:
            if not done.cancelled() and done.exception() is None:
                close(done.result())

        made.add_done_callback(closed)
        raise


def _close_opened(opened: bytes | BinaryIO) -> None:
    if not isinstance(opened, bytes):
        opened.close()


def _read_for_put(path: Path, whole: int) -> bytes | BinaryIO:
    """The file's bytes when it holds at most ``whole`` bytes, else the file, open at its
    start, for the caller to close. Blocking."""
    file = path.open("rb")
    kept = False
    try:
        if os.fstat(file.fileno()).st_size > whole:
            kept = True
            return file
        return file.read()
    finally:
        if not kept:
            file.close()


async def _held(
    read: list[bytes], ended: bool, source: AsyncGenerator[bytes]
) -> AsyncGenerator[bytes]:
    """Each chunk goes only once the next one came, so the last waits for the sha256
    check: a corrupt or cut blob ends the response short, never clean."""
    try:
        if ended:
            for chunk in read:
                yield chunk
            return
        held = read.pop()
        for chunk in read:
            yield chunk
        async for chunk in source:
            yield held
            held = chunk
        yield held
    finally:
        await source.aclose()


def put_names(directory: Path) -> list[str]:
    """Every ``files/`` name, sorted: a name's index is its key's. `OutputFileNameError`
    when an entry is not a regular file or has a name an output may not hold."""
    folder = directory / FILES_DIR
    if not folder.is_dir():
        return []
    names = sorted(path.name for path in folder.iterdir())
    bad = tuple(
        name
        for name in names
        if (folder / name).is_symlink()
        or not (folder / name).is_file()
        or not re.fullmatch(FILE_NAME_PATTERN, name)
    )
    if bad:
        raise OutputFileNameError(directory, bad)
    return names


def _discard_temporary(made: asyncio.Future[tuple[BinaryIO, Path]]) -> None:
    if made.cancelled() or made.exception() is not None:
        return
    file, path = made.result()
    file.close()
    path.unlink(missing_ok=True)


class OutputBytes:
    def __init__(self, content: ContentStore) -> None:
        self.content = content

    async def put(self, key: str, data: BlobData, *, slug: str) -> BlobRef:
        return await self.content.put("output", data, name=key, scope=BlobScope(slug=slug), key=key)

    async def put_if_absent(self, key: str, data: bytes, *, slug: str) -> bool:
        """Put ``data`` under ``key`` only when no blob has that key. False: one had.
        A key the index names already is answered with no hash or upload; the
        compare-and-swap stays the guard for a put that races this one."""
        if await asyncio.to_thread(self.ref, key) is not None:
            return False
        landed = await self.content.replace(
            key, "output", data, name=key, scope=BlobScope(slug=slug), expected=None
        )
        return landed is not None

    async def replace(
        self, key: str, data: BlobData, *, slug: str, expected: str | None
    ) -> BlobRef | None:
        """Put ``data`` under ``key`` only while the key names the sha256 ``expected``
        (``None``: no blob). ``None`` when the key moved on."""
        return await self.content.replace(
            key, "output", data, name=key, scope=BlobScope(slug=slug), expected=expected
        )

    async def _put_file(self, key: str, path: Path, slug: str) -> BlobRef:
        """Put the file. No read of it runs on the loop: botocore reads a file body on
        the loop (its checksum and its signature), so a file of at most the backend's
        `multipart_threshold` is read whole in a thread and put as bytes, and a larger
        one goes up in parts, each read in a thread (`S3ContentBackend._multipart`)."""
        whole = int(getattr(self.content.backend, "multipart_threshold", _WHOLE_PUT))
        data = await in_thread_closing(lambda: _read_for_put(path, whole), _close_opened)
        if isinstance(data, bytes):
            return await self.put(key, data, slug=slug)
        try:
            return await self.put(key, data, slug=slug)
        finally:
            await asyncio.to_thread(data.close)

    async def put_folder(self, directory: Path, output_id: str, slug: str) -> OutputBlobs:
        """Put the folder's bytes, a file at a time and never read whole: ``model.3mf``
        and ``preview.glb``, ``thumbnail.png`` when there, and every ``files/`` entry.
        Before any put: `FileNotFoundError` when ``model.3mf`` or ``preview.glb`` is
        missing, and `OutputFileNameError` when a ``files/`` entry is not a regular file
        or has a name an output may not hold."""
        for name in (MODEL_NAME, PREVIEW_NAME):
            if not await asyncio.to_thread((directory / name).is_file):
                raise FileNotFoundError(directory / name)
        names = await asyncio.to_thread(put_names, directory)
        model = output_blob_key(output_id, "model")
        preview = output_blob_key(output_id, "preview")
        await self._put_file(model, directory / MODEL_NAME, slug)
        await self._put_file(preview, directory / PREVIEW_NAME, slug)
        thumbnail: str | None = None
        if await asyncio.to_thread((directory / THUMBNAIL_NAME).is_file):
            thumbnail = output_blob_key(output_id, "thumbnail")
            await self._put_file(thumbnail, directory / THUMBNAIL_NAME, slug)
        files: list[OutputFileBlob] = []
        for n, name in enumerate(names):
            key = output_file_key(output_id, n)
            ref = await self._put_file(key, directory / FILES_DIR / name, slug)
            files.append(OutputFileBlob(name=name, blob_key=key, size=ref.size))
        return OutputBlobs(model=model, preview=preview, thumbnail=thumbnail, files=tuple(files))

    async def release_replaced(self, key: str, previous: BlobRef) -> None:
        """Release the object ``key`` named before it took another one, as a put that
        replaces a key does."""
        await self.content.release_replaced(key, previous)

    def ref(self, key: str) -> BlobRef | None:
        """The blob ``key`` names: one index read, no backend call. Blocking."""
        stat = self.content.index.get(key)
        return None if stat is None else stat.ref

    async def _ref(self, key: str) -> BlobRef:
        ref = await asyncio.to_thread(self.ref, key)
        if ref is None:
            raise BlobMissingError(key)
        return ref

    async def stream(self, key: str) -> OutputStream:
        """The blob's bytes. Up to `_PREFETCH` bytes and one chunk more are read before
        this returns. So a missing blob, and a corrupt blob of at most `_PREFETCH` bytes,
        raise here (`BlobMissingError`, `BlobCorruptError`), never in the middle of a
        response; so does a `TRANSPORT_ERRORS` error during that read. A larger corrupt
        blob raises at the end of its stream, and the last chunk is held back until the
        check passes; a transport error past the prefetch raises from the chunks, so the
        response ends short. The caller closes the stream (`OutputStream.aclose`), read
        or not."""
        ref = await self._ref(key)
        source = self.content.get(ref)
        read: list[bytes] = []
        size = 0
        ended = False
        try:
            # `<=`: one more read past exactly `_PREFETCH` bytes ends the source and runs
            # its sha256 check, so a blob of that size is checked whole here too.
            while size <= _PREFETCH:
                try:
                    chunk = await anext(source)
                except StopAsyncIteration:
                    ended = True
                    break
                read.append(chunk)
                size += len(chunk)
        except BaseException:
            await source.aclose()
            raise
        if not read:
            ended = True
        return OutputStream(
            size=ref.size, sha256=ref.sha256, chunks=_held(read, ended, source), _source=source
        )

    async def read(self, key: str) -> bytes:
        return await self.content.read(await self._ref(key))

    async def verify(self, key: str) -> bool:
        """Whether the backend holds the blob whole (`ContentStore.verify`)."""
        ref = await asyncio.to_thread(self.ref, key)
        return ref is not None and await self.content.verify(ref)

    def _temporary(self, suffix: str) -> tuple[BinaryIO, Path]:
        """A new empty file under the spool, open for writing, and its path. Blocking."""
        self.content.spool.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(suffix=suffix, dir=self.content.spool)
        return os.fdopen(fd, "wb"), Path(name)

    async def _download_to(self, ref: BlobRef, file: BinaryIO) -> None:
        """Write the blob to ``file``, the one `_temporary` opened, and close it. The
        path is never opened again, so nothing can make it again after its unlink."""
        try:
            async with aclosing(self.content.get(ref)) as chunks:
                async for chunk in chunks:
                    await asyncio.to_thread(file.write, chunk)
        finally:
            await asyncio.to_thread(file.close)

    def local_file(self, key: str, suffix: str) -> AbstractAsyncContextManager[Path]:
        """The blob in a temporary file under the spool, removed on exit, error or not."""
        return self._local_file(key, suffix)

    @contextlib.asynccontextmanager
    async def _local_file(self, key: str, suffix: str) -> AsyncIterator[Path]:
        ref = await self._ref(key)
        made = asyncio.ensure_future(asyncio.to_thread(self._temporary, suffix))
        try:
            file, path = await asyncio.shield(made)
        except BaseException:
            # A cancel while the thread makes the file: remove it once it is made.
            made.add_done_callback(_discard_temporary)
            raise
        try:
            await self._download_to(ref, file)
            yield path
        finally:
            path.unlink(missing_ok=True)
