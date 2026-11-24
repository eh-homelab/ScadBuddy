from __future__ import annotations

import json

import httpx

from scadbuddy.camera.mediamtx import MediaMtx


def make(paths: dict[str, dict]) -> tuple[MediaMtx, list[tuple[str, str, dict | None]]]:
    calls: list[tuple[str, str, dict | None]] = []

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
    stored: dict[str, dict] = {}
    mtx, calls = make(stored)
    await mtx.put_path("p1", {"source": "rtsps://a"})
    await mtx.put_path("p1", {"source": "rtsps://b"})
    assert stored == {"p1": {"source": "rtsps://b"}}
    assert [c[1].split("/")[-2] for c in calls] == ["replace", "add", "replace"]


async def test_paths_lists_by_name() -> None:
    mtx, _ = make({"p1": {"source": "rtsps://a"}})
    assert (await mtx.paths())["p1"]["source"] == "rtsps://a"


async def test_delete_path_ignores_404() -> None:
    stored = {"p1": {}}
    mtx, _ = make(stored)
    await mtx.delete_path("p1")
    await mtx.delete_path("p1")
    assert stored == {}
