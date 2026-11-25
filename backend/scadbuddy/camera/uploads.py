"""The recorder's local segments and their upload to the camera bucket.

Only the recorder pods hold the bucket's camera keys. A key never reaches a log, a repr or an
exception: a failed put raises `UploadError`, which names the printer, the segment's start and
the error's type or S3 code only, never the client's own text (a botocore error can carry the
request URL, and a presigned query names the key id)."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import ClientError

from scadbuddy.camera.settings import RecorderSettings

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

#: botocore logs each request's headers at DEBUG, the ``Authorization`` header with the key id
#: among them. The recorder never lets those records through, whatever its log level.
_QUIET = ("botocore", "aiobotocore")
_CODE = re.compile(r"[A-Za-z0-9.]{1,64}")


@dataclass(frozen=True)
class LocalSegment:
    """One fMP4 segment MediaMTX finished on the recorder's disk."""

    path: Path
    printer_id: str
    started_at: datetime
    duration_ms: int
    bytes: int
    init_bytes: int
    init_sha256: str

    @property
    def ends_at(self) -> datetime:
        return self.started_at + timedelta(milliseconds=self.duration_ms)


class UploadError(Exception):
    """The bucket did not take a segment. It names no key, URL or client text."""


def object_key(prefix: str, printer_id: str, started_at: datetime) -> str:
    """``<prefix>/camera/<printer_id>/<yyyy>/<mm>/<dd>/<HHMMSS.ffffff>.mp4`` (UTC start)."""
    name = started_at.strftime("camera/{}/%Y/%m/%d/%H%M%S.%f.mp4").format(printer_id)
    head = prefix.strip("/")
    return f"{head}/{name}" if head else name


def _why(error: Exception) -> str:
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", ""))
        if _CODE.fullmatch(code):
            return f"ClientError {code}"
    return type(error).__name__


class Uploader:
    def __init__(self, settings: RecorderSettings) -> None:
        for name in _QUIET:
            logger = logging.getLogger(name)
            logger.setLevel(max(logger.getEffectiveLevel(), logging.WARNING))
        self._settings = settings
        self._prefix = settings.camera_s3_prefix
        self._config = AioConfig(
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=30,
            retries={"mode": "standard"},
        )
        self._exits = AsyncExitStack()
        self._client: S3Client | None = None
        self._opening = asyncio.Lock()

    def __repr__(self) -> str:
        return f"Uploader(bucket={self._settings.camera_s3_bucket!r}, prefix={self._prefix!r})"

    async def client(self) -> S3Client:
        """The bucket's client, opened on first use."""
        async with self._opening:
            if self._client is None:
                self._client = await self._exits.enter_async_context(
                    get_session().create_client(
                        "s3",
                        endpoint_url=self._settings.camera_s3_endpoint,
                        region_name="us-east-1",
                        aws_access_key_id=self._settings.camera_s3_access_key.get_secret_value(),
                        aws_secret_access_key=self._settings.camera_s3_secret_key.get_secret_value(),
                        config=self._config,
                    )
                )
            return self._client

    async def put(self, seg: LocalSegment) -> str:
        """Upload the segment and return its object key. A missing file raises
        `FileNotFoundError`; a bucket that does not take it, `UploadError`."""
        key = object_key(self._prefix, seg.printer_id, seg.started_at)
        body = await asyncio.to_thread(seg.path.read_bytes)
        try:
            client = await self.client()
            await client.put_object(
                Bucket=self._settings.camera_s3_bucket,
                Key=key,
                Body=body,
                ContentType="video/mp4",
            )
        except Exception as error:
            raise UploadError(
                f"the bucket did not take segment {seg.started_at.isoformat()} of"
                f" {seg.printer_id}: {_why(error)}"
            ) from None
        return key

    async def aclose(self) -> None:
        await self._exits.aclose()
        self._client = None
