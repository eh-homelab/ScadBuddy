"""A design request's multipart body, read with caps (#5282, spec 2026-11-02 §5.4, plan
Task 9). The edge and the CRUD side both read it here, each for itself: neither trusts
the other to have checked it.

An anonymous visitor sends it, so every count comes from the bytes as they arrive, never
from ``Content-Length`` or a part's own headers: the whole body (`MAX_REQUEST_BODY`), the
number of parts, each part's headers (`MAX_PART_HEADER`), each file
(`MAX_REQUEST_FILE_BYTES`), the number of files (`MAX_REQUEST_FILES`) and the text
(`checked_text`, as soon as its part ends). A declared ``Content-Length`` past the cap is
refused before any byte is read. Each file is spooled to a temporary file past
`SPOOL_BYTES`, never held whole in memory.

Each process bounds what all its uploads hold at once (`UploadGate`): at most
`MAX_UPLOADS_AT_ONCE` bodies, and `MAX_SPOOL_BYTES` spooled across them; past either, 503
``uploads_busy``. The whole body must come within `MAX_BODY_SECONDS`, with no gap past
`MAX_GAP_SECONDS` between chunks, else 408 ``upload_too_slow``. It imports no database
code: the edge imports it.
"""

from __future__ import annotations

import threading
import time
import unicodedata
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Final

import anyio
from fastapi import Request, status
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from scadbuddy.core.problems import ApiError
from scadbuddy.storefront.wire import (
    MAX_REQUEST_FILE_BYTES,
    MAX_REQUEST_FILES,
    MAX_REQUEST_TEXT,
    STOREFRONT_PROBLEMS,
)

#: The largest whole body: every file at its cap, and room for the text and the
#: multipart framing.
MAX_REQUEST_BODY = MAX_REQUEST_FILES * MAX_REQUEST_FILE_BYTES + 1024 * 1024
#: The most bytes of one part's headers.
MAX_PART_HEADER: Final = 4096
#: The most bytes of the text part (each character at most four bytes of UTF-8).
MAX_TEXT_BYTES: Final = MAX_REQUEST_TEXT * 4
#: Past this, a file goes to a temporary file on disk.
SPOOL_BYTES: Final = 1024 * 1024
#: The most request bodies one process reads or holds at once.
MAX_UPLOADS_AT_ONCE = 2
#: The most body bytes one process holds across those bodies.
MAX_SPOOL_BYTES = 600 * 1024 * 1024
#: The longest a whole body may take, and the longest gap between two of its chunks.
MAX_BODY_SECONDS = 300.0
MAX_GAP_SECONDS = 30.0
#: What a visitor turned away as busy waits before it asks again.
BUSY_RETRY: Final = 30
#: The control characters a request's text may hold (after line ends are made ``\n``).
_KEPT_CONTROLS: Final = frozenset("\n\t")


def refused(status_code: int, slug: str, detail: str) -> ApiError:
    """A refusal under ``https://scadbuddy.dev/problems/storefront/<slug>``."""
    return ApiError(status_code, detail, type_=STOREFRONT_PROBLEMS + slug)


def not_a_request() -> ApiError:
    return refused(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "draft_body_invalid",
        "This request is not a design request.",
    )


def file_too_large() -> ApiError:
    return refused(
        status.HTTP_413_CONTENT_TOO_LARGE,
        "file_too_large",
        f"Each file may be at most {MAX_REQUEST_FILE_BYTES // (1024 * 1024)} MB.",
    )


def too_many_files() -> ApiError:
    return refused(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "too_many_files",
        f"Send at most {MAX_REQUEST_FILES} files.",
    )


def request_text_invalid() -> ApiError:
    return refused(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "request_text",
        f"Describe what you want in 1 to {MAX_REQUEST_TEXT} characters of plain text.",
    )


def uploads_busy() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "The shop is taking other files right now. Try again in a moment.",
        type_=STOREFRONT_PROBLEMS + "uploads_busy",
        headers={"Retry-After": str(BUSY_RETRY)},
        retry_after=BUSY_RETRY,
    )


def upload_too_slow() -> ApiError:
    return refused(
        status.HTTP_408_REQUEST_TIMEOUT,
        "upload_too_slow",
        "Your files took too long to arrive. Try again on a faster connection.",
    )


def checked_text(sent: str) -> str:
    """A request's text as plain text: line ends made ``\n``, stripped, with no control
    character but ``\n`` and ``\t``, and 1 to `MAX_REQUEST_TEXT` characters; else 422
    ``request_text``."""
    text = sent.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not 1 <= len(text) <= MAX_REQUEST_TEXT:
        raise request_text_invalid()
    if any(c not in _KEPT_CONTROLS and unicodedata.category(c) == "Cc" for c in text):
        raise request_text_invalid()
    return text


class UploadGate:
    """What one process's uploads hold at once: `MAX_UPLOADS_AT_ONCE` bodies, and
    `MAX_SPOOL_BYTES` across them. Each upload holds a slot (`enter`) from before its
    first byte until its files are closed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = 0
        self.spooled = 0

    def enter(self) -> UploadSlot:
        """A slot; 503 ``uploads_busy`` while `MAX_UPLOADS_AT_ONCE` are taken."""
        with self._lock:
            if self.running >= MAX_UPLOADS_AT_ONCE:
                raise uploads_busy()
            self.running += 1
        return UploadSlot(self)


class UploadSlot:
    def __init__(self, gate: UploadGate) -> None:
        self._gate = gate
        self._taken = 0
        self._released = False

    def take(self, size: int) -> None:
        """``size`` more bytes held; 503 ``uploads_busy`` when the process would hold
        more than `MAX_SPOOL_BYTES`."""
        with self._gate._lock:
            if self._gate.spooled + size > MAX_SPOOL_BYTES:
                raise uploads_busy()
            self._gate.spooled += size
            self._taken += size

    def release(self) -> None:
        with self._gate._lock:
            if self._released:
                return
            self._released = True
            self._gate.running -= 1
            self._gate.spooled -= self._taken


#: This process's gate.
UPLOADS = UploadGate()


@dataclass
class RequestUpload:
    """The text and the files, each spooled and at its start, and the gate's slot they
    hold. `close` them."""

    text: str
    files: list[UploadFile] = field(default_factory=list)
    slot: UploadSlot | None = None

    async def close(self) -> None:
        try:
            for upload in self.files:
                await upload.close()
        finally:
            if self.slot is not None:
                self.slot.release()


class _CappedParser(MultiPartParser):
    """Starlette's parser, with a cap on each part's headers, on each file's bytes, on
    the files and on the fields, each refused as a storefront problem."""

    spool_max_size = SPOOL_BYTES

    def __init__(self, headers: Headers, stream: AsyncGenerator[bytes]) -> None:
        # The counts are this class's own, so a refusal says which cap it hit.
        super().__init__(
            headers,
            stream,
            max_files=float("inf"),
            max_fields=float("inf"),
            max_part_size=MAX_TEXT_BYTES,
        )
        self._header_bytes = 0
        self._file_bytes = 0
        self._texts = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._header_bytes = 0
        self._file_bytes = 0

    def _header(self, size: int) -> None:
        self._header_bytes += size
        if self._header_bytes > MAX_PART_HEADER:
            raise not_a_request()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header(end - start)
        super().on_header_value(data, start, end)

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        name = self._current_part.field_name
        if self._current_part.file is None:
            self._texts += 1
            if name != "text" or self._texts > 1:
                raise not_a_request()
        else:
            if name != "files":
                raise not_a_request()
            if self._current_files > MAX_REQUEST_FILES:
                raise too_many_files()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._file_bytes += end - start
            if self._file_bytes > MAX_REQUEST_FILE_BYTES:
                raise file_too_large()
        elif len(self._current_part.data) + end - start > MAX_TEXT_BYTES:
            raise request_text_invalid()
        super().on_part_data(data, start, end)

    def on_part_end(self) -> None:
        super().on_part_end()
        if self._current_part.file is None:
            # The text, checked as soon as it ends: before any later file arrives.
            name, sent = self.items[-1]
            assert isinstance(sent, str)
            self.items[-1] = (name, checked_text(sent))


async def _capped(request: Request, slot: UploadSlot) -> AsyncGenerator[bytes]:
    """The body as it arrives, each chunk held against the gate's budget: 413
    ``file_too_large`` past `MAX_REQUEST_BODY`, 503 ``uploads_busy`` past
    `MAX_SPOOL_BYTES`, 408 ``upload_too_slow`` past `MAX_BODY_SECONDS` or a gap past
    `MAX_GAP_SECONDS`."""
    deadline = time.monotonic() + MAX_BODY_SECONDS
    chunks = request.stream().__aiter__()
    seen = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise upload_too_slow()
        try:
            with anyio.fail_after(min(MAX_GAP_SECONDS, remaining)):
                chunk = await chunks.__anext__()
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise upload_too_slow() from None
        seen += len(chunk)
        if seen > MAX_REQUEST_BODY:
            raise file_too_large()
        slot.take(len(chunk))
        yield chunk


async def read_request(request: Request, gate: UploadGate | None = None) -> RequestUpload:
    """The request's text and files, every cap checked as the bytes arrive, under a slot
    of ``gate`` (`UPLOADS`) that the answer holds until it is closed. Refusals: 503
    ``uploads_busy`` (before any byte is read, or as the spool budget is crossed), 413
    ``file_too_large`` (a declared ``Content-Length`` too, before any byte), 408
    ``upload_too_slow``, 422 ``too_many_files``, 422 ``request_text``, 422
    ``draft_body_invalid`` (not multipart, a part header past `MAX_PART_HEADER`, a field
    other than one ``text`` and the ``files``)."""
    slot = (gate or UPLOADS).enter()
    try:
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            raise not_a_request()
        declared = request.headers.get("content-length")
        if declared is not None:
            if not declared.isdigit():
                raise not_a_request()
            if int(declared) > MAX_REQUEST_BODY:
                raise file_too_large()
        parser = _CappedParser(request.headers, _capped(request, slot))
        try:
            form = await parser.parse()
        except (MultiPartException, ValueError):
            # python-multipart's own refusals (a malformed body, its header limit) are
            # `ValueError`s.
            raise not_a_request() from None
        files = [value for _, value in form.multi_items() if isinstance(value, UploadFile)]
        texts = [value for _, value in form.multi_items() if isinstance(value, str)]
        upload = RequestUpload(text=texts[0] if texts else "", files=files, slot=slot)
    except BaseException:
        slot.release()
        raise
    if not texts:
        await upload.close()
        raise request_text_invalid()
    return upload
