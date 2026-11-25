from __future__ import annotations

import struct
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from scadbuddy.camera.mp4 import SEGMENT_NAME
from scadbuddy.camera.settings import RecorderSettings
from scadbuddy.camera.uploads import LocalSegment, Uploader, UploadError, object_key
from tests.support.store import moto_s3

KEY_ID = "AKIAKEYIDSECRET0001"
SECRET = "SECRETACCESSKEY0002"
RELAY_PASS = "RELAYPASS0003"
T0 = datetime(2026, 11, 23, 10, 15, 2, 123456, tzinfo=UTC)


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def write_segment(
    root: Path, printer_id: str, start: datetime, *, init: bytes = b"A", media: int = 100
) -> Path:
    """A file as MediaMTX names it, with an init (``ftyp`` + ``moov``) and a media box."""
    folder = root / printer_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{start.strftime(SEGMENT_NAME)}.mp4"
    path.write_bytes(
        box(b"ftyp", b"isom\0\0\0\0")
        + box(b"moov", box(b"trak", init * 16))
        + box(b"mdat", b"\0" * media)
    )
    return path


def settings(**overrides: Any) -> RecorderSettings:
    values: dict[str, Any] = {
        "api_internal_url": "http://api",
        "camera_s3_endpoint": "http://s3",
        "camera_s3_bucket": "cams",
        "camera_s3_prefix": "rec",
        "camera_s3_access_key": SecretStr(KEY_ID),
        "camera_s3_secret_key": SecretStr(SECRET),
        "camera_recorder_name": "rec-a",
        "camera_relay_rtsp_url": "rtsp://relay:8554",
        "camera_relay_read_user": "reader",
        "camera_relay_read_pass": SecretStr(RELAY_PASS),
    }
    return RecorderSettings(**(values | overrides))


def segment(path: Path, printer_id: str = "p1", start: datetime = T0) -> LocalSegment:
    size = path.stat().st_size
    return LocalSegment(
        path=path,
        printer_id=printer_id,
        started_at=start,
        duration_ms=4000,
        bytes=size,
        init_bytes=48,
        init_sha256="0" * 64,
    )


@pytest.fixture
def moto() -> Iterator[RecorderSettings]:
    with moto_s3() as s3:
        yield settings(camera_s3_endpoint=s3.endpoint, camera_s3_bucket=s3.bucket)


def test_object_key_is_the_utc_start() -> None:
    assert object_key("rec", "p1", T0) == "rec/camera/p1/2026/11/23/101502.123456.mp4"
    assert object_key("", "p1", T0) == "camera/p1/2026/11/23/101502.123456.mp4"
    assert object_key("/a/b/", "p1", T0) == "a/b/camera/p1/2026/11/23/101502.123456.mp4"


def test_segment_ends_at(tmp_path: Path) -> None:
    seg = segment(write_segment(tmp_path, "p1", T0))
    assert seg.ends_at == T0 + timedelta(seconds=4)


async def test_put_stores_the_segment(tmp_path: Path, moto: RecorderSettings) -> None:
    path = write_segment(tmp_path, "p1", T0)
    uploader = Uploader(moto)
    try:
        key = await uploader.put(segment(path))
        client = await uploader.client()
        stored = await client.get_object(Bucket=moto.camera_s3_bucket, Key=key)
        body = await stored["Body"].read()
    finally:
        await uploader.aclose()
    assert key == "rec/camera/p1/2026/11/23/101502.123456.mp4"
    assert body == path.read_bytes()
    assert stored["ContentType"] == "video/mp4"


async def test_failed_put_names_no_secret(tmp_path: Path, moto: RecorderSettings) -> None:
    path = write_segment(tmp_path, "p1", T0)
    uploader = Uploader(moto.model_copy(update={"camera_s3_bucket": "missing"}))
    try:
        with pytest.raises(UploadError) as raised:
            await uploader.put(segment(path))
    finally:
        await uploader.aclose()
    message = str(raised.value)
    assert "p1" in message
    assert "NoSuchBucket" in message
    assert raised.value.__suppress_context__
    for secret in (KEY_ID, SECRET):
        assert secret not in message
        assert secret not in repr(uploader)


async def test_failed_put_drops_the_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client error's own text may carry a presigned query or the key id: never kept."""
    path = write_segment(tmp_path, "p1", T0)
    uploader = Uploader(settings())

    class Leaky:
        async def put_object(self, **_: Any) -> None:
            raise RuntimeError(
                f"https://s3/cams/k?X-Amz-Credential={KEY_ID}&X-Amz-Signature=abc {SECRET}"
            )

    async def client() -> Leaky:
        return Leaky()

    monkeypatch.setattr(uploader, "client", client)
    with pytest.raises(UploadError) as raised:
        await uploader.put(segment(path))
    assert str(raised.value).endswith("RuntimeError")
    assert KEY_ID not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
