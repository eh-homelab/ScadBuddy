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
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, BinaryIO, Final, get_args

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import ClientError
from opentelemetry.trace import Span, SpanKind

from scadbuddy.core.tracing import span, traced_thread
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
    from types_aiobotocore_s3.type_defs import CompletedPartTypeDef, ObjectTypeDef

logger = logging.getLogger(__name__)

_CHUNK = 1 << 20
#: How long an idle connection is kept for the next request. aiobotocore's default, 12 s,
#: dropped connections DO Spaces still served: on prod (Tempo, 48 h to 2026-11-24) a HEAD
#: after more than 15 s idle took 167-212 ms at the median and up to 3.9 s, against 49 ms
#: on a kept connection; atl1.digitaloceanspaces.com kept an idle connection 60 s and
#: closed it by 90 s, and a new one's TCP and TLS took 80 ms, or 0.6-1.9 s in 3 of 8
#: tries (measured from the homelab LAN on 2026-11-24, #7287). Well under the 60 s.
KEEPALIVE_S: Final = 40
_MISSING = frozenset({"404", "NoSuchKey", "NotFound"})
_OBJECT = re.compile(
    "(" + "|".join(re.escape(kind) for kind in get_args(BlobKind)) + ")/[0-9a-f]{64}"
)


def object_key(prefix: str, kind: BlobKind, sha256: str) -> str:
    return f"{prefix}/{kind}/{sha256}"


#: The storefront's staging area (#6133, spec 2026-11-13 §6): an upload slot's object, and
#: its preview beside it. Nothing here is content-addressed, so `sweep_unrecorded` (which
#: takes `_OBJECT` keys only) never touches it; `storefront_uploads` rows own it.
_STAGED = re.compile(r"(staging/[0-9a-f]{32})(?:\.png|\.[0-9a-f]{16}\.frozen)?")


#: The canned ACL every staging object is written with, signed into its presigned PUT.
STAGED_ACL: Final = "private"
#: The headers a presigned staging PUT carries beside ``Content-Type``.
STAGED_PUT_HEADERS: Final = {"x-amz-acl": STAGED_ACL}


def staging_key(
    prefix: str, upload_id: str, *, preview: bool = False, frozen: str | None = None
) -> str:
    """The staging object of the upload slot ``upload_id``, its preview, or one of its
    frozen copies: a claim attempt's server-side copy, named by that attempt's nonce
    (``frozen``, 16 hex characters), which no presigned URL names, so the customer cannot
    change it after the claim and two attempts at once never write one key (#6133)."""
    if frozen is not None:
        if re.fullmatch(r"[0-9a-f]{16}", frozen) is None:
            raise ValueError(f"not a frozen copy's nonce: {frozen!r}")
        return f"{prefix}/staging/{upload_id}.{frozen}.frozen"
    return f"{prefix}/staging/{upload_id}{'.png' if preview else ''}"


def staged_upload(prefix: str, key: str) -> str | None:
    """The upload's own staging key that a staging object (the upload, its preview or a
    frozen copy) belongs to; None for a key that is not a staging object."""
    found = _STAGED.fullmatch(key.removeprefix(f"{prefix}/")) if key.startswith(prefix) else None
    return None if found is None else f"{prefix}/{found.group(1)}"


@dataclass(frozen=True)
class Frozen:
    """A frozen copy (`S3ContentBackend.freeze_staged`): its size, ETag, and SHA-256 as
    S3 computed it on the copy (base64), or None where the endpoint gave none."""

    size: int
    etag: str
    sha256: str | None


def _missing(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in _MISSING


@contextmanager
def _request(operation: str, kind: str, *, size: int | None = None) -> Iterator[Span]:
    """One span per S3 request (#7245), named as OpenTelemetry's AWS SDK conventions name
    one (``S3.PutObject``). The span carries the kind of object and its size when known,
    never the key, the bucket or a URL.

    Made here, not by ``opentelemetry-instrumentation-botocore``'s
    ``AiobotocoreInstrumentor`` (it wraps ``AioBaseClient._make_api_call``,
    https://github.com/open-telemetry/opentelemetry-python-contrib/blob/f1b9368aa6e2ad4cb0aaa79d341cd19b0b9cd13b/instrumentation/opentelemetry-instrumentation-botocore/src/opentelemetry/instrumentation/botocore/__init__.py):
    by default it injects an X-Ray trace header into every request it sends, and trace
    context never leaves ScadBuddy (spec 2026-10-01 §4); and it knows neither the kind
    of object nor a streamed body's size."""
    attributes: dict[str, str | int] = {
        "rpc.system": "aws-api",
        "rpc.service": "S3",
        "rpc.method": operation,
        "scadbuddy.blob_kind": kind,
    }
    if size is not None:
        attributes["scadbuddy.bytes"] = size
    with span(f"S3.{operation}", kind=SpanKind.CLIENT, attributes=attributes) as current:
        yield current


def _kind_of(prefix: str, key: str) -> str:
    """The kind of object a key names (`object_key`), ``staging`` or ``other``; never the key."""
    first = key.removeprefix(f"{prefix}/").split("/", 1)[0]
    return first if first in get_args(BlobKind) or first == "staging" else "other"


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
            connector_args={"keepalive_timeout": KEEPALIVE_S},
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

    async def upload(
        self,
        kind: BlobKind,
        data: BlobData,
        *,
        name: str,
        scope: BlobScope,
        digest: tuple[str, int] | None = None,
    ) -> str:
        if digest is None:
            digest = await traced_thread(
                "store.digest", blob_digest, data, attributes={"scadbuddy.blob_kind": kind}
            )
        sha256, size = digest
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
            with _request("PutObject", kind, size=size):
                await client.put_object(Bucket=bucket, Key=key, Body=data)
            return key
        try:
            if size <= self.multipart_threshold:
                # The file itself: botocore and aiohttp read it a chunk at a time.
                with _request("PutObject", kind, size=size):
                    await client.put_object(Bucket=bucket, Key=key, Body=data, ContentLength=size)
            else:
                await self._multipart(client, key, data, kind)
        finally:
            data.seek(0)
        return key

    async def _multipart(self, client: S3Client, key: str, data: BinaryIO, kind: str) -> None:
        bucket = self.settings.bucket
        with _request("CreateMultipartUpload", kind):
            created = await client.create_multipart_upload(Bucket=bucket, Key=key)
        upload_id = created["UploadId"]
        try:
            parts: list[CompletedPartTypeDef] = []
            number = 1
            while part := await asyncio.to_thread(data.read, self.part_size):
                with _request("UploadPart", kind, size=len(part)):
                    done = await client.upload_part(
                        Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=part
                    )
                parts.append({"ETag": done["ETag"], "PartNumber": number})
                number += 1
            with _request("CompleteMultipartUpload", kind) as current:
                current.set_attribute("scadbuddy.parts", len(parts))
                await client.complete_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
                )
        except BaseException:
            try:
                with _request("AbortMultipartUpload", kind):
                    await client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            except Exception:
                logger.warning("aborting multipart upload %s failed", upload_id, exc_info=True)
            raise

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        key = self._key(backend_id)
        client = await self._s3()
        # The request up to its headers only: a span held across this generator's
        # `yield` would be current in its consumer. The consumer's own span (the
        # download cache's) covers the body's read.
        with _request("GetObject", _kind_of(self.settings.prefix, key)) as current:
            try:
                response = await client.get_object(Bucket=self.settings.bucket, Key=key)
            except ClientError as error:
                if _missing(error):
                    current.set_attribute("scadbuddy.blob_found", False)
                    raise BlobMissingError(backend_id) from None
                raise
            current.set_attribute("scadbuddy.bytes", response["ContentLength"])
        body = response["Body"]
        async with body:
            async for chunk in body.iter_chunks(_CHUNK):
                yield chunk

    async def exists(self, backend_id: str) -> bool:
        key = self._key(backend_id)
        client = await self._s3()
        with _request("HeadObject", _kind_of(self.settings.prefix, key)) as current:
            try:
                await client.head_object(Bucket=self.settings.bucket, Key=key)
            except ClientError as error:
                if _missing(error):
                    current.set_attribute("scadbuddy.blob_found", False)
                    return False
                raise
            current.set_attribute("scadbuddy.blob_found", True)
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
        with _request("DeleteObject", _kind_of(self.settings.prefix, key)):
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
        with _request("HeadObject", "staging") as current:
            try:
                head = await client.head_object(Bucket=self.settings.bucket, Key=checked)
            except ClientError as error:
                if _missing(error):
                    current.set_attribute("scadbuddy.blob_found", False)
                    return None
                raise
            current.set_attribute("scadbuddy.blob_found", True)
        return int(head["ContentLength"])

    async def freeze_staged(self, key: str, frozen: str) -> Frozen | None:
        """The staging object copied, server side, to ``frozen`` (a key no presigned URL
        names, unique to the claim attempt), with S3 computing the copy's SHA-256
        (``x-amz-checksum-algorithm``, which the Spaces API reference lists for
        CopyObject: https://docs.digitalocean.com/reference/api/spaces/); the copy's size
        and ETag from a ``HEAD`` of it. None when there is no object to copy. No byte
        passes through this process."""
        source, target = self._staged(key), self._staged(frozen)
        client = await self._s3()
        with _request("CopyObject", "staging") as current:
            try:
                copied = await client.copy_object(
                    Bucket=self.settings.bucket,
                    Key=target,
                    CopySource={"Bucket": self.settings.bucket, "Key": source},
                    ACL=STAGED_ACL,
                    ChecksumAlgorithm="SHA256",
                )
            except ClientError as error:
                if _missing(error):
                    current.set_attribute("scadbuddy.blob_found", False)
                    return None
                raise
        with _request("HeadObject", "staging"):
            head = await client.head_object(Bucket=self.settings.bucket, Key=target)
        sha256 = copied.get("CopyObjectResult", {}).get("ChecksumSHA256")
        return Frozen(int(head["ContentLength"]), str(head["ETag"]), sha256 or None)

    async def read_staged(self, key: str, max_bytes: int) -> bytes | None:
        """The staging object's bytes, as opaque bytes (its caller stores them, never opens
        them), or None when there is none or it is past ``max_bytes``."""
        checked = self._staged(key)
        client = await self._s3()
        with _request("GetObject", "staging") as current:
            try:
                response = await client.get_object(Bucket=self.settings.bucket, Key=checked)
            except ClientError as error:
                if _missing(error):
                    current.set_attribute("scadbuddy.blob_found", False)
                    return None
                raise
            body = response["Body"]
            taken = bytearray()
            async with body:
                async for chunk in body.iter_chunks(_CHUNK):
                    taken += chunk
                    if len(taken) > max_bytes:
                        return None
            current.set_attribute("scadbuddy.bytes", len(taken))
        return bytes(taken)

    async def remove_staged(self, key: str) -> None:
        """The staging object deleted (none is no error: S3's delete is idempotent)."""
        checked = self._staged(key)
        client = await self._s3()
        with _request("DeleteObject", "staging"):
            await client.delete_object(Bucket=self.settings.bucket, Key=checked)

    async def remove_staged_of(self, key: str) -> None:
        """Every staging object of the upload ``key`` deleted: the upload, its preview and
        each frozen copy, a losing claim attempt's included."""
        checked = self._staged(key)
        if staged_upload(self.settings.prefix, checked) != checked:
            raise ValueError(f"not an upload's own staging key: {key!r}")
        client = await self._s3()
        async for items in self._listed(client, checked, "staging"):
            for item in items:
                if staged_upload(self.settings.prefix, item["Key"]) == checked:
                    with _request("DeleteObject", "staging"):
                        await client.delete_object(Bucket=self.settings.bucket, Key=item["Key"])

    async def staged_objects(self) -> AsyncGenerator[tuple[str, datetime]]:
        """Every staging object's key and its last write."""
        prefix = f"{self.settings.prefix}/staging/"
        client = await self._s3()
        async for items in self._listed(client, prefix, "staging"):
            for item in items:
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
        async for items in self._listed(client, prefix, "other"):
            for item in items:
                key = item["Key"]
                if not _OBJECT.fullmatch(key.removeprefix(prefix)):
                    continue
                if item["LastModified"] < cutoff and not await asyncio.to_thread(recorded, key):
                    with _request("DeleteObject", _kind_of(self.settings.prefix, key)):
                        await client.delete_object(Bucket=self.settings.bucket, Key=key)
                    removed += 1
        return removed

    async def _listed(
        self, client: S3Client, prefix: str, kind: str
    ) -> AsyncGenerator[list[ObjectTypeDef]]:
        """The objects under ``prefix``, a page at a time, as botocore's paginator pages
        them (``ContinuationToken``). Each page's request is a span of its own, ended
        before the page is handed on: a span held across a ``yield`` would be current
        in the consumer."""
        token: str | None = None
        while True:
            with _request("ListObjectsV2", kind) as current:
                if token is None:
                    page = await client.list_objects_v2(Bucket=self.settings.bucket, Prefix=prefix)
                else:
                    page = await client.list_objects_v2(
                        Bucket=self.settings.bucket, Prefix=prefix, ContinuationToken=token
                    )
                items = page.get("Contents", [])
                current.set_attribute("scadbuddy.objects", len(items))
            yield items
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or token is None:
                return
