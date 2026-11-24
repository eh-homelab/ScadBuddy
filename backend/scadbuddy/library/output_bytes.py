"""`OutputBytes`: a saved output's bytes as blobs of kind ``output`` (#7256,
outputs-in-Postgres spec 2026-11-23 §4.3).

It wraps the API's `ContentStore`: keyed puts, a compare-and-swap for the byte copy,
streamed reads, and a temporary file for a reader that needs a path. The keys are
`output_rows.output_blob_key` and `output_file_key`; the row and its holds are
`OutputRows`' to write. The temporary files go under ``content.spool``, the pod's own
disk, and are removed when the reader is done, error or not.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, AbstractContextManager, aclosing
from dataclasses import dataclass
from pathlib import Path

from scadbuddy.library.output_rows import (
    OutputBlobs,
    OutputFileBlob,
    output_blob_key,
    output_file_key,
)
from scadbuddy.render.job_models import FILE_NAME_PATTERN
from scadbuddy.store.content import BlobData, BlobMissingError, BlobRef, BlobScope, ContentStore

#: How long `OutputBytes.blocking_local_file` waits for the download, in seconds.
BLOCKING_READ_TIMEOUT = 300.0

#: The folder's byte files, as `library/outputs.py` names them. Not imported from there:
#: `outputs.py` reads through this module.
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"
THUMBNAIL_NAME = "thumbnail.png"
FILES_DIR = "files"


def usable(content: ContentStore | None) -> bool:
    """Whether output blobs go to ``content``: never on the `local` backend (no content
    store, Ruling R1) or on `bambuddy` (spec §6.4)."""
    return content is not None and content.name != "bambuddy"


@dataclass
class OutputStream:
    size: int
    #: ``None`` when the bytes come from a file, which has no recorded sha256.
    sha256: str | None
    chunks: AsyncIterator[bytes]


async def _rest(first: bytes, chunks: AsyncGenerator[bytes]) -> AsyncGenerator[bytes]:
    try:
        if first:
            yield first
        async for chunk in chunks:
            yield chunk
    finally:
        await chunks.aclose()


def _put_names(directory: Path) -> list[str]:
    """The ``files/`` names an output may hold, sorted: a name's index is its key's."""
    folder = directory / FILES_DIR
    if not folder.is_dir():
        return []
    return sorted(
        path.name
        for path in folder.iterdir()
        if path.is_file() and re.fullmatch(FILE_NAME_PATTERN, path.name)
    )


class OutputBytes:
    def __init__(self, content: ContentStore, loop: asyncio.AbstractEventLoop) -> None:
        self.content = content
        #: The loop that runs `blocking_local_file`'s downloads.
        self._loop = loop

    async def put(self, key: str, data: BlobData, *, slug: str) -> BlobRef:
        return await self.content.put("output", data, name=key, scope=BlobScope(slug=slug), key=key)

    async def put_if_absent(self, key: str, data: bytes, *, slug: str) -> bool:
        """Put ``data`` under ``key`` only when no blob has that key. False: one had."""
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
        file = await asyncio.to_thread(path.open, "rb")
        try:
            return await self.put(key, file, slug=slug)
        finally:
            await asyncio.to_thread(file.close)

    async def put_folder(self, directory: Path, output_id: str, slug: str) -> OutputBlobs:
        """Put the folder's bytes, a file at a time and never read whole: ``model.3mf``
        and ``preview.glb`` (`FileNotFoundError` before any put when one is missing),
        ``thumbnail.png`` when there, and each ``files/`` entry with a file name an
        output may hold."""
        for name in (MODEL_NAME, PREVIEW_NAME):
            if not await asyncio.to_thread((directory / name).is_file):
                raise FileNotFoundError(directory / name)
        model = output_blob_key(output_id, "model")
        preview = output_blob_key(output_id, "preview")
        await self._put_file(model, directory / MODEL_NAME, slug)
        await self._put_file(preview, directory / PREVIEW_NAME, slug)
        thumbnail: str | None = None
        if await asyncio.to_thread((directory / THUMBNAIL_NAME).is_file):
            thumbnail = output_blob_key(output_id, "thumbnail")
            await self._put_file(thumbnail, directory / THUMBNAIL_NAME, slug)
        files: list[OutputFileBlob] = []
        for n, name in enumerate(await asyncio.to_thread(_put_names, directory)):
            key = output_file_key(output_id, n)
            ref = await self._put_file(key, directory / FILES_DIR / name, slug)
            files.append(OutputFileBlob(name=name, blob_key=key, size=ref.size))
        return OutputBlobs(model=model, preview=preview, thumbnail=thumbnail, files=tuple(files))

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
        """The blob's bytes. The first chunk is read before this returns, so a missing
        blob raises `BlobMissingError` here, not in the middle of a response."""
        ref = await self._ref(key)
        chunks = self.content.get(ref)
        try:
            first = await anext(chunks)
        except StopAsyncIteration:
            first = b""
        except BaseException:
            await chunks.aclose()
            raise
        return OutputStream(size=ref.size, sha256=ref.sha256, chunks=_rest(first, chunks))

    async def read(self, key: str) -> bytes:
        return await self.content.read(await self._ref(key))

    async def verify(self, key: str) -> bool:
        """Whether the backend holds the blob whole (`ContentStore.verify`)."""
        ref = await asyncio.to_thread(self.ref, key)
        return ref is not None and await self.content.verify(ref)

    def _temporary(self, suffix: str) -> Path:
        """A new empty file under the spool. Blocking."""
        self.content.spool.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(suffix=suffix, dir=self.content.spool)
        os.close(fd)
        return Path(name)

    async def _download_to(self, ref: BlobRef, path: Path) -> None:
        file = await asyncio.to_thread(path.open, "wb")
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
        path = await asyncio.to_thread(self._temporary, suffix)
        try:
            await self._download_to(ref, path)
            yield path
        finally:
            path.unlink(missing_ok=True)

    def blocking_local_file(self, key: str, suffix: str) -> AbstractContextManager[Path]:
        """`local_file` for a worker thread (Ruling R6): the download runs on the loop,
        and the thread waits at most `BLOCKING_READ_TIMEOUT` (then `TimeoutError`). On
        a thread that runs an event loop it raises `RuntimeError` at once: the wait
        would block the loop that must run the download."""
        return self._blocking_local_file(key, suffix)

    @contextlib.contextmanager
    def _blocking_local_file(self, key: str, suffix: str) -> Iterator[Path]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "blocking_local_file runs on an event loop's thread, where it would block"
                " the loop that must download the blob; use local_file there"
            )
        ref = self.ref(key)
        if ref is None:
            raise BlobMissingError(key)
        path = self._temporary(suffix)
        try:
            future = asyncio.run_coroutine_threadsafe(self._download_to(ref, path), self._loop)
            try:
                future.result(timeout=BLOCKING_READ_TIMEOUT)
            except TimeoutError:
                future.cancel()
                raise
            yield path
        finally:
            path.unlink(missing_ok=True)
