"""Helpers the camera tests share: a fake MediaMTX control API, a CRUD client's token, the
change stream's frames, segment files as MediaMTX names them, recorder settings, and a
printer that qualifies for recording."""

from __future__ import annotations

import asyncio
import json
import re
import struct
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from scadbuddy.camera.mediamtx import MediaMtx
from scadbuddy.camera.mp4 import SEGMENT_NAME
from scadbuddy.camera.settings import RecorderSettings
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool

KEY_ID = "AKIAKEYIDSECRET0001"
SECRET = "SECRETACCESSKEY0002"
RELAY_PASS = "RELAYPASS0003"
REPORT: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "h2c-report.json").read_text()
)
SERIAL = "01P00A000000001"
CODE = "12345678"
CERT = "ab" * 32
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)


class Token:
    def token(self) -> str | None:
        return "t"

    def invalidate(self, token: str) -> None:
        pass


def frame(event: str, data: dict[str, Any], id: str | None = None) -> bytes:
    head = f"id: {id}\n" if id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Body(httpx.AsyncByteStream):
    """The chunks, then (with ``hold``) an open stream until ``hold`` is set. ``drained`` is
    set when the reader asks for more than the chunks: it has handled every frame in them."""

    def __init__(
        self,
        chunks: list[bytes],
        hold: asyncio.Event | None = None,
        drained: asyncio.Event | None = None,
    ) -> None:
        self.chunks, self.hold, self.drained = chunks, hold, drained

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.drained is not None:
            self.drained.set()
        if self.hold is not None:
            await self.hold.wait()


def sse(
    chunks: list[bytes], hold: asyncio.Event | None = None, drained: asyncio.Event | None = None
) -> httpx.Response:
    headers = {"X-ScadBuddy-Crud": "1", "Content-Type": "text/event-stream"}
    return httpx.Response(200, headers=headers, stream=Body(chunks, hold, drained))


async def until(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


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


class FakeMtx:
    def __init__(self, paths: dict[str, dict[str, Any]] | None = None) -> None:
        self.paths: dict[str, dict[str, Any]] = paths or {}
        self.calls: list[tuple[str, str]] = []
        self.fail = False
        #: The runtime ``ready`` flag per path; false when absent.
        self.ready: dict[str, bool] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        name = path.rsplit("/", 1)[-1]
        if path == "/v3/paths/list":
            # The runtime list's fields, measured on MediaMTX v1.21.4.
            runtime = [
                {"name": n, "confName": n, "ready": self.ready.get(n, False), "online": True}
                for n in self.paths
            ]
            return httpx.Response(200, json={"pageCount": 1, "items": runtime})
        if path.endswith("/list"):
            # As MediaMTX 1.x does (measured 2026-11-24, bluenviron/mediamtx:1).
            items = [
                {
                    "name": n,
                    **c,
                    "source": re.sub(r"//[^@/]*@", "//<redacted>:<redacted>@", c["source"]),
                }
                for n, c in self.paths.items()
            ]
            return httpx.Response(200, json={"pageCount": 1, "items": items})
        verb = path.split("/")[-2]
        self.calls.append((verb, name))
        if self.fail:
            return httpx.Response(400, json={"error": "invalid", "echo": request.content.decode()})
        if verb == "replace" and name not in self.paths:
            return httpx.Response(404)
        if verb in ("replace", "add"):
            self.paths[name] = json.loads(request.content)
        elif verb == "delete":
            if name not in self.paths:
                return httpx.Response(404)
            del self.paths[name]
        return httpx.Response(200)

    def client(self) -> MediaMtx:
        return MediaMtx("http://mtx", transport=httpx.MockTransport(self.handle))


def make_printer(
    printers: PrinterStore,
    pool: PgPool,
    *,
    serial: str = SERIAL,
    code: bool = True,
    report: dict[str, Any] | None = REPORT,
) -> str:
    row = printers.create(PrinterCreate(serial=serial, name="H2C", model="H2C"))
    if code:
        printers.link_lan(row.id, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT)
    if report is not None:
        PrinterReportStore(pool).put(row.id, report, NOW)
    return row.id
