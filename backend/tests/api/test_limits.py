from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from scadbuddy.api.limits import (
    MAX_TEXT_BODY_BYTES,
    ClientGoneError,
    unless_the_client_leaves,
)


def _request(receive: object) -> Request:
    scope = {"type": "http", "method": "POST", "path": "/api/v1/models/check", "headers": []}
    return Request(scope, receive)  # type: ignore[arg-type]


async def test_a_client_that_leaves_gets_its_permit_back() -> None:
    """The editor aborts a superseded check. Nothing in Starlette cancels the handler,
    so without this the abandoned check still runs — holding the one check permit
    while the check the user is waiting for queues behind it."""
    permit = asyncio.Semaphore(1)
    started = asyncio.Event()

    async def work() -> str:
        async with permit:
            started.set()
            await asyncio.sleep(30)
        return "never"

    async def receive() -> dict[str, str]:
        await started.wait()
        return {"type": "http.disconnect"}

    with pytest.raises(ClientGoneError):
        await unless_the_client_leaves(_request(receive), work())

    assert not permit.locked(), "the abandoned check kept the permit"


async def test_work_that_finishes_first_is_returned_untouched() -> None:
    async def work() -> str:
        return "done"

    async def receive() -> dict[str, str]:
        await asyncio.sleep(30)
        return {"type": "http.disconnect"}

    assert await unless_the_client_leaves(_request(receive), work()) == "done"


def test_a_body_too_large_is_refused_on_its_headers(client: TestClient) -> None:
    """Not after it is buffered: the point of the cap is to not read it at all."""
    response = client.post(
        "/api/v1/models",
        content=b"x" * (MAX_TEXT_BODY_BYTES + 1),
        headers={"Content-Type": "text/plain", "X-Model-Name": "Huge"},
    )
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert "reads at most" in response.json()["detail"]


def test_an_ordinary_body_passes_the_gate(client: TestClient) -> None:
    response = client.post("/api/v1/models/check", json={"source": "cube(1);\n"})
    assert response.status_code == 200
