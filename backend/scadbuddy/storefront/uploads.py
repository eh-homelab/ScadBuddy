"""A design request's multipart body, read with caps (#5282, spec 2026-11-02 §5.4, plan
Task 9). The edge and the CRUD side both read it here, each for itself: neither trusts
the other to have checked it.

An anonymous visitor sends it, so every count comes from the bytes as they arrive, never
from ``Content-Length`` or a part's own headers: the whole body (`MAX_REQUEST_BODY`), the
number of parts, each part's headers (`MAX_PART_HEADER`), each file
(`MAX_REQUEST_FILE_BYTES`), the number of files (`MAX_REQUEST_FILES`) and the text
(`MAX_REQUEST_TEXT` characters). Each file is spooled to a temporary file past
`SPOOL_BYTES`, never held whole in memory. The only fields are ``text`` (once) and
``files``. It imports no database code: the edge imports it.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Final

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


@dataclass
class RequestUpload:
    """The text and the files, each spooled and at its start. `close` them."""

    text: str
    files: list[UploadFile] = field(default_factory=list)

    async def close(self) -> None:
        for upload in self.files:
            await upload.close()


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
        super().on_part_data(data, start, end)


async def _capped(request: Request) -> AsyncGenerator[bytes]:
    """The body as it arrives; 413 ``file_too_large`` past `MAX_REQUEST_BODY`."""
    seen = 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > MAX_REQUEST_BODY:
            raise file_too_large()
        yield chunk


async def read_request(request: Request) -> RequestUpload:
    """The request's text and files, every cap checked as the bytes arrive. Refusals:
    413 ``file_too_large``, 422 ``too_many_files``, 422 ``draft_body_invalid`` (not
    multipart, a part header past `MAX_PART_HEADER`, a field other than ``text`` and
    ``files``, no text or a text past `MAX_REQUEST_TEXT` characters)."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise not_a_request()
    parser = _CappedParser(request.headers, _capped(request))
    try:
        form = await parser.parse()
    except (MultiPartException, ValueError):
        # python-multipart's own refusals (a malformed body, its header limit) are
        # `ValueError`s.
        raise not_a_request() from None
    files = [value for _, value in form.multi_items() if isinstance(value, UploadFile)]
    texts = [value for _, value in form.multi_items() if isinstance(value, str)]
    upload = RequestUpload(text=texts[0] if texts else "", files=files)
    if not 1 <= len(upload.text) <= MAX_REQUEST_TEXT:
        await upload.close()
        raise not_a_request()
    return upload
