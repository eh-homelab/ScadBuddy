from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.camera.follow import ChangeFollower
from scadbuddy.crud.client import CrudClient

PATH = "/api/v1/internal/crud/camera/changes"


class Token:
    def token(self) -> str | None:
        return "t"

    def invalidate(self, token: str) -> None:
        pass


def frame(event: str, data: dict, id: str | None = None) -> bytes:
    head = f"id: {id}\n" if id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def change(kind: str, printer_id: str, id: str) -> bytes:
    payload = json.dumps({"kind": kind, "printer_id": printer_id})
    return frame("change", {"c": "scadbuddy_events", "p": payload}, id)


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], hold: asyncio.Event | None = None) -> None:
        self.chunks, self.hold = chunks, hold

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.hold is not None:
            await self.hold.wait()


def sse(chunks: list[bytes], hold: asyncio.Event | None = None) -> httpx.Response:
    headers = {"X-ScadBuddy-Crud": "1", "Content-Type": "text/event-stream"}
    return httpx.Response(200, headers=headers, stream=Body(chunks, hold))


def follower(handler, changes: list[str], resyncs: list[int], **options) -> ChangeFollower:
    client = CrudClient(
        "http://api", "camera", ServiceAuth(Token()), transport=httpx.MockTransport(handler)
    )

    async def on_change(printer_id: str) -> None:
        changes.append(printer_id)

    async def on_resync() -> None:
        resyncs.append(1)

    return ChangeFollower(
        client, PATH, on_change, on_resync, retry_min=0.01, retry_max=0.02, **options
    )


async def until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_follower_rereads_on_resync() -> None:
    hold = asyncio.Event()
    body = [
        frame("ready", {"resync": True, "reconnect": False}, "5"),
        change("printer.changed", "p1", "6"),
        change("printer.report", "p2", "7"),
    ]
    changes: list[str] = []
    resyncs: list[int] = []
    task = asyncio.create_task(follower(lambda request: sse(body, hold), changes, resyncs).run())
    await until(lambda: changes)
    await asyncio.sleep(0.05)
    task.cancel()
    assert resyncs == [1]
    assert changes == ["p1"]


async def test_follower_calls_resync_on_reconnected_and_camera_reconnect() -> None:
    hold = asyncio.Event()
    body = [
        frame("ready", {"resync": False, "reconnect": False}, "5"),
        frame("reconnected", {"resync": False}, "9"),
        change("camera.reconnect", "p3", "10"),
    ]
    changes: list[str] = []
    resyncs: list[int] = []
    task = asyncio.create_task(follower(lambda request: sse(body, hold), changes, resyncs).run())
    await until(lambda: changes)
    task.cancel()
    assert resyncs == [1]
    assert changes == ["p3"]


async def test_follower_reconnects_with_last_event_id() -> None:
    hold = asyncio.Event()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return sse(
                [
                    frame("ready", {"resync": False, "reconnect": False}, "5"),
                    change("printer.changed", "p1", "6"),
                ]
            )
        return sse([frame("ready", {"resync": False, "reconnect": True}, "6")], hold)

    changes: list[str] = []
    resyncs: list[int] = []
    task = asyncio.create_task(follower(handler, changes, resyncs).run())
    await until(lambda: len(requests) >= 2 and resyncs)
    task.cancel()
    assert "last-event-id" not in requests[0].headers
    assert requests[1].headers["last-event-id"] == "6"
    assert requests[1].headers["authorization"] == "Bearer t"
    assert changes == ["p1"]


async def test_follower_treats_silence_as_dead() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return sse([frame("ready", {"resync": False, "reconnect": False}, "5")], asyncio.Event())

    task = asyncio.create_task(follower(handler, [], [], idle=0.05).run())
    await until(lambda: len(requests) >= 2)
    task.cancel()
    assert requests[1].headers["last-event-id"] == "5"
