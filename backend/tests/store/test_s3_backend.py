from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import socket
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from aiobotocore.config import AioConfig
from aiohttp import ClientConnectorError, ClientOSError
from aiohttp.client_reqrep import ConnectionKey
from botocore.exceptions import (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.loaders import JSONFileLoader

from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.content import ContentBackend
from scadbuddy.store.content_models import BlobMissingError, BlobScope
from scadbuddy.store.s3 import (
    CONNECT_BACKOFF,
    KEEPALIVE_S,
    STAGED_PUT_HEADERS,
    S3ContentBackend,
    connect_failed,
    object_key,
    staged_upload,
    staging_key,
)

SCOPE = BlobScope(slug="demo", title="Demo")
MISSING = "t/asset/" + "0" * 64


def _conforms(backend: S3ContentBackend) -> ContentBackend:
    return backend


class RecordedReads(io.BufferedReader):
    """A file that remembers the size asked of every read."""

    def __init__(self, path: Path) -> None:
        super().__init__(io.FileIO(path, "rb"))
        self.asked: list[int | None] = []

    def read(self, size: int | None = -1, /) -> bytes:
        self.asked.append(size)
        return super().read(size)


def test_object_key_is_prefix_kind_sha() -> None:
    assert object_key("p", "font", "ab") == "p/font/ab"


async def test_the_fixture_bucket_is_served(s3: S3Settings) -> None:
    """The `s3` fixture makes its bucket through moto's internals (#6301). A moto release
    that moves them fails here, not as a `NoSuchBucket` in another test (#6495)."""
    backend = S3ContentBackend(s3)
    try:
        client = await backend.client()
        listed = await client.list_buckets()
        assert [bucket["Name"] for bucket in listed["Buckets"]] == [s3.bucket]
    finally:
        await backend.aclose()


async def test_the_client_retries_in_standard_mode_by_default(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    try:
        client = await backend.client()
        # Config's options are set at run time, untyped: read them from its __dict__.
        assert vars(client.meta.config)["retries"] == {"mode": "standard"}
        assert vars(client.meta.config)["s3"] == {"addressing_style": "path"}
    finally:
        await backend.aclose()


async def test_an_idle_connection_is_kept_past_aiobotocores_twelve_seconds(
    s3: S3Settings,
) -> None:
    """A request after more than 12 s idle opened a new TLS connection on prod (#7287)."""
    backend = S3ContentBackend(s3)
    try:
        client: Any = await backend.client()
        assert not await backend.exists(MISSING)
        # The aiohttp session aiobotocore made for the request: private, so read it here.
        sessions: dict[str | None, Any] = client._endpoint.http_session._sessions
        assert sessions[None].connector._keepalive_timeout == KEEPALIVE_S
    finally:
        await backend.aclose()


async def test_a_given_config_replaces_the_retries(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3, config=AioConfig(retries={"max_attempts": 1}))
    assert vars(backend.config)["retries"] == {"max_attempts": 1}
    assert vars(backend.config)["s3"] == {"addressing_style": "path"}
    await backend.aclose()


async def test_the_clients_read_botocores_data_files_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7359: a new client lists every service botocore knows and reads its JSON models.
    On a pod that had just started, that held the API's loop for 1.3 s; it runs in a
    thread now, and both clients take it from one loader."""
    loop_thread = threading.get_ident()
    on_loop: list[str] = []
    read: list[str] = []
    exists, load_file = JSONFileLoader.exists, JSONFileLoader.load_file

    def watched_exists(self: JSONFileLoader, file_path: str) -> bool:
        if threading.get_ident() == loop_thread:
            on_loop.append(file_path)
        return exists(self, file_path)

    def watched_load_file(self: JSONFileLoader, file_path: str) -> Any:
        read.append(file_path)
        if threading.get_ident() == loop_thread:
            on_loop.append(file_path)
        return load_file(self, file_path)

    monkeypatch.setattr(JSONFileLoader, "exists", watched_exists)
    monkeypatch.setattr(JSONFileLoader, "load_file", watched_load_file)
    backend = S3ContentBackend(
        S3Settings(
            endpoint="http://127.0.0.1:9",
            bucket="sb",
            prefix="t",
            access_key_id="testing",
            secret_access_key="testing",
        )
    )
    try:
        await backend.client()
        once = len(read)
        await backend.presign_put(
            staging_key("t", STAGED), size=5, media_type="model/stl", expires=timedelta(minutes=1)
        )
    finally:
        await backend.aclose()
    assert any(path.endswith("service-2") for path in read)
    assert on_loop == []
    assert len(read) == once  # the signing client loaded nothing again


async def test_upload_returns_the_prefixed_kind_sha_key(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    try:
        key = await backend.upload("asset", b"<svg/>", name="a.svg", scope=SCOPE)
        assert key == f"t/asset/{hashlib.sha256(b'<svg/>').hexdigest()}"
        assert await backend.exists(key)
        assert await backend.upload("asset", b"<svg/>", name="b.svg", scope=SCOPE) == key
    finally:
        await backend.aclose()


async def test_download_streams_the_bytes_and_a_missing_key_raises(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    try:
        key = await backend.upload("font", b"glyphs", name="f.ttf", scope=SCOPE)
        assert b"".join([c async for c in backend.download(key)]) == b"glyphs"
        assert not await backend.exists(MISSING)
        with pytest.raises(BlobMissingError):
            [c async for c in backend.download(MISSING)]
    finally:
        await backend.aclose()


async def test_a_file_over_the_multipart_threshold_round_trips(
    s3: S3Settings, tmp_path: Path
) -> None:
    backend = S3ContentBackend(s3, multipart_threshold=5 << 20, part_size=5 << 20)
    data = os.urandom(11 << 20)
    f = tmp_path / "big"
    f.write_bytes(data)
    try:
        with f.open("rb") as fh:
            key = await backend.upload("piece", fh, name="piece.zip", scope=SCOPE)
            assert fh.tell() == 0
        assert key == f"t/piece/{hashlib.sha256(data).hexdigest()}"
        assert b"".join([c async for c in backend.download(key)]) == data
    finally:
        await backend.aclose()


async def test_a_small_file_round_trips(s3: S3Settings, tmp_path: Path) -> None:
    backend = S3ContentBackend(s3)
    f = tmp_path / "small"
    f.write_bytes(b"small file")
    try:
        with f.open("rb") as fh:
            key = await backend.upload("snapshot", fh, name="s.zip", scope=SCOPE)
            assert fh.tell() == 0
        assert b"".join([c async for c in backend.download(key)]) == b"small file"
    finally:
        await backend.aclose()


async def test_a_file_under_the_threshold_is_never_read_whole(
    s3: S3Settings, tmp_path: Path
) -> None:
    backend = S3ContentBackend(s3, multipart_threshold=8 << 20)
    data = os.urandom((8 << 20) - 1)
    f = tmp_path / "under"
    f.write_bytes(data)
    try:
        with RecordedReads(f) as fh:
            key = await backend.upload("piece", fh, name="piece.zip", scope=SCOPE)
            assert fh.tell() == 0
        assert fh.asked
        assert all(size is not None and 0 < size <= 1 << 20 for size in fh.asked), fh.asked
        assert b"".join([c async for c in backend.download(key)]) == data
    finally:
        await backend.aclose()


async def _failing_multipart(
    s3: S3Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, abort_fails: bool
) -> S3ContentBackend:
    backend = S3ContentBackend(s3, multipart_threshold=5 << 20, part_size=5 << 20)
    client = await backend._s3()
    upload_part = client.upload_part
    calls = 0

    async def second_part_fails(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("part lost")
        return await upload_part(**kwargs)

    async def abort_refused(**kwargs: Any) -> Any:
        raise RuntimeError("abort refused")

    monkeypatch.setattr(client, "upload_part", second_part_fails)
    if abort_fails:
        monkeypatch.setattr(client, "abort_multipart_upload", abort_refused)
    f = tmp_path / "big"
    f.write_bytes(os.urandom(11 << 20))
    with f.open("rb") as fh, pytest.raises(RuntimeError, match="part lost"):
        await backend.upload("piece", fh, name="piece.zip", scope=SCOPE)
    assert calls == 2
    return backend


async def test_a_failed_multipart_upload_is_aborted(
    s3: S3Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = await _failing_multipart(s3, tmp_path, monkeypatch, abort_fails=False)
    try:
        client = await backend._s3()
        assert "Uploads" not in await client.list_multipart_uploads(Bucket=s3.bucket)
    finally:
        await backend.aclose()


async def test_a_failed_abort_keeps_the_original_error(
    s3: S3Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = await _failing_multipart(s3, tmp_path, monkeypatch, abort_fails=True)
    await backend.aclose()


@pytest.mark.parametrize(
    "backend_id",
    [
        "other/asset/" + "0" * 64,
        "asset/" + "0" * 64,
        "t/model/" + "0" * 64,
        "t/asset/" + "0" * 63,
        "t/asset/" + "0" * 64 + "\n",
        "t/asset/../" + "0" * 64,
    ],
)
async def test_a_key_outside_the_store_is_refused(backend_id: str) -> None:
    backend = S3ContentBackend(
        S3Settings(
            endpoint="http://127.0.0.1:1",
            bucket="sb-test",
            prefix="t",
            access_key_id="testing",
            secret_access_key="testing",
        )
    )
    with pytest.raises(ValueError, match="not an S3 object key"):
        await backend.exists(backend_id)
    with pytest.raises(ValueError, match="not an S3 object key"):
        [c async for c in backend.download(backend_id)]
    with pytest.raises(ValueError, match="not an S3 object key"):
        await backend.remove(backend_id)
    await backend.aclose()


async def test_remove_is_idempotent(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    try:
        key = await backend.upload("asset", b"gone", name="g", scope=SCOPE)
        await backend.remove(key)
        assert not await backend.exists(key)
        await backend.remove(key)
    finally:
        await backend.aclose()


async def test_a_connection_error_is_not_a_missing_object() -> None:
    # A port just closed refuses at once; one nobody listens on may only time out.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    backend = S3ContentBackend(
        S3Settings(
            endpoint=f"http://127.0.0.1:{port}",
            bucket="sb",
            prefix="t",
            access_key_id="testing",
            secret_access_key="testing",
        ),
        # One attempt: a refused connection fails at once, not after standard's retries.
        config=AioConfig(retries={"max_attempts": 1}),
        connect_backoff=(),
    )
    try:
        # Not a BlobMissingError, which would drop the index row of a blob still there.
        with pytest.raises(EndpointConnectionError):
            await backend.exists(MISSING)
        with pytest.raises(EndpointConnectionError):
            [c async for c in backend.download(MISSING)]
    finally:
        await backend.aclose()


async def test_sweep_unrecorded_removes_old_unrecorded_objects_only(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    try:
        kept = await backend.upload("piece", b"recorded", name="a", scope=SCOPE)
        client = await backend._s3()
        await client.put_object(Bucket=s3.bucket, Key="t/stray.txt", Body=b"not ours")
        swept = await backend.upload("piece", b"orphan", name="b", scope=SCOPE)
        cutoff = datetime.now(UTC)
        # S3's LastModified is whole seconds: the next upload must land in a later one.
        await asyncio.sleep(1.1)
        young = await backend.upload("piece", b"young", name="c", scope=SCOPE)
        seen: list[str] = []
        loop_thread = threading.current_thread()

        def recorded(key: str) -> bool:
            # A Postgres query: it must not block the event loop.
            assert threading.current_thread() is not loop_thread
            seen.append(key)
            return key == kept

        naive = cutoff.replace(tzinfo=None)
        assert await backend.sweep_unrecorded(cutoff=naive, recorded=recorded) == 1
        assert await backend.exists(kept)
        assert not await backend.exists(swept)
        assert await backend.exists(young)
        assert young not in seen
        assert "t/stray.txt" not in seen
        assert "Contents" in await client.list_objects_v2(Bucket=s3.bucket, Prefix="t/stray")
    finally:
        await backend.aclose()


async def test_a_slice_blob_round_trips(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    try:
        key = await backend.upload("slice", b"gcode", name="plate.3mf", scope=SCOPE)
        assert key == f"t/slice/{hashlib.sha256(b'gcode').hexdigest()}"
        assert b"".join([c async for c in backend.download(key)]) == b"gcode"
    finally:
        await backend.aclose()


# -- the storefront's staging area (#6133, spec 2026-11-13 §6) ---------------------------

STAGED = "0123456789abcdef0123456789abcdef"


async def test_a_presigned_put_signs_its_length_and_type_and_writes_one_staging_key(
    s3: S3Settings,
) -> None:
    backend = S3ContentBackend(s3)
    try:
        key = staging_key(s3.prefix, STAGED)
        assert key == f"t/staging/{STAGED}"
        url = await backend.presign_put(
            key, size=5, media_type="model/stl", expires=timedelta(minutes=15)
        )
        query = parse_qs(urlsplit(url).query)
        assert query["X-Amz-SignedHeaders"] == ["content-length;content-type;host;x-amz-acl"]
        assert query["X-Amz-Expires"] == ["900"]
        sent = {"Content-Type": "model/stl", **STAGED_PUT_HEADERS}
        # moto checks no signature, so a PUT with another ACL is not refused here; the
        # signed ``x-amz-acl`` is what makes S3 refuse it (SigV4, signed headers).
        async with httpx.AsyncClient() as http:
            answer = await http.put(url, content=b"solid", headers=sent)
        answer.raise_for_status()
        assert await backend.staged_size(key) == 5
        assert await backend.staged_size(staging_key(s3.prefix, "f" * 32)) is None
        got = await backend.presign_staged_get(key, timedelta(minutes=5))
        async with httpx.AsyncClient() as http:
            assert (await http.get(got)).content == b"solid"
        await backend.remove_staged(key)
        assert await backend.staged_size(key) is None
    finally:
        await backend.aclose()


@pytest.mark.parametrize(
    "key",
    [
        "t/asset/" + "0" * 64,
        f"t/staging/{STAGED}/x",
        "t/staging/../asset/" + "0" * 64,
        f"other/staging/{STAGED}",
        "t/staging/ABC",
        f"t/staging/{STAGED}\n",
    ],
)
async def test_staging_calls_refuse_any_other_key(s3: S3Settings, key: str) -> None:
    backend = S3ContentBackend(s3)
    try:
        with pytest.raises(ValueError):
            await backend.presign_put(
                key, size=1, media_type="model/stl", expires=timedelta(minutes=1)
            )
        with pytest.raises(ValueError):
            await backend.staged_size(key)
        with pytest.raises(ValueError):
            await backend.remove_staged(key)
        with pytest.raises(ValueError):
            await backend.presign_staged_get(key, timedelta(minutes=1))
    finally:
        await backend.aclose()


def test_a_preview_key_sits_beside_its_upload() -> None:
    assert staging_key("t", STAGED, preview=True) == f"t/staging/{STAGED}.png"


async def test_a_frozen_copy_keeps_the_bytes_of_the_moment_it_was_made(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    source = staging_key("t", STAGED)
    frozen = staging_key("t", STAGED, frozen="0123456789abcdef")
    assert frozen == f"t/staging/{STAGED}.0123456789abcdef.frozen"
    assert staged_upload("t", frozen) == staged_upload("t", f"{source}.png") == source
    with pytest.raises(ValueError):
        staging_key("t", STAGED, frozen="../x")
    try:
        assert await backend.freeze_staged(source, frozen) is None  # nothing to copy
        client = await backend._s3()
        await client.put_object(Bucket=s3.bucket, Key=source, Body=b"first")
        copy = await backend.freeze_staged(source, frozen)
        assert copy is not None and copy.size == 5 and copy.etag.startswith('"')
        if copy.sha256 is not None:  # S3 computed it on the copy
            assert copy.sha256 == base64.b64encode(hashlib.sha256(b"first").digest()).decode()
        await client.put_object(Bucket=s3.bucket, Key=source, Body=b"later")
        held = await client.get_object(Bucket=s3.bucket, Key=frozen, IfMatch=copy.etag)
        assert await held["Body"].read() == b"first"
    finally:
        await backend.aclose()


async def test_an_uploads_staging_objects_go_together(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    other = "f" * 32
    keys = [
        staging_key("t", STAGED),
        staging_key("t", STAGED, preview=True),
        staging_key("t", STAGED, frozen="0123456789abcdef"),
        staging_key("t", STAGED, frozen="fedcba9876543210"),
        staging_key("t", other),
    ]
    try:
        client = await backend._s3()
        for key in keys:
            await client.put_object(Bucket=s3.bucket, Key=key, Body=b"x")
        await backend.remove_staged_of(staging_key("t", STAGED))
        assert [key async for key, _ in backend.staged_objects()] == [staging_key("t", other)]
        with pytest.raises(ValueError):
            await backend.remove_staged_of(staging_key("t", STAGED, preview=True))
    finally:
        await backend.aclose()


async def test_staged_objects_lists_the_staging_area_only(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    try:
        client = await backend._s3()
        await client.put_object(Bucket=s3.bucket, Key=staging_key("t", STAGED), Body=b"x")
        await backend.upload("piece", b"recorded", name="a", scope=SCOPE)
        listed = [key async for key, _ in backend.staged_objects()]
        assert listed == [staging_key("t", STAGED)]
    finally:
        await backend.aclose()


async def test_sweep_unrecorded_never_touches_the_staging_area(s3: S3Settings) -> None:
    backend = S3ContentBackend(s3)
    try:
        client = await backend._s3()
        await client.put_object(Bucket=s3.bucket, Key=staging_key("t", STAGED), Body=b"x")
        await asyncio.sleep(1.1)
        swept = await backend.sweep_unrecorded(cutoff=datetime.now(UTC), recorded=lambda _: False)
        assert swept == 0
        assert await backend.staged_size(staging_key("t", STAGED)) == 1
    finally:
        await backend.aclose()


URL = "https://store.test/t/piece/" + "0" * 64


def _refused() -> EndpointConnectionError:
    key = ConnectionKey("store.test", 443, True, True, None, None, None)
    return EndpointConnectionError(
        endpoint_url=URL, error=ClientConnectorError(key, ConnectionRefusedError(111, "refused"))
    )


def test_only_a_connection_never_made_is_a_connect_failure() -> None:
    """#8471: a connect timeout, a refused connection and a failed lookup sent no byte;
    a read timeout, a closed connection or a reset during the body may have."""
    assert connect_failed(ConnectTimeoutError(endpoint_url=URL))
    assert connect_failed(_refused())
    assert connect_failed(
        EndpointConnectionError(endpoint_url=URL, error=socket.gaierror(-2, "unknown name"))
    )
    assert not connect_failed(ReadTimeoutError(endpoint_url=URL))
    assert not connect_failed(ConnectionClosedError(endpoint_url=URL))
    # aiobotocore wraps any aiohttp `ClientConnectionError` so, a reset mid-body included.
    reset = EndpointConnectionError(endpoint_url=URL, error=ClientOSError(104, "reset"))
    assert not connect_failed(reset)
    assert not connect_failed(RuntimeError("no"))


def _failing_first(real: Any, errors: list[Exception], calls: list[int]) -> Any:
    async def call(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if errors:
            raise errors.pop(0)
        return await real(*args, **kwargs)

    return call


async def test_a_connect_timeout_on_a_read_is_tried_again(
    s3: S3Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#8471: the HEAD that `claim` and `upload` make, and the GET of a download."""
    backend = S3ContentBackend(s3, connect_backoff=(0, 0, 0))
    try:
        key = await backend.upload("piece", b"kept", name="p", scope=SCOPE)
        client = await backend._s3()
        heads: list[int] = []
        monkeypatch.setattr(
            client,
            "head_object",
            _failing_first(client.head_object, [ConnectTimeoutError(endpoint_url=URL)], heads),
        )
        assert await backend.exists(key)
        assert len(heads) == 2
        gets: list[int] = []
        monkeypatch.setattr(
            client, "get_object", _failing_first(client.get_object, [_refused()], gets)
        )
        assert b"".join([c async for c in backend.download(key)]) == b"kept"
        assert len(gets) == 2
    finally:
        await backend.aclose()


async def test_a_connect_timeout_on_a_put_is_tried_again(
    s3: S3Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#8471: no byte of the first PUT was sent; the key is the bytes' own, so the
    second PUT writes the object once, from the start of the file."""
    backend = S3ContentBackend(s3, connect_backoff=(0, 0, 0))
    try:
        client = await backend._s3()
        puts: list[int] = []
        monkeypatch.setattr(
            client,
            "put_object",
            _failing_first(
                client.put_object,
                [ConnectTimeoutError(endpoint_url=URL), ConnectTimeoutError(endpoint_url=URL)],
                puts,
            ),
        )
        f = tmp_path / "piece"
        f.write_bytes(b"piece bytes")
        with f.open("rb") as fh:
            key = await backend.upload("piece", fh, name="p.zip", scope=SCOPE)
        assert len(puts) == 3
        assert b"".join([c async for c in backend.download(key)]) == b"piece bytes"
    finally:
        await backend.aclose()


async def test_a_connect_failure_past_the_backoff_propagates(
    s3: S3Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each wait of the backoff, then one last try; its error is the caller's."""
    backend = S3ContentBackend(s3)
    waits: list[float] = []

    real_sleep = asyncio.sleep

    async def sleep(wait: float) -> None:
        waits.append(wait)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    try:
        client = await backend._s3()
        heads: list[int] = []
        errors: list[Exception] = [ConnectTimeoutError(endpoint_url=URL) for _ in range(9)]
        monkeypatch.setattr(
            client, "head_object", _failing_first(client.head_object, errors, heads)
        )
        with pytest.raises(ConnectTimeoutError):
            await backend.exists(MISSING)
        assert waits == list(CONNECT_BACKOFF) == [0.5, 1.0, 2.0]
        assert len(heads) == 4
    finally:
        await backend.aclose()


@pytest.mark.parametrize(
    "error",
    [
        ReadTimeoutError(endpoint_url=URL),
        ConnectionClosedError(endpoint_url=URL),
        EndpointConnectionError(endpoint_url=URL, error=ClientOSError(104, "reset")),
    ],
    ids=["read-timeout", "closed", "reset-mid-body"],
)
async def test_an_error_after_the_request_was_sent_is_not_tried_again(
    s3: S3Settings, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """#8471: the store may have taken the request, so the error is the caller's."""
    backend = S3ContentBackend(s3, connect_backoff=(0, 0, 0))
    try:
        client = await backend._s3()
        puts: list[int] = []
        monkeypatch.setattr(client, "put_object", _failing_first(client.put_object, [error], puts))
        with pytest.raises(type(error)):
            await backend.upload("piece", b"once", name="p", scope=SCOPE)
        assert len(puts) == 1
    finally:
        await backend.aclose()
