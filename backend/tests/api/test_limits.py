from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from scadbuddy.api import crud_agent_events, crud_agent_transcripts, crud_agent_turns
from scadbuddy.api.limits import (
    BODY_LIMITS,
    INVOICE_FILE_UPLOAD_PATH,
    MAX_MULTIPART_BODY_BYTES,
    MAX_TEXT_BODY_BYTES,
    ORDER_ATTACHMENT_UPLOAD_PATH,
    BodySizeGate,
    ClientGoneError,
    RouteLimit,
    unless_the_client_leaves,
)
from tests.support.operations import press


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
        headers={**press(), "Content-Type": "text/plain", "X-Model-Name": "Huge"},
    )
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert "reads at most" in response.json()["detail"]


def test_a_chunked_body_is_cut_off_at_the_limit(client: TestClient) -> None:
    """No Content-Length, so nothing to judge on the headers: the gate counts instead,
    and stops reading at the chunk that crosses the limit."""

    def chunks() -> object:
        for _ in range(MAX_TEXT_BODY_BYTES // (1024 * 1024) + 4):
            yield b"x" * (1024 * 1024)

    response = client.post(
        "/api/v1/models",
        content=chunks(),
        headers={**press(), "Content-Type": "text/plain", "X-Model-Name": "Huge"},
    )
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert "most this API reads" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/api/v1/models/check", "/api/v1/lsp/diagnostics"])
def test_a_chunked_json_body_is_a_413_on_a_pydantic_route(client: TestClient, path: str) -> None:
    """#1315: FastAPI reads a pydantic body itself and turns any error raised while
    reading into a 400 "error parsing the body", so the gate's own exception never
    reached it. The refusal is the same 413 a declared length gets."""

    def chunks() -> object:
        yield b'{"source": "'
        for _ in range(MAX_TEXT_BODY_BYTES // (1024 * 1024) + 4):
            yield b"a" * (1024 * 1024)
        yield b'"}'

    response = client.post(path, content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert "most this API reads" in response.json()["detail"]


def test_an_oversized_upload_is_refused_like_a_paste(client: TestClient) -> None:
    """Starlette spools a file part to disk with no total, and the route then reads it
    whole, so multipart is gated too -- with the same 413 problem the pastes get."""
    response = client.post(
        "/api/v1/models",
        files={"file": ("huge.scad", b"x" * (MAX_MULTIPART_BODY_BYTES + 1), "text/plain")},
        headers=press(),
    )
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["title"] == "Content Too Large"
    assert "reads at most" in response.json()["detail"]


def test_a_small_chunked_body_passes_the_gate(client: TestClient) -> None:
    def chunks() -> object:
        yield b'{"source": '
        yield b'"cube(1);\\n"}'

    response = client.post(
        "/api/v1/models/check", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200


def test_an_ordinary_body_passes_the_gate(client: TestClient) -> None:
    response = client.post("/api/v1/models/check", json={"source": "cube(1);\n"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/orders/o1/attachments",
        "/api/v1/orders/o1/items/i1/attachments",
        "/api/v1/orders/o1/items/i1/fulfilments/f1/attachments",
        "/api/v1/orders/o1/items/i1/revisions/r1/attachments",
    ],
)
def test_every_order_upload_route_takes_the_media_limit(path: str) -> None:
    """Each of `api/order_attachments.py`'s upload routes, the attempts' among them, is
    as large as a template's media, never the multipart cap."""
    assert ORDER_ATTACHMENT_UPLOAD_PATH.match(path)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/orders/o1/attachments/a1/to-model",
        "/api/v1/orders/o1/items/i1/fulfilments/attachments",
        "/api/v1/orders/o1/items/i1/revisions/r1/fulfilments/f1/attachments",
    ],
)
def test_no_other_order_route_takes_the_media_limit(path: str) -> None:
    assert not ORDER_ATTACHMENT_UPLOAD_PATH.match(path)


def test_an_invoice_file_upload_takes_the_media_limit() -> None:
    assert INVOICE_FILE_UPLOAD_PATH.match("/api/v1/filament/invoices/abc/file")
    assert not INVOICE_FILE_UPLOAD_PATH.match("/api/v1/filament/invoices/abc/lines")


def test_a_json_body_with_no_content_type_is_capped_too(client: TestClient) -> None:
    """#6861: FastAPI reads a body with no Content-Type as JSON, so a caller that left
    the header out must not get past the cap the header would have met."""
    body = b'{"source": "' + b"a" * MAX_TEXT_BODY_BYTES + b'"}'
    response = client.post("/api/v1/models/check", content=body)
    assert "content-type" not in response.request.headers
    assert response.status_code == 413
    assert "reads at most" in response.json()["detail"]


def test_a_chunked_body_with_no_content_type_is_cut_off_too(client: TestClient) -> None:
    def chunks() -> object:
        yield b'{"source": "'
        for _ in range(MAX_TEXT_BODY_BYTES // (1024 * 1024) + 4):
            yield b"a" * (1024 * 1024)
        yield b'"}'

    response = client.post("/api/v1/models/check", content=chunks())
    assert "content-type" not in response.request.headers
    assert response.status_code == 413
    assert "most this API reads" in response.json()["detail"]


def _gated() -> TestClient:
    async def read(request: Request) -> PlainTextResponse:
        return PlainTextResponse(str(len(await request.body())))

    app = Starlette(
        routes=[Route("/plain", read, methods=["PUT"]), Route("/big", read, methods=["PUT"])]
    )
    app.add_middleware(
        BodySizeGate,
        limits=BODY_LIMITS,
        routes=[
            RouteLimit(
                "PUT", re.compile(r"^/big$"), 3 * MAX_TEXT_BODY_BYTES, "a big upload", "fixed"
            )
        ],
    )
    return TestClient(app)


@pytest.mark.parametrize("kind", ["application/octet-stream", "model/3mf", None])
def test_an_unknown_type_is_capped_unless_the_route_has_its_own_limit(kind: str | None) -> None:
    """#6861: a route that reads a larger non-JSON body names its `RouteLimit`; the rest
    get `MAX_TEXT_BODY_BYTES` however the body is typed."""
    headers = {} if kind is None else {"Content-Type": kind}
    size = MAX_TEXT_BODY_BYTES + 1
    gated = _gated()
    refused = gated.put("/plain", content=b"x" * size, headers=headers)
    assert refused.status_code == 413
    kept = gated.put("/big", content=b"x" * size, headers=headers)
    assert (kept.status_code, kept.text) == (200, str(size))
    assert gated.put("/plain", content=b"x" * 1024, headers=headers).text == "1024"


_AGENT_STORE_MODULES = [crud_agent_transcripts, crud_agent_turns, crud_agent_events]


def _bounded_paths(module: Any) -> list[str]:
    paths = [
        f"/api/v1{route.path}".replace("{session_id}", "s")
        for route in module.router.routes
        if isinstance(route, APIRoute)
        and any(dep.call is module._bounded for dep in route.dependant.dependencies)
    ]
    assert paths
    return paths


@pytest.mark.parametrize("module", _AGENT_STORE_MODULES, ids=lambda m: m.__name__)
def test_every_bounded_agent_store_route_has_its_declared_limit(module: Any) -> None:
    """#6863: a route's declared `BODY_BYTES_MAX` above the gate's 8 MiB default is only
    reachable through a `RouteLimit`, and the limit covers every route that declares it."""
    assert module.BODY_BYTES_MAX > MAX_TEXT_BODY_BYTES
    assert module.ROUTE_LIMIT.limit == module.BODY_BYTES_MAX
    for path in _bounded_paths(module):
        assert module.ROUTE_LIMIT.matches({"method": "POST", "path": path}), path


def test_main_installs_the_agent_store_route_limits(client: TestClient) -> None:
    gate = next(m for m in client.app.user_middleware if m.cls is BodySizeGate)  # type: ignore[attr-defined]
    installed = gate.kwargs["routes"]
    for module in _AGENT_STORE_MODULES:
        assert module.ROUTE_LIMIT in installed


@pytest.mark.parametrize("module", _AGENT_STORE_MODULES, ids=lambda m: m.__name__)
def test_an_agent_store_body_over_8_mib_reaches_the_route_until_its_bound(module: Any) -> None:
    async def read(request: Request) -> PlainTextResponse:
        return PlainTextResponse(str(len(await request.body())))

    path = _bounded_paths(module)[0]
    app = Starlette(routes=[Route(path, read, methods=["POST"])])
    app.add_middleware(BodySizeGate, limits=BODY_LIMITS, routes=[module.ROUTE_LIMIT])
    gated = TestClient(app)
    headers = {"Content-Type": "application/json"}
    size = MAX_TEXT_BODY_BYTES + 1
    kept = gated.post(path, content=b"x" * size, headers=headers)
    assert (kept.status_code, kept.text) == (200, str(size))
    refused = gated.post(path, content=b"x" * (module.BODY_BYTES_MAX + 1), headers=headers)
    assert refused.status_code == 413


def test_the_fallback_limit_is_a_keyword_argument() -> None:
    async def read(request: Request) -> PlainTextResponse:
        return PlainTextResponse(str(len(await request.body())))

    app = Starlette(routes=[Route("/x", read, methods=["POST"])])
    app.add_middleware(BodySizeGate, limits={}, default=10)
    gated = TestClient(app)
    assert gated.post("/x", content=b"x" * 10).status_code == 200
    assert gated.post("/x", content=b"x" * 11).status_code == 413
