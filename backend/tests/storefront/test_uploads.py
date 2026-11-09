"""A design request's body, read with caps (#5282, plan Task 9): the bounds on time, on a
declared length and on what one process holds, which no test client can drive."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from starlette.requests import Request

from scadbuddy.core.problems import ApiError
from scadbuddy.storefront import uploads
from scadbuddy.storefront.uploads import UploadGate, read_request
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS

BOUNDARY = "b"
HEAD = b'--b\r\nContent-Disposition: form-data; name="text"\r\n\r\nA hook\r\n'
FILE = b'--b\r\nContent-Disposition: form-data; name="files"; filename="a.png"\r\n\r\n'
TAIL = b"\r\n--b--\r\n"


def request(
    receive: Callable[[], Awaitable[dict[str, Any]]], *, length: int | None = None
) -> Request:
    headers = [(b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    return Request(scope, receive)


def chunks(*parts: bytes, pause: float = 0.0) -> Callable[[], Awaitable[dict[str, Any]]]:
    sent = list(parts)

    async def receive() -> dict[str, Any]:
        if pause:
            await asyncio.sleep(pause)
        body = sent.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(sent)}

    return receive


def slug(error: ApiError) -> str:
    return str(error.type).removeprefix(STOREFRONT_PROBLEMS)


async def refused(req: Request, gate: UploadGate) -> ApiError:
    with pytest.raises(ApiError) as caught:
        await read_request(req, gate)
    assert (gate.running, gate.spooled) == (0, 0)
    return caught.value


async def test_a_declared_length_past_the_cap_is_refused_before_any_byte() -> None:
    async def never() -> dict[str, Any]:
        raise AssertionError("the body was read")

    error = await refused(request(never, length=uploads.MAX_REQUEST_BODY + 1), UploadGate())
    assert (error.status, slug(error)) == (413, "file_too_large")


async def test_a_gap_past_the_cap_is_too_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uploads, "MAX_GAP_SECONDS", 0.05)
    error = await refused(request(chunks(HEAD, FILE, b"x", TAIL, pause=0.2)), UploadGate())
    assert (error.status, slug(error)) == (408, "upload_too_slow")


async def test_a_body_past_its_deadline_is_too_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uploads, "MAX_BODY_SECONDS", 0.2)
    trickle = [HEAD, FILE] + [b"x"] * 50 + [TAIL]
    error = await refused(request(chunks(*trickle, pause=0.02)), UploadGate())
    assert (error.status, slug(error)) == (408, "upload_too_slow")


async def test_a_full_gate_is_busy_before_any_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uploads, "MAX_UPLOADS_AT_ONCE", 1)
    gate = UploadGate()
    held = await read_request(request(chunks(HEAD + TAIL)), gate)
    try:

        async def never() -> dict[str, Any]:
            raise AssertionError("the body was read")

        with pytest.raises(ApiError) as caught:
            await read_request(request(never), gate)
        assert (caught.value.status, slug(caught.value)) == (503, "uploads_busy")
    finally:
        await held.close()
    assert (gate.running, gate.spooled) == (0, 0)


async def test_the_spool_budget_counts_every_upload_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = HEAD + FILE + b"x" * 1000 + TAIL
    monkeypatch.setattr(uploads, "MAX_SPOOL_BYTES", len(body) + 100)
    gate = UploadGate()
    held = await read_request(request(chunks(body)), gate)
    try:
        error = await refused_while(gate, request(chunks(body)))
        assert (error.status, slug(error)) == (503, "uploads_busy")
    finally:
        await held.close()
    assert (gate.running, gate.spooled) == (0, 0)


async def refused_while(gate: UploadGate, req: Request) -> ApiError:
    with pytest.raises(ApiError) as caught:
        await read_request(req, gate)
    return caught.value


async def test_the_text_is_checked_as_its_part_ends() -> None:
    nul = b'--b\r\nContent-Disposition: form-data; name="text"\r\n\r\na\x00b\r\n'

    async def receive_once() -> dict[str, Any]:
        return {"type": "http.request", "body": nul + FILE + b"x", "more_body": True}

    error = await refused(request(receive_once), UploadGate())
    assert (error.status, slug(error)) == (422, "request_text")
