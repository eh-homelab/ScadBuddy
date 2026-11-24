from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest

from scadbuddy.camera.mediamtx import MediaMtx
from scadbuddy.camera.process import healthy, serve_health


def mtx(status: int | None, running: tuple[str, ...] = ()) -> MediaMtx:
    """A MediaMTX whose API answers ``status`` with the ``running`` paths in its runtime
    list, or refuses the connection for ``None``."""

    def handle(request: httpx.Request) -> httpx.Response:
        if status is None:
            raise httpx.ConnectError("refused", request=request)
        assert request.url.path == "/v3/paths/list"
        items = [{"name": name, "ready": True} for name in running]
        return httpx.Response(
            status, json={"pageCount": 1, "itemCount": len(items), "items": items}
        )

    return MediaMtx("http://mtx", transport=httpx.MockTransport(handle))


async def get(path: str, alive: Callable[[], Awaitable[bool]]) -> httpx.Response:
    server = await serve_health(0, alive)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient() as http:
            return await http.get(f"http://127.0.0.1:{port}{path}")
    finally:
        server.close()


async def test_healthz_200_when_alive() -> None:
    async def alive() -> bool:
        return True

    response = await get("/healthz", alive)
    assert response.status_code == 200


async def test_healthz_503_when_not_alive() -> None:
    async def alive() -> bool:
        return False

    response = await get("/healthz", alive)
    assert response.status_code == 503


async def test_healthz_503_when_the_check_fails() -> None:
    async def alive() -> bool:
        raise RuntimeError("boom")

    response = await get("/healthz", alive)
    assert response.status_code == 503


async def test_healthz_503_when_the_check_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scadbuddy.camera.process.CHECK_BUDGET", 0.05)

    async def alive() -> bool:
        await asyncio.sleep(10)
        return True

    response = await get("/healthz", alive)
    assert response.status_code == 503


async def test_metrics_answer_whatever_the_check() -> None:
    async def alive() -> bool:
        return False

    response = await get("/metrics", alive)
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


async def _forever() -> None:
    await asyncio.Event().wait()


async def test_healthy_with_mediamtx_and_a_running_follower() -> None:
    follower = asyncio.create_task(_forever())
    try:
        assert await healthy(mtx(200), follower)
    finally:
        follower.cancel()


async def test_unhealthy_when_mediamtx_does_not_answer() -> None:
    follower = asyncio.create_task(_forever())
    try:
        assert not await healthy(mtx(None), follower)
        assert not await healthy(mtx(500), follower)
    finally:
        follower.cancel()


async def test_unhealthy_when_the_follower_ended() -> None:
    follower = asyncio.create_task(asyncio.sleep(0))
    await follower
    assert not await healthy(mtx(200), follower)


async def test_before_the_follower_starts_only_mediamtx_counts() -> None:
    """The process waits for the CRUD service or reads its first list: a restart cannot help."""
    assert await healthy(mtx(200), None)
    assert not await healthy(mtx(None), None)


async def test_unhealthy_when_a_wanted_path_is_missing() -> None:
    """MediaMTX's container restarted and lost the controller's paths."""
    follower = asyncio.create_task(_forever())
    try:
        assert await healthy(mtx(200, ("p1", "p2")), follower, {"p1", "p2"})
        assert not await healthy(mtx(200, ("p1",)), follower, {"p1", "p2"})
        assert not await healthy(mtx(200), None, {"p1"})
    finally:
        follower.cancel()


async def test_healthz_503_when_a_wanted_path_is_missing() -> None:
    wanted = {"p1", "p2"}

    async def all_present() -> bool:
        return await healthy(mtx(200, ("p1", "p2", "other")), None, wanted)

    async def one_missing() -> bool:
        return await healthy(mtx(200, ("p2",)), None, wanted)

    assert (await get("/healthz", all_present)).status_code == 200
    assert (await get("/healthz", one_missing)).status_code == 503


def test_a_refusal_from_run_exits_2(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The recorder's clock check at start refuses from inside ``run``."""
    from scadbuddy.camera import process
    from scadbuddy.camera.settings import CameraRefusal

    monkeypatch.setattr(process, "forbid_dumping", lambda: None)
    monkeypatch.setattr(process, "configure_logging", lambda *args: None)

    async def run(settings: object, stop: asyncio.Event) -> None:
        raise CameraRefusal(["the recorder's clock is +3.0 s off the API's"])

    status = process.main_with(lambda environ: None, object, lambda settings: "INFO", run)
    assert status == 2
    assert "+3.0 s off" in caplog.text
