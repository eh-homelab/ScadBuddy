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
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, BinaryIO, Final, get_args

from aiobotocore.config import AioConfig
from aiobotocore.session import AioSession, get_session
from aiohttp import ClientConnectorError
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError
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

#: What a new S3 client reads from botocore's data files: its loader lists every
#: service it knows (a directory listing and a file check each) and reads these.
_S3_MODELS: Final = ("service-2", "endpoint-rule-set-1")
_SHARED_DATA: Final = ("endpoints", "partitions", "_retry", "sdk-default-configuration")


def _load_s3_data(session: AioSession) -> None:
    """Read what ``session.create_client("s3")`` reads into the session's loader, which
    keeps it (botocore's ``instance_cache``). Blocking file reads: run it off the loop.
    On an API pod that had just started, the client's own read held the loop 1.3 s
    (#7359)."""
    loader = session.get_component("data_loader")
    for model in _S3_MODELS:
        # As botocore calls it: its cache keys on the keyword too.
        loader.load_service_model("s3", model, api_version=None)
    for name in _SHARED_DATA:
        loader.load_data(name)


_CHUNK = 1 << 20
#: How long an idle connection is kept for the next request. aiobotocore's default, 12 s,
#: dropped connections DO Spaces still served: on prod (Tempo, 48 h to 2026-11-24) a HEAD
#: after more than 15 s idle took 167-212 ms at the median and up to 3.9 s, against 49 ms
#: on a kept connection; atl1.digitaloceanspaces.com kept an idle connection 60 s and
#: closed it by 90 s, and a new one's TCP and TLS took 80 ms, or 0.6-1.9 s in 3 of 8
#: tries (measured from the homelab LAN on 2026-11-24, #7287). Well under the 60 s.
KEEPALIVE_S: Final = 40
_MISSING = frozenset({"404", "NoSuchKey", "NotFound"})
#: The waits before each retry of a content request whose connection was never made
#: (`connect_failed`, #8471), on top of botocore's own standard-mode attempts.
CONNECT_BACKOFF: Final = (0.5, 1.0, 2.0)
_OBJECT = re.compile(
    "(" + "|".join(re.escape(kind) for kind in get_args(BlobKind)) + ")/[0-9a-f]{64}"
)


def object_key(prefix: str, kind: BlobKind, sha256: str) -> str:
    return f"{prefix}/{kind}/{sha256}"


#: The storefront's staging area (#6133, spec 2026-11-13 §6): an upload slot's object, and
#: its preview beside it. Nothing here is content-addressed, so `sweep_unrecorded` (which
#: takes `_OBJECT` keys only) never touches it; `storefront_uploads` rows own it. Core's
#: blob uploads (#7958, `tools/go/core/internal/blobstore` `StagingKey`) PUT to a
#: `staging/<32 hex>` key no row names, which their commit deletes; one left behind goes
#: with `orders/storefront/staged_files.py` `sweep_staged` past its `ORPHAN_AGE`.
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


def connect_failed(error: BaseException) -> bool:
    """Whether ``error`` is a request whose connection was never made, so no byte of it
    reached the store (#8471): a connect timeout, or an ``EndpointConnectionError`` over
    a refused connection or a failed name lookup. aiobotocore 3.10 raises
    ``EndpointConnectionError`` for any aiohttp ``ClientConnectionError`` too
    (``aiobotocore/httpsession.py``), a reset in the middle of a body included, so only
    those two causes count. A read timeout (``ReadTimeoutError``) or a closed connection
    (``ConnectionClosedError``) follows a sent request and is never one."""
    if isinstance(error, ConnectTimeoutError):
        return True
    if isinstance(error, EndpointConnectionError):
        cause = error.kwargs.get("error")
        return isinstance(cause, ClientConnectorError | socket.gaierror)
    return False


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
        connect_backoff: Sequence[float] = CONNECT_BACKOFF,
    ) -> None:
        """``config`` is merged over the default, botocore's standard retry mode (three
        attempts, https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html);
        a check that must fail fast passes its own ``retries``. A store request whose
        connection was never made is tried again after each wait of ``connect_backoff``
        (`_connect_retried`, #8471)."""
        self.settings = settings
        self.connect_backoff = tuple(connect_backoff)
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
        self._session: AioSession | None = None
        self._opening = asyncio.Lock()

    async def _loaded_session(self) -> AioSession:
        """One session for both clients, its data files read in a thread (#7359).
        Called under `_opening`."""
        if self._session is None:
            session = get_session()
            await asyncio.to_thread(_load_s3_data, session)
            self._session = session
        return self._session

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
                session = await self._loaded_session()
                client = self._client = await self._exits.enter_async_context(
                    session.create_client(
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
                session = await self._loaded_session()
                signer = self._signer = await self._exits.enter_async_context(
                    session.create_client(
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

    async def _connect_retried[T](self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        """``call`` (one request), made again after each wait of `connect_backoff` while
        it fails before its connection was made (`connect_failed`). No byte of such a
        request reached the store, so another try repeats no effect. Any other error, a
        read timeout after the request was sent included, propagates at once. On
        2026-11-28 one ``entity_attachment_add`` failed on a connect timeout to DO Spaces
        that outlasted botocore's own three attempts, 7 minutes after its pod started
        (#8471)."""
        for wait in self.connect_backoff:
            try:
                return await call()
            except (ConnectTimeoutError, EndpointConnectionError) as error:
                if not connect_failed(error):
                    raise
                logger.warning(
                    "the store took no connection; trying the request again",
                    extra={"operation": operation, "wait_s": wait, "error": type(error).__name__},
                )
            await asyncio.sleep(wait)
        return await call()

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
            # only removes objects last written before its cutoff. So no lock here. A save
            # (`ContentStore.stage` with a `StageHold`) holds the object's stage lock
            # across this HEAD, which every removal claims (#7477); other puts take none.
            return key
        client = await self._s3()
        bucket = self.settings.bucket
        if isinstance(data, bytes):
            body = data

            async def put_bytes() -> None:
                with _request("PutObject", kind, size=size):
                    await client.put_object(Bucket=bucket, Key=key, Body=body)

            # The key is the bytes' sha256: a second PUT writes the same object.
            await self._connect_retried("PutObject", put_bytes)
            return key
        try:
            if size <= self.multipart_threshold:
                stream = data

                async def put_file() -> None:
                    stream.seek(0)
                    # The file itself: botocore and aiohttp read it a chunk at a time.
                    with _request("PutObject", kind, size=size):
                        await client.put_object(
                            Bucket=bucket, Key=key, Body=stream, ContentLength=size
                        )

                await self._connect_retried("PutObject", put_file)
            else:
                await self._multipart(client, key, data, kind)
        finally:
            data.seek(0)
        return key

    async def _multipart(self, client: S3Client, key: str, data: BinaryIO, kind: str) -> None:
        bucket = self.settings.bucket

        async def create() -> str:
            with _request("CreateMultipartUpload", kind):
                created = await client.create_multipart_upload(Bucket=bucket, Key=key)
            return created["UploadId"]

        upload_id = await self._connect_retried("CreateMultipartUpload", create)
        try:
            parts: list[CompletedPartTypeDef] = []
            number = 1
            while part := await asyncio.to_thread(data.read, self.part_size):
                # Bound as defaults: the loop moves `part` and `number` on.
                async def put_part(part: bytes = part, number: int = number) -> str:
                    with _request("UploadPart", kind, size=len(part)):
                        done = await client.upload_part(
                            Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=part
                        )
                    return done["ETag"]

                # A part sent again under its number replaces the first copy.
                etag = await self._connect_retried("UploadPart", put_part)
                parts.append({"ETag": etag, "PartNumber": number})
                number += 1

            async def complete() -> None:
                with _request("CompleteMultipartUpload", kind) as current:
                    current.set_attribute("scadbuddy.parts", len(parts))
                    await client.complete_multipart_upload(
                        Bucket=bucket,
                        Key=key,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    )

            await self._connect_retried("CompleteMultipartUpload", complete)
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

        async def get() -> Any:
            with _request("GetObject", _kind_of(self.settings.prefix, key)) as current:
                try:
                    response = await client.get_object(Bucket=self.settings.bucket, Key=key)
                except ClientError as error:
                    if _missing(error):
                        current.set_attribute("scadbuddy.blob_found", False)
                        raise BlobMissingError(backend_id) from None
                    raise
                current.set_attribute("scadbuddy.bytes", response["ContentLength"])
            return response

        response = await self._connect_retried("GetObject", get)
        body = response["Body"]
        async with body:
            async for chunk in body.iter_chunks(_CHUNK):
                yield chunk

    async def exists(self, backend_id: str) -> bool:
        key = self._key(backend_id)
        client = await self._s3()

        async def head() -> bool:
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

        return await self._connect_retried("HeadObject", head)

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
        await self._delete(await self._s3(), key, _kind_of(self.settings.prefix, key))

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

        async def head() -> int | None:
            with _request("HeadObject", "staging") as current:
                try:
                    answer = await client.head_object(Bucket=self.settings.bucket, Key=checked)
                except ClientError as error:
                    if _missing(error):
                        current.set_attribute("scadbuddy.blob_found", False)
                        return None
                    raise
                current.set_attribute("scadbuddy.blob_found", True)
            return int(answer["ContentLength"])

        return await self._connect_retried("HeadObject", head)

    async def freeze_staged(self, key: str, frozen: str) -> Frozen | None:
        """The staging object copied, server side, to ``frozen`` (a key no presigned URL
        names, unique to the claim attempt), with S3 computing the copy's SHA-256
        (``x-amz-checksum-algorithm``, which the Spaces API reference lists for
        CopyObject: https://docs.digitalocean.com/reference/api/spaces/); the copy's size
        and ETag from a ``HEAD`` of it. None when there is no object to copy. No byte
        passes through this process."""
        source, target = self._staged(key), self._staged(frozen)
        client = await self._s3()

        async def copy() -> Any:
            with _request("CopyObject", "staging") as current:
                try:
                    return await client.copy_object(
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

        async def head_copy() -> Any:
            with _request("HeadObject", "staging"):
                return await client.head_object(Bucket=self.settings.bucket, Key=target)

        # The target is unique to the claim attempt: a second copy writes the same object.
        copied = await self._connect_retried("CopyObject", copy)
        if copied is None:
            return None
        head = await self._connect_retried("HeadObject", head_copy)
        sha256 = copied.get("CopyObjectResult", {}).get("ChecksumSHA256")
        return Frozen(int(head["ContentLength"]), str(head["ETag"]), sha256 or None)

    async def read_staged(self, key: str, max_bytes: int) -> bytes | None:
        """The staging object's bytes, as opaque bytes (its caller stores them, never opens
        them), or None when there is none or it is past ``max_bytes``."""
        checked = self._staged(key)
        client = await self._s3()

        async def get() -> bytes | None:
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

        # A failure during the body is not a connect failure, so only a GET that never
        # connected is made again, and it reads the body from its start.
        return await self._connect_retried("GetObject", get)

    async def remove_staged(self, key: str) -> None:
        """The staging object deleted (none is no error: S3's delete is idempotent)."""
        await self._delete(await self._s3(), self._staged(key), "staging")

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
                    await self._delete(client, item["Key"], "staging")

    async def staged_objects(self) -> AsyncGenerator[tuple[str, datetime]]:
        """Every staging object's key and its last write."""
        prefix = f"{self.settings.prefix}/staging/"
        client = await self._s3()
        async for items in self._listed(client, prefix, "staging"):
            for item in items:
                key = item["Key"]
                if _STAGED.fullmatch(key.removeprefix(f"{self.settings.prefix}/")):
                    yield key, item["LastModified"]

    async def sweep_unrecorded(
        self,
        *,
        cutoff: datetime,
        recorded: Callable[[str], bool],
        claim: Callable[[str], AbstractAsyncContextManager[bool]] | None = None,
    ) -> int:
        """Delete every object under the prefix last written before ``cutoff`` that
        ``recorded`` (given the object's key) says no `store_blobs` row names. A naive
        ``cutoff`` is UTC; an object whose key is not one this backend writes is left.
        With ``claim`` (`ContentStore.claim_object`), such an object is then claimed, which
        checks the rows again under its lock, and is deleted only inside the claim's block:
        one a save uploaded and has not indexed yet stays,
        even when an older upload of the same bytes made it (#7477). A save that died
        before its commit holds no lock, so its objects go once past ``cutoff``."""
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
                if item["LastModified"] >= cutoff:
                    continue
                # One query for an object a row names, the most of them; only one no row
                # names is claimed (#7477 review M3).
                if await asyncio.to_thread(recorded, key):
                    continue
                if claim is None:
                    await self._delete_unrecorded(client, key)
                    removed += 1
                    continue
                async with claim(key) as free:
                    if free:
                        await self._delete_unrecorded(client, key)
                        removed += 1
        return removed

    async def _delete_unrecorded(self, client: S3Client, key: str) -> None:
        await self._delete(client, key, _kind_of(self.settings.prefix, key))

    async def _delete(self, client: S3Client, key: str, kind: str) -> None:
        """One ``DeleteObject``, made again after a connect failure: S3's delete is
        idempotent, so a second one of a gone object is no error."""

        async def delete() -> None:
            with _request("DeleteObject", kind):
                await client.delete_object(Bucket=self.settings.bucket, Key=key)

        await self._connect_retried("DeleteObject", delete)

    async def _listed(
        self, client: S3Client, prefix: str, kind: str
    ) -> AsyncGenerator[list[ObjectTypeDef]]:
        """The objects under ``prefix``, a page at a time, as botocore's paginator pages
        them (``ContinuationToken``). Each page's request is a span of its own, ended
        before the page is handed on: a span held across a ``yield`` would be current
        in the consumer."""
        token: str | None = None
        while True:

            async def list_page(token: str | None = token) -> Any:
                with _request("ListObjectsV2", kind) as current:
                    if token is None:
                        page = await client.list_objects_v2(
                            Bucket=self.settings.bucket, Prefix=prefix
                        )
                    else:
                        page = await client.list_objects_v2(
                            Bucket=self.settings.bucket, Prefix=prefix, ContinuationToken=token
                        )
                    current.set_attribute("scadbuddy.objects", len(page.get("Contents", [])))
                return page

            # A list reads only, so a second one of the same page changes nothing.
            page = await self._connect_retried("ListObjectsV2", list_page)
            items = page.get("Contents", [])
            yield items
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or token is None:
                return
