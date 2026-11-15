"""A design request's body, read with caps (#5282, plan Task 9): the bounds on time, on a
declared length and on what one process holds, which no test client can drive."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
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


def test_eight_uploads_at_once_per_process() -> None:
    assert uploads.MAX_UPLOADS_AT_ONCE == 8


async def test_a_sender_under_the_floor_is_too_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each byte comes well inside the gap, but too few come in each window.
    monkeypatch.setattr(uploads, "RATE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(uploads, "RATE_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(uploads, "MIN_WINDOW_BYTES", 1000)
    trickle = [HEAD, FILE] + [b"x"] * 100 + [TAIL]
    error = await refused(request(chunks(*trickle, pause=0.01)), UploadGate())
    assert (error.status, slug(error)) == (408, "upload_too_slow")


async def test_a_quiet_window_is_too_slow_at_its_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # Enough at first, then nothing: refused as the window ends, before the gap does.
    monkeypatch.setattr(uploads, "RATE_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(uploads, "RATE_WINDOW_SECONDS", 0.1)
    monkeypatch.setattr(uploads, "MIN_WINDOW_BYTES", 10)
    sent = [HEAD + FILE + b"x" * 100]

    async def then_quiet() -> dict[str, Any]:
        if sent:
            return {"type": "http.request", "body": sent.pop(), "more_body": True}
        await asyncio.sleep(10)
        raise AssertionError("not refused")

    started = asyncio.get_running_loop().time()
    error = await refused(request(then_quiet), UploadGate())
    assert (error.status, slug(error)) == (408, "upload_too_slow")
    assert asyncio.get_running_loop().time() - started < 1.0


def clocked(
    monkeypatch: pytest.MonkeyPatch, *arrivals: tuple[float, bytes]
) -> Callable[[], Awaitable[dict[str, Any]]]:
    """Each chunk read at its arrival time on `uploads`' clock, at once in real time; a
    chunk that would come after the read's time limit times the read out instead."""
    now = [0.0]
    limit = [float("inf")]
    monkeypatch.setattr(uploads, "_now", lambda: now[0])

    @contextmanager
    def fail_after(delay: float) -> Iterator[None]:
        limit[0] = now[0] + delay
        yield

    monkeypatch.setattr(uploads, "_fail_after", fail_after)
    sent = list(arrivals)

    async def receive() -> dict[str, Any]:
        at, body = sent.pop(0)
        if at > limit[0]:
            now[0] = limit[0]
            raise TimeoutError
        now[0] = at
        return {"type": "http.request", "body": body, "more_body": bool(sent)}

    return receive


KIB64 = b"x" * (64 * 1024)


async def test_a_steady_sender_just_over_the_floor_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The re-review's sender (about 1.5x the floor): windows [10, 20), [20, 30), [30, 40)
    # each get one 64 KiB chunk, at 19, 25.67 and 32.3.
    receive = clocked(
        monkeypatch,
        (0.0, HEAD + FILE),
        (19.0, KIB64),
        (25.67, KIB64),
        (32.3, KIB64),
        (39.0, KIB64),
        (40.0, TAIL),
    )
    upload = await read_request(request(receive), UploadGate())
    try:
        assert len(await upload.files[0].read()) == 4 * len(KIB64)
    finally:
        await upload.close()


async def test_a_one_byte_a_second_trickle_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    trickle = [(float(t), b"x") for t in range(1, 60)]
    receive = clocked(monkeypatch, (0.0, HEAD + FILE), *trickle, (60.0, TAIL))
    error = await refused(request(receive), UploadGate())
    assert (error.status, slug(error)) == (408, "upload_too_slow")


async def test_empty_windows_fail_at_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # [10, 20) is full; [20, 30) gets nothing, though a chunk comes at 35.
    receive = clocked(monkeypatch, (0.0, HEAD + FILE), (15.0, KIB64), (35.0, KIB64), (36.0, TAIL))
    error = await refused(request(receive), UploadGate())
    assert (error.status, slug(error)) == (408, "upload_too_slow")


async def test_a_sender_over_the_floor_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uploads, "RATE_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(uploads, "RATE_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(uploads, "MIN_WINDOW_BYTES", 1)
    trickle = [HEAD, FILE] + [b"x" * 10] * 20 + [TAIL]
    upload = await read_request(request(chunks(*trickle, pause=0.01)), UploadGate())
    try:
        assert upload.text == "A hook" and len(upload.files) == 1
    finally:
        await upload.close()


async def test_releasing_frees_the_slot_and_keeps_the_files() -> None:
    gate = UploadGate()
    upload = await read_request(request(chunks(HEAD + FILE + b"x" * 100 + TAIL)), gate)
    upload.release()
    assert (gate.running, gate.spooled) == (0, 0)
    assert await upload.files[0].read() == b"x" * 100
    await upload.close()
    assert (gate.running, gate.spooled) == (0, 0)


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


def test_an_escaped_file_name_decodes_in_either_case() -> None:
    # Review of #5829: browsers send upper case, but a lower-case escape decodes the same.
    from scadbuddy.storefront.wire import unescape_filename

    assert unescape_filename("a%22b.jpg") == 'a"b.jpg'
    assert unescape_filename("a%5cb%0a.jpg") == "a\\b\n.jpg"
    assert unescape_filename("50%25.jpg") == "50%25.jpg"
