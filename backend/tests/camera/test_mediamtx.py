from __future__ import annotations

import json
from typing import Any

import httpx

from scadbuddy.camera.mediamtx import MediaMtx


def make(
    paths: dict[str, dict[str, Any]],
) -> tuple[MediaMtx, list[tuple[str, str, dict[str, Any] | None]]]:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        path = request.url.path
        name = path.rsplit("/", 1)[-1]
        if path.endswith("/list"):
            items = [{"name": n, **c} for n, c in paths.items()]
            return httpx.Response(
                200, json={"pageCount": 1, "itemCount": len(items), "items": items}
            )
        if "/replace/" in path:
            if name not in paths:
                return httpx.Response(404, json={"error": "path configuration not found"})
            paths[name] = body or {}
        elif "/add/" in path:
            if name in paths:
                return httpx.Response(400, json={"error": "path already exists"})
            paths[name] = body or {}
        elif "/delete/" in path:
            if name not in paths:
                return httpx.Response(404, json={"error": "path configuration not found"})
            del paths[name]
        return httpx.Response(200)

    return MediaMtx("http://mtx", transport=httpx.MockTransport(handle)), calls


async def test_put_path_adds_then_replaces() -> None:
    stored: dict[str, dict[str, Any]] = {}
    mtx, calls = make(stored)
    await mtx.put_path("p1", {"source": "rtsps://a"})
    await mtx.put_path("p1", {"source": "rtsps://b"})
    assert stored == {"p1": {"source": "rtsps://b"}}
    assert [c[1].split("/")[-2] for c in calls] == ["replace", "add", "replace"]


async def test_paths_lists_by_name() -> None:
    mtx, _ = make({"p1": {"source": "rtsps://a"}})
    assert (await mtx.paths())["p1"]["source"] == "rtsps://a"


async def test_delete_path_ignores_404() -> None:
    stored: dict[str, dict[str, Any]] = {"p1": {}}
    mtx, _ = make(stored)
    await mtx.delete_path("p1")
    await mtx.delete_path("p1")
    assert stored == {}


async def test_ready_reads_the_runtime_list() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/paths/list"
        items = [{"name": "p1", "ready": True}, {"name": "p2", "ready": False}]
        return httpx.Response(200, json={"pageCount": 1, "itemCount": 2, "items": items})

    mtx = MediaMtx("http://mtx", transport=httpx.MockTransport(handle))
    assert await mtx.ready() == {"p1": True, "p2": False}


async def test_received_reads_the_ready_paths_byte_counts() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/paths/list"
        items = [
            {"name": "p1", "ready": True, "bytesReceived": 9084602},
            {"name": "p2", "ready": False, "bytesReceived": 5},
            {"name": "p3", "ready": True},
        ]
        return httpx.Response(200, json={"pageCount": 1, "itemCount": 3, "items": items})

    mtx = MediaMtx("http://mtx", transport=httpx.MockTransport(handle))
    # A path that is not ready, or lists no count, has nothing to judge.
    assert await mtx.received() == {"p1": 9084602}
