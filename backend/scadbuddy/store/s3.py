"""The content backend on an S3-compatible bucket (spec
`docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §4.1).

An object's key is `<prefix>/<kind>/<sha256>`, so an upload of bytes the bucket already
has is a `HEAD` and nothing more. A file past `multipart_threshold` goes up in
`part_size` parts: S3 takes at most 5 GiB in one PUT, and every part but the last must
be at least 5 MiB (https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import TYPE_CHECKING, BinaryIO, get_args

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import ClientError

from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.content_models import (
    BlobData,
    BlobKind,
    BlobMissingError,
    BlobScope,
    blob_digest,
)

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client
    from types_aiobotocore_s3.type_defs import CompletedPartTypeDef

logger = logging.getLogger(__name__)

_CHUNK = 1 << 20
_MISSING = frozenset({"404", "NoSuchKey", "NotFound"})
_OBJECT = re.compile(
    "(" + "|".join(re.escape(kind) for kind in get_args(BlobKind)) + ")/[0-9a-f]{64}"
)


def object_key(prefix: str, kind: BlobKind, sha256: str) -> str:
    return f"{prefix}/{kind}/{sha256}"


def _missing(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in _MISSING


class S3ContentBackend:
    backend = "s3"

    def __init__(
        self,
        settings: S3Settings,
        *,
        multipart_threshold: int = 64 << 20,
        part_size: int = 16 << 20,
        config: AioConfig | None = None,
    ) -> None:
        """``config`` is merged over the default, botocore's standard retry mode (three
        attempts, https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html);
        a check that must fail fast passes its own ``retries``."""
        self.settings = settings
        self.multipart_threshold = multipart_threshold
        self.part_size = part_size
        self.config = AioConfig(
            s3={"addressing_style": "path" if settings.path_style else "auto"},
            connect_timeout=5,
            retries={"mode": "standard"},
        )
        if config is not None:
            self.config = self.config.merge(config)
        self._exits = AsyncExitStack()
        self._client: S3Client | None = None
        self._opening = asyncio.Lock()

    def _key(self, backend_id: str) -> str:
        prefix = f"{self.settings.prefix}/"
        # fullmatch: `$` would also accept a key ending in a newline.
        if not (
            backend_id.startswith(prefix) and _OBJECT.fullmatch(backend_id.removeprefix(prefix))
        ):
            raise ValueError(f"not an S3 object key: {backend_id!r}")
        return backend_id

    async def _s3(self) -> S3Client:
        async with self._opening:
            client = self._client
            if client is None:
                client = self._client = await self._exits.enter_async_context(
                    get_session().create_client(
                        "s3",
                        endpoint_url=self.settings.endpoint,
                        region_name=self.settings.region,
                        aws_access_key_id=self.settings.access_key_id,
                        aws_secret_access_key=self.settings.secret_access_key,
                        config=self.config,
                    )
                )
            return client

    async def client(self) -> S3Client:
        """The backend's client, opened on first use."""
        return await self._s3()

    async def aclose(self) -> None:
        await self._exits.aclose()
        self._client = None

    async def upload(self, kind: BlobKind, data: BlobData, *, name: str, scope: BlobScope) -> str:
        sha256, size = await asyncio.to_thread(blob_digest, data)
        key = object_key(self.settings.prefix, kind, sha256)
        if await self.exists(key):
            # Not atomic with the row the caller inserts next: a remove of the last row
            # naming this key, or `sweep_unrecorded`, could delete the object between
            # this HEAD and that insert, leaving a row that names a missing object. That
            # read is a miss, not corruption, and the piece re-renders; and the sweep
            # only removes objects last written before its cutoff. So no lock.
            return key
        client = await self._s3()
        bucket = self.settings.bucket
        if isinstance(data, bytes):
            await client.put_object(Bucket=bucket, Key=key, Body=data)
            return key
        try:
            if size <= self.multipart_threshold:
                # The file itself: botocore and aiohttp read it a chunk at a time.
                await client.put_object(Bucket=bucket, Key=key, Body=data, ContentLength=size)
            else:
                await self._multipart(client, key, data)
        finally:
            data.seek(0)
        return key

    async def _multipart(self, client: S3Client, key: str, data: BinaryIO) -> None:
        bucket = self.settings.bucket
        upload_id = (await client.create_multipart_upload(Bucket=bucket, Key=key))["UploadId"]
        try:
            parts: list[CompletedPartTypeDef] = []
            number = 1
            while part := await asyncio.to_thread(data.read, self.part_size):
                done = await client.upload_part(
                    Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=part
                )
                parts.append({"ETag": done["ETag"], "PartNumber": number})
                number += 1
            await client.complete_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
            )
        except BaseException:
            try:
                await client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            except Exception:
                logger.warning("aborting multipart upload %s failed", upload_id, exc_info=True)
            raise

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        key = self._key(backend_id)
        client = await self._s3()
        try:
            response = await client.get_object(Bucket=self.settings.bucket, Key=key)
        except ClientError as error:
            if _missing(error):
                raise BlobMissingError(backend_id) from None
            raise
        body = response["Body"]
        async with body:
            async for chunk in body.iter_chunks(_CHUNK):
                yield chunk

    async def exists(self, backend_id: str) -> bool:
        key = self._key(backend_id)
        client = await self._s3()
        try:
            await client.head_object(Bucket=self.settings.bucket, Key=key)
        except ClientError as error:
            if _missing(error):
                return False
            raise
        return True

    async def remove(self, backend_id: str) -> None:
        key = self._key(backend_id)
        client = await self._s3()
        await client.delete_object(Bucket=self.settings.bucket, Key=key)

    async def sweep_unrecorded(self, *, cutoff: datetime, recorded: Callable[[str], bool]) -> int:
        """Delete every object under the prefix last written before ``cutoff`` that
        ``recorded`` (given the object's key) says no `store_blobs` row names. A naive
        ``cutoff`` is UTC; an object whose key is not one this backend writes is left."""
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        prefix = f"{self.settings.prefix}/"
        client = await self._s3()
        removed = 0
        pages = client.get_paginator("list_objects_v2").paginate(
            Bucket=self.settings.bucket, Prefix=prefix
        )
        async for page in pages:
            for item in page.get("Contents", []):
                key = item["Key"]
                if not _OBJECT.fullmatch(key.removeprefix(prefix)):
                    continue
                if item["LastModified"] < cutoff and not await asyncio.to_thread(recorded, key):
                    await client.delete_object(Bucket=self.settings.bucket, Key=key)
                    removed += 1
        return removed
