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
``uploads_busy``. The byte budget is the main bound; the count keeps slow senders from
holding every slot. The whole body must come within `MAX_BODY_SECONDS`, with no gap past
`MAX_GAP_SECONDS` between chunks, and, after the first `RATE_GRACE_SECONDS`, at least
`MIN_WINDOW_BYTES` in each `RATE_WINDOW_SECONDS`; else 408 ``upload_too_slow``. It
imports no database code: the edge imports it.
"""

from __future__ import annotations

import json
import threading
import time
import unicodedata
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Final

import anyio
from fastapi import Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from scadbuddy.core.problems import ApiError
from scadbuddy.storefront.wire import (
    MAX_LINK_CHARS,
    MAX_LINK_NOTE,
    MAX_PURCHASE_LINKS,
    MAX_PURCHASE_LINKS_BYTES,
    MAX_REQUEST_FILE_BYTES,
    MAX_REQUEST_FILES,
    MAX_REQUEST_TEXT,
    PURCHASE_LINKS_FIELD,
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
MAX_UPLOADS_AT_ONCE = 8
#: The most body bytes one process holds across those bodies.
MAX_SPOOL_BYTES = 600 * 1024 * 1024
#: The longest a whole body may take, and the longest gap between two of its chunks.
MAX_BODY_SECONDS = 300.0
MAX_GAP_SECONDS = 30.0
#: The least rate a body must keep: `MIN_WINDOW_BYTES` in each `RATE_WINDOW_SECONDS`,
#: counted from `RATE_GRACE_SECONDS` after its start.
RATE_GRACE_SECONDS = 10.0
RATE_WINDOW_SECONDS = 10.0
MIN_WINDOW_BYTES = 64 * 1024
#: The clock `_capped` times a body by (a test sets its own).
_now = time.monotonic
_fail_after = anyio.fail_after
#: What a visitor turned away as busy waits before it asks again.
BUSY_RETRY: Final = 30
#: The control characters a request's text may hold (after line ends are made ``\n``).
_KEPT_CONTROLS: Final = frozenset("\n\t")


class PurchaseLinkAsk(BaseModel):
    """A thing the customer asks the shop to buy for their design request (#6133, spec
    2026-11-13 §10.1): a link, kept as text, and a note. The CRUD side holds the link to
    the text rules (`orders/customer_links.py` `checked_link`)."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=MAX_LINK_CHARS)
    note: str = Field(default="", max_length=MAX_LINK_NOTE)


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


def too_many_links() -> ApiError:
    return refused(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "too_many_links",
        f"Send at most {MAX_PURCHASE_LINKS} things to buy.",
    )


def link_invalid(index: int, detail: str = "This link cannot be used.") -> ApiError:
    """422 ``link_invalid`` for the link at ``index`` of the things to buy."""
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail,
        type_=STOREFRONT_PROBLEMS + "link_invalid",
        index=index,
    )


def checked_purchase_links(sent: str) -> list[PurchaseLinkAsk]:
    """A design request's things to buy, from their part's JSON: a list of at most
    `MAX_PURCHASE_LINKS` objects, each a ``url`` and an optional ``note``. Refusals: 422
    ``draft_body_invalid`` for text that is not such a list, ``too_many_links``, and
    ``link_invalid`` (with ``index``) for an entry of another shape, past a cap, or with
    text UTF-8 cannot carry (a lone surrogate)."""
    try:
        found = json.loads(sent)
    except (ValueError, RecursionError):
        raise not_a_request() from None
    if not isinstance(found, list):
        raise not_a_request()
    if len(found) > MAX_PURCHASE_LINKS:
        raise too_many_links()
    links = []
    for index, entry in enumerate(found):
        try:
            link = PurchaseLinkAsk.model_validate(entry)
            # `json.loads` takes a lone surrogate escape, which UTF-8 cannot carry: the
            # edge's forwarded part and the CRUD side's insert would fail on it (review
            # of #6200).
            link.url.encode()
            link.note.encode()
        except (ValidationError, UnicodeEncodeError):
            raise link_invalid(index) from None
        links.append(link)
    return links


def request_text_invalid() -> ApiError:
    return refused(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "request_text",
        f"Describe what you want in 1 to {MAX_REQUEST_TEXT} characters of plain text.",
    )


#: The problem type of `uploads_busy`.
UPLOADS_BUSY: Final = STOREFRONT_PROBLEMS + "uploads_busy"


def uploads_busy() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "The shop is taking other files right now. Try again in a moment.",
        type_=UPLOADS_BUSY,
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
    """The text, the things to buy (#6133) and the files, each spooled and at its start,
    and the gate's slot they hold. `close` them; `release` frees the slot first, when the
    caller keeps the files past what the gate should count."""

    text: str
    files: list[UploadFile] = field(default_factory=list)
    slot: UploadSlot | None = None
    purchase_links: list[PurchaseLinkAsk] = field(default_factory=list)

    def release(self) -> None:
        if self.slot is not None:
            self.slot.release()

    async def close(self) -> None:
        try:
            for upload in self.files:
                await upload.close()
        finally:
            self.release()


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
            max_part_size=max(MAX_TEXT_BYTES, MAX_PURCHASE_LINKS_BYTES),
        )
        self._header_bytes = 0
        self._file_bytes = 0
        self._texts = 0
        self._link_parts = 0
        #: The things to buy, read from their part as soon as it ends.
        self.purchase_links: list[PurchaseLinkAsk] = []

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
            if name == "text":
                self._texts += 1
            elif name == PURCHASE_LINKS_FIELD:
                self._link_parts += 1
            else:
                raise not_a_request()
            if self._texts > 1 or self._link_parts > 1:
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
        elif self._current_part.field_name == PURCHASE_LINKS_FIELD:
            if len(self._current_part.data) + end - start > MAX_PURCHASE_LINKS_BYTES:
                raise not_a_request()
        elif len(self._current_part.data) + end - start > MAX_TEXT_BYTES:
            raise request_text_invalid()
        super().on_part_data(data, start, end)

    def on_part_end(self) -> None:
        super().on_part_end()
        if self._current_part.file is None:
            # Each text part, checked as soon as it ends: before any later file arrives.
            name, sent = self.items[-1]
            assert isinstance(sent, str)
            if name == PURCHASE_LINKS_FIELD:
                self.purchase_links = checked_purchase_links(sent)
            else:
                self.items[-1] = (name, checked_text(sent))


async def _capped(request: Request, slot: UploadSlot) -> AsyncGenerator[bytes]:
    """The body as it arrives, each chunk held against the gate's budget: 413
    ``file_too_large`` past `MAX_REQUEST_BODY`, 503 ``uploads_busy`` past
    `MAX_SPOOL_BYTES`, 408 ``upload_too_slow`` past `MAX_BODY_SECONDS`, a gap past
    `MAX_GAP_SECONDS`, or a window under `MIN_WINDOW_BYTES`.

    Each wait ends at the first moment the body would be too slow, so a wait that times
    out is always a refusal: the stream is never read again after a cancelled read."""
    start = _now()
    deadline = start + MAX_BODY_SECONDS
    # The window being counted: [window_end - RATE_WINDOW_SECONDS, window_end).
    window_end = start + RATE_GRACE_SECONDS + RATE_WINDOW_SECONDS
    window_bytes = 0
    chunks = request.stream().__aiter__()
    seen = 0
    while True:
        now = _now()
        # The window that must next hold `MIN_WINDOW_BYTES`: this one, or (already full)
        # the next.
        floor = window_end if window_bytes < MIN_WINDOW_BYTES else window_end + RATE_WINDOW_SECONDS
        wait = min(MAX_GAP_SECONDS, deadline - now, floor - now)
        if wait <= 0:
            raise upload_too_slow()
        try:
            with _fail_after(wait):
                chunk = await chunks.__anext__()
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise upload_too_slow() from None
        now = _now()
        # Every window that closed before this chunk came is judged on its own bytes
        # (the first empty one refuses), then the chunk counts in the window it came in.
        while now >= window_end:
            if window_bytes < MIN_WINDOW_BYTES:
                raise upload_too_slow()
            window_end += RATE_WINDOW_SECONDS
            window_bytes = 0
        if now >= window_end - RATE_WINDOW_SECONDS:
            window_bytes += len(chunk)
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
    other than one ``text``, at most one `PURCHASE_LINKS_FIELD` and the ``files``), and
    the things to buy's own (`checked_purchase_links`)."""
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
        texts = [value for name, value in form.multi_items() if name == "text"]
        upload = RequestUpload(
            text=str(texts[0]) if texts else "",
            files=files,
            slot=slot,
            purchase_links=parser.purchase_links,
        )
    except BaseException:
        slot.release()
        raise
    if not texts:
        await upload.close()
        raise request_text_invalid()
    return upload
