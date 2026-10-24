"""`RemoteOutputs` against answers the API's internal routes do not give (#1060): an API
that is restarting, the SPA's fallback page, a 404 that is not the API's problem."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from scadbuddy.bambuddy.output_reader import BlobMissingAnswerError, RemoteBlobs, RemoteOutputs
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, ApiError
from scadbuddy.core.secrets import kek_from_base64
from scadbuddy.library.outputs import OutputNotFoundError
from scadbuddy.slicing.transfer import INTERNAL_BLOBS, TRANSFER_HEADER
from scadbuddy.slicing.transfer_token import transfer_key, verify

OUTPUT = "a" * 32
KEK = kek_from_base64(base64.b64encode(b"\x21" * 32).decode())
INDEX_HTML = b"<!doctype html><html><body>ScadBuddy</body></html>"


def reader(answer: Callable[[httpx.Request], httpx.Response]) -> RemoteOutputs:
    async def no_sleep(_: float) -> None:
        return None

    http = httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(answer))
    return RemoteOutputs("http://api", client=http, sleep=no_sleep)


def test_a_restarting_api_is_retried_until_it_answers() -> None:
    calls = 0

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 4:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, content=b"3mf", headers={"content-type": "model/3mf"})

    assert asyncio.run(reader(answer).model_3mf(OUTPUT)) == b"3mf"
    assert calls == 5


def test_an_api_that_stays_down_raises_after_the_budget() -> None:
    calls = 0

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(httpx.ConnectError):
        asyncio.run(reader(answer).model_3mf(OUTPUT))
    assert calls > 1


def test_a_404_that_is_not_a_problem_is_an_error_not_a_gone_output() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"Not Found", headers={"content-type": "text/plain"})

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(reader(answer).get(OUTPUT))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(reader(answer).model_3mf(OUTPUT))


def test_a_problem_404_is_a_gone_output() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"title": "Not Found", "status": 404, "detail": "gone"},
            headers={"content-type": PROBLEM_MEDIA_TYPE},
        )

    with pytest.raises(OutputNotFoundError):
        asyncio.run(reader(answer).get(OUTPUT))
    assert asyncio.run(reader(answer).model_3mf(OUTPUT)) is None


def test_the_spa_fallback_page_is_an_error_not_data() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=INDEX_HTML, headers={"content-type": "text/html"})

    with pytest.raises(httpx.HTTPError):
        asyncio.run(reader(answer).get(OUTPUT))
    with pytest.raises(httpx.HTTPError):
        asyncio.run(reader(answer).model_3mf(OUTPUT))


# --- RemoteBlobs: a LAN print's sliced blob through the internal route (plan 3a Task 9) --


def _blobs(answer: Callable[[httpx.Request], httpx.Response]) -> RemoteBlobs:
    http = httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(answer))
    return RemoteBlobs("http://api", KEK, client=http)


async def _read(blobs: RemoteBlobs, key: str) -> bytes:
    return b"".join([chunk async for chunk in blobs.stream(key)])


def test_a_blob_is_read_with_a_token_for_its_one_key() -> None:
    key = "slice-" + "b" * 64
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"sliced")

    assert asyncio.run(_read(_blobs(answer), key)) == b"sliced"
    [request] = seen
    assert request.url.path == f"{INTERNAL_BLOBS}/{key}"
    token = verify(
        transfer_key(KEK),
        request.headers[TRANSFER_HEADER],
        method="GET",
        blob_key=key,
        now=datetime.now(UTC),
    )
    assert token.blob_key == key


def test_the_apis_404_is_a_missing_blob() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"title": "Not Found", "status": 404, "detail": "gone"},
            headers={"content-type": PROBLEM_MEDIA_TYPE},
        )

    with pytest.raises(BlobMissingAnswerError):
        asyncio.run(_read(_blobs(answer), "slice-x"))


def test_a_refused_token_is_the_apis_problem() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"title": "Forbidden", "status": 403, "detail": "transfer refused"},
            headers={"content-type": PROBLEM_MEDIA_TYPE},
        )

    with pytest.raises(ApiError) as raised:
        asyncio.run(_read(_blobs(answer), "slice-x"))
    assert raised.value.status == 403
