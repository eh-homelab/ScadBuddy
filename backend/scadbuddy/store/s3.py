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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, BinaryIO, Final, get_args

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


#: The storefront's staging area (#6133, spec 2026-11-13 §6): an upload slot's object, and
#: its preview beside it. Nothing here is content-addressed, so `sweep_unrecorded` (which
#: takes `_OBJECT` keys only) never touches it; `storefront_uploads` rows own it.
_STAGED = re.compile(r"staging/[0-9a-f]{32}(?:\.png|\.frozen)?")


#: The canned ACL every staging object is written with, signed into its presigned PUT.
STAGED_ACL: Final = "private"
#: The headers a presigned staging PUT carries beside ``Content-Type``.
STAGED_PUT_HEADERS: Final = {"x-amz-acl": STAGED_ACL}


def staging_key(prefix: str, upload_id: str, *, preview: bool = False, frozen: bool = False) -> str:
    """The staging object of the upload slot ``upload_id``, its preview, or its frozen
    copy: the claim's server-side copy, which no presigned URL names, so the customer
    cannot change it after the claim (#6133)."""
    suffix = ".png" if preview else ".frozen" if frozen else ""
    return f"{prefix}/staging/{upload_id}{suffix}"


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
        self._signer: S3Client | None = None
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

    async def _v4(self) -> S3Client:
        """A client that signs with Signature V4 whatever the region: only V4 signs a
        presigned PUT's length and type (`presign_put`), and botocore presigns with V2
        for a client with no region. Kept apart so the other calls sign as before."""
        async with self._opening:
            signer = self._signer
            if signer is None:
                signer = self._signer = await self._exits.enter_async_context(
                    get_session().create_client(
                        "s3",
                        endpoint_url=self.settings.endpoint,
                        region_name=self.settings.region,
                        aws_access_key_id=self.settings.access_key_id,
                        aws_secret_access_key=self.settings.secret_access_key,
                        config=self.config.merge(AioConfig(signature_version="s3v4")),
                    )
                )
            return signer

    async def client(self) -> S3Client:
        """The backend's client, opened on first use."""
        return await self._s3()

    async def aclose(self) -> None:
        await self._exits.aclose()
        self._client = None
        self._signer = None

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

    async def presign_get(
        self,
        backend_id: str,
        expires: timedelta,
        *,
        media_type: str | None = None,
        disposition: str | None = None,
    ) -> str:
        """A URL that GETs the object without credentials until ``expires`` from now
        (aiobotocore's `generate_presigned_url` is a coroutine). ``media_type`` and
        ``disposition`` are the ``Content-Type`` and ``Content-Disposition`` S3 answers
        with (signed into the URL, so the holder cannot change them). The query carries
        the signature: never log it."""
        params: dict[str, str] = {"Bucket": self.settings.bucket, "Key": self._key(backend_id)}
        if media_type is not None:
            params["ResponseContentType"] = media_type
        if disposition is not None:
            params["ResponseContentDisposition"] = disposition
        client = await self._s3()
        return await client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=int(expires.total_seconds())
        )

    async def remove(self, backend_id: str) -> None:
        key = self._key(backend_id)
        client = await self._s3()
        await client.delete_object(Bucket=self.settings.bucket, Key=key)

    def _staged(self, key: str) -> str:
        prefix = f"{self.settings.prefix}/"
        # fullmatch: `$` would also accept a key ending in a newline.
        if not (key.startswith(prefix) and _STAGED.fullmatch(key.removeprefix(prefix))):
            raise ValueError(f"not a staging key: {key!r}")
        return key

    async def presign_put(self, key: str, *, size: int, media_type: str, expires: timedelta) -> str:
        """A URL that PUTs exactly ``size`` bytes of ``media_type`` to the staging object
        ``key`` without credentials until ``expires`` from now. botocore signs the length,
        the type and the canned ACL ``private``
        (``X-Amz-SignedHeaders=content-length;content-type;host;x-amz-acl``), so the store
        refuses a body of any other length or type, and a PUT that asks for any other ACL
        (``public-read``) fails the signature: the browser sends `STAGED_PUT_HEADERS`. The
        query carries the signature: never log it."""
        params = {
            "Bucket": self.settings.bucket,
            "Key": self._staged(key),
            "ContentType": media_type,
            "ContentLength": size,
            "ACL": STAGED_ACL,
        }
        client = await self._v4()
        return await client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=int(expires.total_seconds())
        )

    async def presign_staged_get(
        self,
        key: str,
        expires: timedelta,
        *,
        media_type: str | None = None,
        disposition: str | None = None,
        if_match: str | None = None,
    ) -> str:
        """`presign_get` of a staging object. With ``if_match``, ``If-Match`` is a signed
        header: the caller must send it, and S3 answers 412 for an object of another
        ETag."""
        params: dict[str, str] = {"Bucket": self.settings.bucket, "Key": self._staged(key)}
        if if_match is not None:
            params["IfMatch"] = if_match
        if media_type is not None:
            params["ResponseContentType"] = media_type
        if disposition is not None:
            params["ResponseContentDisposition"] = disposition
        client = await self._s3()
        return await client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=int(expires.total_seconds())
        )

    async def staged_size(self, key: str) -> int | None:
        """The staging object's size by a ``HEAD``, or None when there is none. No byte of
        it is read."""
        checked = self._staged(key)
        client = await self._s3()
        try:
            head = await client.head_object(Bucket=self.settings.bucket, Key=checked)
        except ClientError as error:
            if _missing(error):
                return None
            raise
        return int(head["ContentLength"])

    async def freeze_staged(self, key: str, frozen: str) -> tuple[int, str] | None:
        """The staging object copied, server side, to ``frozen`` (a key no presigned URL
        names), and the copy's size and ETag from a ``HEAD`` of it; None when there is no
        object to copy. No byte passes through this process."""
        source, target = self._staged(key), self._staged(frozen)
        client = await self._s3()
        try:
            await client.copy_object(
                Bucket=self.settings.bucket,
                Key=target,
                CopySource={"Bucket": self.settings.bucket, "Key": source},
                ACL=STAGED_ACL,
            )
        except ClientError as error:
            if _missing(error):
                return None
            raise
        head = await client.head_object(Bucket=self.settings.bucket, Key=target)
        return int(head["ContentLength"]), str(head["ETag"])

    async def read_staged(self, key: str, max_bytes: int) -> bytes | None:
        """The staging object's bytes, as opaque bytes (its caller stores them, never opens
        them), or None when there is none or it is past ``max_bytes``."""
        checked = self._staged(key)
        client = await self._s3()
        try:
            response = await client.get_object(Bucket=self.settings.bucket, Key=checked)
        except ClientError as error:
            if _missing(error):
                return None
            raise
        body = response["Body"]
        taken = bytearray()
        async with body:
            async for chunk in body.iter_chunks(_CHUNK):
                taken += chunk
                if len(taken) > max_bytes:
                    return None
        return bytes(taken)

    async def remove_staged(self, key: str) -> None:
        """The staging object deleted (none is no error: S3's delete is idempotent)."""
        checked = self._staged(key)
        client = await self._s3()
        await client.delete_object(Bucket=self.settings.bucket, Key=checked)

    async def staged_objects(self) -> AsyncGenerator[tuple[str, datetime]]:
        """Every staging object's key and its last write."""
        prefix = f"{self.settings.prefix}/staging/"
        client = await self._s3()
        pages = client.get_paginator("list_objects_v2").paginate(
            Bucket=self.settings.bucket, Prefix=prefix
        )
        async for page in pages:
            for item in page.get("Contents", []):
                key = item["Key"]
                if _STAGED.fullmatch(key.removeprefix(f"{self.settings.prefix}/")):
                    yield key, item["LastModified"]

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
