from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import EndpointConnectionError

from scadbuddy.library.settings_store import S3Settings
from scadbuddy.store.content_models import BlobMissingError, BlobScope
from scadbuddy.store.s3 import S3ContentBackend, object_key

SCOPE = BlobScope(slug="demo", title="Demo")
MISSING = "t/asset/" + "0" * 64


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
        swept = await backend.upload("piece", b"orphan", name="b", scope=SCOPE)
        cutoff = datetime.now(UTC)
        # S3's LastModified is whole seconds: the next upload must land in a later one.
        await asyncio.sleep(1.1)
        young = await backend.upload("piece", b"young", name="c", scope=SCOPE)
        seen: list[str] = []

        def recorded(key: str) -> bool:
            seen.append(key)
            return key == kept

        assert await backend.sweep_unrecorded(cutoff=cutoff, recorded=recorded) == 1
        assert await backend.exists(kept)
        assert not await backend.exists(swept)
        assert await backend.exists(young)
        assert young not in seen
    finally:
        await backend.aclose()
