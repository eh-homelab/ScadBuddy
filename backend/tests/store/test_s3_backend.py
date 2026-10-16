from __future__ import annotations

import asyncio
import hashlib
import io
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import EndpointConnectionError

from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.content import ContentBackend
from scadbuddy.store.content_models import BlobMissingError, BlobScope
from scadbuddy.store.s3 import S3ContentBackend, object_key

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
        )
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

        def recorded(key: str) -> bool:
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
