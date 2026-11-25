from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx

from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.camera.follow import ChangeFollower
from scadbuddy.crud.client import CrudClient
from tests.camera.support import Token, frame, sse, until

PATH = "/api/v1/internal/crud/camera/changes"


def change(kind: str, printer_id: str, id: str) -> bytes:
    payload = json.dumps({"kind": kind, "printer_id": printer_id})
    return frame("change", {"c": "scadbuddy_events", "p": payload}, id)


def follower(
    handler: Callable[[httpx.Request], httpx.Response],
    changes: list[str],
    resyncs: list[int],
    **options: Any,
) -> ChangeFollower:
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


async def test_follower_rereads_on_resync() -> None:
    hold = asyncio.Event()
    body = [
        frame("ready", {"resync": True, "reconnect": False}, "5"),
        change("printer.changed", "p1", "6"),
        change("printer.report", "p2", "7"),
    ]
    changes: list[str] = []
    resyncs: list[int] = []
    drained = asyncio.Event()
    task = asyncio.create_task(
        follower(lambda request: sse(body, hold, drained), changes, resyncs).run()
    )
    await asyncio.wait_for(drained.wait(), 5)
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


def test_follow_kinds_match_the_api_filter() -> None:
    from scadbuddy.api.crud_camera import CAMERA_KINDS
    from scadbuddy.camera.follow import CHANGE_KINDS

    assert CHANGE_KINDS == CAMERA_KINDS


async def test_follower_retries_a_failed_resync() -> None:
    hold = asyncio.Event()
    requests: list[httpx.Request] = []
    calls: list[int] = []

    drained = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = [
            frame("ready", {"resync": len(requests) == 1, "reconnect": False}, str(len(requests)))
        ]
        if len(requests) > 2:
            return sse(body, hold, drained)
        return sse(body)

    client = CrudClient(
        "http://api", "camera", ServiceAuth(Token()), transport=httpx.MockTransport(handler)
    )

    async def on_change(printer_id: str) -> None:
        pass

    async def on_resync() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("API 500")

    task = asyncio.create_task(
        ChangeFollower(client, PATH, on_change, on_resync, retry_min=0.01, retry_max=0.02).run()
    )
    # The third stream's ready, handled with no third resync.
    await asyncio.wait_for(drained.wait(), 5)
    assert not task.done()
    task.cancel()
    assert len(calls) == 2


async def test_follower_resyncs_after_a_failed_change() -> None:
    hold = asyncio.Event()
    requests: list[httpx.Request] = []
    resyncs: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        ready = frame("ready", {"resync": False, "reconnect": False}, "5")
        if len(requests) == 1:
            return sse([ready, change("printer.changed", "p1", "6")])
        return sse([ready], hold)

    client = CrudClient(
        "http://api", "camera", ServiceAuth(Token()), transport=httpx.MockTransport(handler)
    )

    async def on_change(printer_id: str) -> None:
        raise RuntimeError("API 500")

    async def on_resync() -> None:
        resyncs.append(1)

    task = asyncio.create_task(
        ChangeFollower(client, PATH, on_change, on_resync, retry_min=0.01, retry_max=0.02).run()
    )
    await until(lambda: resyncs)
    task.cancel()
    assert len(requests) >= 2


async def test_follower_resync_first() -> None:
    hold = asyncio.Event()
    body = [frame("ready", {"resync": False, "reconnect": False}, "5")]
    resyncs: list[int] = []
    drained = asyncio.Event()
    task = asyncio.create_task(
        follower(lambda request: sse(body, hold, drained), [], resyncs, resync_first=True).run()
    )
    await asyncio.wait_for(drained.wait(), 5)
    task.cancel()
    assert resyncs == [1]


async def test_follower_without_resync_first_does_not_resync() -> None:
    hold = asyncio.Event()
    requests: list[httpx.Request] = []

    drained = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return sse([frame("ready", {"resync": False, "reconnect": False}, "5")], hold, drained)

    resyncs: list[int] = []
    task = asyncio.create_task(follower(handler, [], resyncs).run())
    await asyncio.wait_for(drained.wait(), 5)
    task.cancel()
    assert resyncs == []
