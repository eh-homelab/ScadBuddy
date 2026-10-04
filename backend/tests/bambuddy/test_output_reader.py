"""`RemoteOutputs` against answers the API's internal routes do not give (#1060): an API
that is restarting, the SPA's fallback page, a 404 that is not the API's problem."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest

from scadbuddy.bambuddy.output_reader import RemoteOutputs
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE
from scadbuddy.library.outputs import OutputNotFoundError

OUTPUT = "a" * 32
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
