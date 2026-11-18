"""#6264 Task 8: the SpoolBuddy tag scan on Bambuddy's WebSocket (`next_tag_scan`) and the
token it joins with (`ws_token`). The socket is a local `websockets` server that plays
Bambuddy's frames (`ws_events` in ``recordings/openapi/rfid-routes.json``); the HTTP calls
are respx. The live socket is never opened."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import SCOPE_PROBLEM
from scadbuddy.bambuddy.models import TagScan
from scadbuddy.core.problems import ApiError

Frame = dict[str, Any]

PRINTER_STATUS: Frame = {"type": "printer_status", "printer_id": 1, "data": {}}
UNKNOWN: Frame = {
    "type": "spoolbuddy_unknown_tag",
    "device_id": "sb-1",
    "tag_uid": "04AABBCC",
    "tray_uuid": None,
    "sak": 0,
    "tag_type": "NTAG215",
    "tag_data": None,
}


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mocked:
        yield mocked


def _auth(router: respx.MockRouter, enabled: bool) -> None:
    router.get(url__regex=r"http://127\.0\.0\.1:\d+/api/v1/auth/status").mock(
        return_value=httpx.Response(200, json={"auth_enabled": enabled})
    )


@asynccontextmanager
async def _fake(
    frames: list[Frame], *, token: str | None = None, seen: list[str] | None = None
) -> AsyncIterator[BambuddyClient]:
    """A Bambuddy whose ``/api/v1/ws`` sends ``frames`` and then waits. With ``token``
    set it refuses, as Bambuddy does with auth on, a join without ``?token=<token>``."""

    async def handler(connection: ServerConnection) -> None:
        assert connection.request is not None
        if seen is not None:
            seen.append(connection.request.path)
        for frame in frames:
            await connection.send(json.dumps(frame))
        await connection.wait_closed()

    def refuse(connection: ServerConnection, request: Request) -> Response | None:
        if token is not None and not request.path.endswith(f"?token={token}"):
            return connection.respond(403, "refused\n")
        return None

    async with serve(handler, "127.0.0.1", 0, process_request=refuse) as server:
        port = server.sockets[0].getsockname()[1]
        config = BambuddyConfig(base_url=f"http://127.0.0.1:{port}", api_key="s3cret")
        async with BambuddyClient(config) as client:
            yield client


async def test_the_scan_ignores_other_frames_and_returns_the_tag(router: respx.MockRouter) -> None:
    _auth(router, False)
    async with _fake([PRINTER_STATUS, UNKNOWN]) as client:
        scan = await client.next_tag_scan(timeout=5)

    assert scan == TagScan(
        device_id="sb-1",
        tag_uid="04AABBCC",
        tray_uuid=None,
        tag_type="NTAG215",
        bb_spool_id=None,
    )


async def test_a_matched_frame_carries_the_bambuddy_spool(router: respx.MockRouter) -> None:
    _auth(router, False)
    matched: Frame = {
        "type": "spoolbuddy_tag_matched",
        "device_id": "sb-1",
        "tag_uid": "04AABBCC",
        "tray_uuid": None,
        "spool": {"id": 11, "material": "PLA"},
    }
    async with _fake([matched]) as client:
        scan = await client.next_tag_scan(timeout=5)

    assert scan is not None
    assert (scan.bb_spool_id, scan.tag_uid, scan.device_id) == (11, "04AABBCC", "sb-1")


async def test_a_scan_with_no_frame_times_out(router: respx.MockRouter) -> None:
    _auth(router, False)
    async with _fake([PRINTER_STATUS]) as client:
        assert await client.next_tag_scan(timeout=0.2) is None


async def test_a_scan_of_another_device_is_skipped(router: respx.MockRouter) -> None:
    _auth(router, False)
    other: Frame = {**UNKNOWN, "device_id": "sb-2", "tag_uid": "04000000"}
    async with _fake([other, UNKNOWN]) as client:
        scan = await client.next_tag_scan(timeout=5, device_id="sb-1")
        assert scan is not None
        assert (scan.device_id, scan.tag_uid) == ("sb-1", "04AABBCC")

    async with _fake([other]) as client:
        assert await client.next_tag_scan(timeout=0.2, device_id="sb-1") is None


async def test_with_auth_on_the_socket_joins_with_a_minted_token(router: respx.MockRouter) -> None:
    _auth(router, True)
    minted = router.post(url__regex=r"http://127\.0\.0\.1:\d+/api/v1/auth/ws-token").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    seen: list[str] = []
    async with _fake([UNKNOWN], token="abc123", seen=seen) as client:
        scan = await client.next_tag_scan(timeout=5)

    assert scan is not None
    assert minted.call_count == 1
    assert minted.calls.last.request.headers["X-API-Key"] == "s3cret"
    assert seen == ["/api/v1/ws?token=abc123"]


async def test_with_auth_off_the_socket_joins_with_no_token(router: respx.MockRouter) -> None:
    _auth(router, False)
    minted = router.post(url__regex=r"http://127\.0\.0\.1:\d+/api/v1/auth/ws-token")
    seen: list[str] = []
    async with _fake([UNKNOWN], seen=seen) as client:
        await client.next_tag_scan(timeout=5)

    assert not minted.called
    assert seen == ["/api/v1/ws"]


async def test_a_refused_join_is_the_scope_409(router: respx.MockRouter) -> None:
    _auth(router, False)
    # The fake wants a token that the client, seeing auth off, does not send.
    async with _fake([UNKNOWN], token="needed") as client:
        with pytest.raises(ApiError) as caught:
            await client.next_tag_scan(timeout=5)

    assert (caught.value.status, caught.value.type) == (409, SCOPE_PROBLEM)
    assert caught.value.extensions["required_scope"] == "Read Status"


@respx.mock
async def test_ws_token_is_none_when_auth_is_off(bambuddy: BambuddyClient) -> None:
    respx.get("https://bambuddy.test/api/v1/auth/status").mock(
        return_value=httpx.Response(200, json={"auth_enabled": False})
    )

    assert await bambuddy.ws_token() is None


@respx.mock
async def test_ws_token_posts_when_auth_is_on(bambuddy: BambuddyClient) -> None:
    respx.get("https://bambuddy.test/api/v1/auth/status").mock(
        return_value=httpx.Response(200, json={"auth_enabled": True})
    )
    route = respx.post("https://bambuddy.test/api/v1/auth/ws-token").mock(
        return_value=httpx.Response(200, json={"token": "tok"})
    )

    assert await bambuddy.ws_token() == "tok"
    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"


@respx.mock
async def test_ws_token_without_a_token_is_a_502(bambuddy: BambuddyClient) -> None:
    respx.get("https://bambuddy.test/api/v1/auth/status").mock(
        return_value=httpx.Response(200, json={"auth_enabled": True})
    )
    respx.post("https://bambuddy.test/api/v1/auth/ws-token").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.ws_token()

    assert caught.value.status == 502
