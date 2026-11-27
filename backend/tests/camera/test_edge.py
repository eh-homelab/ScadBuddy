from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry
from pydantic import SecretStr

from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.camera import edge as edge_module
from scadbuddy.camera.edge import BASE, Edge, ViewersCollector
from scadbuddy.camera.follow import ChangeFollower
from scadbuddy.camera.settings import EdgeSettings
from scadbuddy.crud.client import CrudClient
from tests.camera.support import FakeMtx, Token, frame, sse, until

CRUD = {"X-ScadBuddy-Crud": "1"}
READ_PASS = "relay-read-secret"


def source(printer_id: str) -> dict[str, Any]:
    return {
        "printer_id": printer_id,
        "host": "10.0.0.5",
        "port": 322,
        "path": "/streaming/live/1",
        "cert_sha256": "ab" * 32,
    }


class FakeApi:
    """The ``camera`` CRUD routes the edge reads."""

    def __init__(self, sources: list[dict[str, Any]]) -> None:
        self.sources = sources
        self.stream: list[bytes] = []
        self.hold = asyncio.Event()

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(BASE)
        if path == "/changes":
            return sse(self.stream, self.hold)
        if path == "/printers":
            return httpx.Response(200, headers=CRUD, json=self.sources)
        found = [s for s in self.sources if s["printer_id"] == path.split("/")[2]]
        if not found:
            problem = {"detail": "This printer is not recorded.", "reason": "not_recorded"}
            return httpx.Response(404, headers=CRUD, json=problem)
        return httpx.Response(200, headers=CRUD, json=found[0])

    def client(self) -> CrudClient:
        return CrudClient(
            "http://api",
            "camera",
            ServiceAuth(Token()),
            transport=httpx.MockTransport(self.handle),
            unavailable_budget=0.0,
        )


def settings(**overrides: Any) -> EdgeSettings:
    values: dict[str, Any] = {
        "api_internal_url": "http://api",
        "camera_relay_rtsp_url": "rtsp://scadbuddy-camera-relay:8554",
        "camera_relay_read_user": "reader",
        "camera_relay_read_pass": SecretStr(READ_PASS),
    }
    return EdgeSettings(**(values | overrides))


def edge(api: FakeApi, mtx: FakeMtx) -> Edge:
    return Edge(api.client(), mtx.client(), settings())


async def test_reconcile_puts_an_on_demand_path_from_the_relay() -> None:
    api, mtx = FakeApi([source("p1")]), FakeMtx()
    subject = edge(api, mtx)
    await subject.reconcile("p1")
    assert mtx.paths == {
        "p1": {
            "source": f"rtsp://reader:{READ_PASS}@scadbuddy-camera-relay:8554/p1",
            "sourceOnDemand": True,
            "sourceOnDemandCloseAfter": "10s",
            "rtspTransport": "tcp",
        }
    }
    assert subject.wanted() == {"p1"}
    # Unchanged: no second put, so no viewer of the path is cut.
    await subject.reconcile("p1")
    await subject.reconcile_all()
    assert mtx.calls == [("replace", "p1"), ("add", "p1")]


async def test_reconcile_quotes_the_read_secret() -> None:
    api, mtx = FakeApi([source("p1")]), FakeMtx()
    subject = Edge(
        api.client(), mtx.client(), settings(camera_relay_read_pass=SecretStr("a@b/c:d"))
    )
    await subject.reconcile("p1")
    assert mtx.paths["p1"]["source"] == "rtsp://reader:a%40b%2Fc%3Ad@scadbuddy-camera-relay:8554/p1"


async def test_reconcile_removes_a_printer_no_longer_recorded() -> None:
    api, mtx = FakeApi([source("p1")]), FakeMtx()
    subject = edge(api, mtx)
    await subject.reconcile_all()
    assert set(mtx.paths) == {"p1"}
    api.sources = []
    await subject.reconcile("p1")
    assert mtx.paths == {}
    assert subject.wanted() == set()
    # A second 404 has nothing to delete.
    await subject.reconcile("p1")
    assert mtx.calls.count(("delete", "p1")) == 1


async def test_reconcile_all_drops_unknown_paths() -> None:
    api = FakeApi([source("p1"), source("p2")])
    mtx = FakeMtx({"stale": {"source": "rtsp://reader:x@relay:8554/stale"}})
    subject = edge(api, mtx)
    assert not subject.ready()
    await subject.reconcile_all()
    assert set(mtx.paths) == {"p1", "p2"}
    assert subject.ready()
    api.sources = [source("p2")]
    await subject.reconcile_all()
    assert set(mtx.paths) == {"p2"}
    assert subject.wanted() == {"p2"}


async def test_the_read_password_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    api, mtx = FakeApi([source("p1")]), FakeMtx()
    # MediaMTX refuses the path and echoes the body, the source with the secret, back.
    mtx.fail = True
    subject = edge(api, mtx)
    api.stream = [frame("ready", {"resync": True, "reconnect": False}, "1")]
    resyncs: list[int] = []

    async def on_resync() -> None:
        resyncs.append(1)
        await subject.reconcile_all()

    follower = ChangeFollower(
        api.client(), f"{BASE}/changes", subject.reconcile, on_resync, retry_min=0.01
    )
    task = asyncio.create_task(follower.run())
    await until(lambda: len(resyncs) >= 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert "MediaMTX" in caplog.text
    assert READ_PASS not in caplog.text
    assert READ_PASS not in repr(subject)
    assert READ_PASS not in repr(settings())


def test_edge_refuses_a_database_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from scadbuddy.camera import process

    monkeypatch.setattr(process, "configure_logging", lambda *args: None)
    monkeypatch.setenv("SCADBUDDY_DATABASE_URL", "postgresql://x")
    assert edge_module.main() == 2
    assert "SCADBUDDY_DATABASE_URL" in caplog.text


def test_viewers_gauge_reads_readers() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/paths/list"
        # The runtime list's fields, measured on MediaMTX v1.21.4.
        items = [
            {
                "name": "p1",
                "ready": True,
                "readers": [{"type": "hlsMuxer", "id": ""}, {"type": "webRTCSession", "id": "x"}],
            },
            {"name": "p2", "ready": False, "readers": []},
        ]
        return httpx.Response(200, json={"pageCount": 1, "items": items})

    registry = CollectorRegistry()
    registry.register(ViewersCollector("http://mtx", transport=httpx.MockTransport(answer)))
    assert registry.get_sample_value("scadbuddy_camera_viewers", {"printer": "p1"}) == 2
    assert registry.get_sample_value("scadbuddy_camera_viewers", {"printer": "p2"}) == 0


def test_viewers_gauge_is_empty_when_mediamtx_does_not_answer() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    registry = CollectorRegistry()
    registry.register(ViewersCollector("http://mtx", transport=httpx.MockTransport(refuse)))
    assert registry.get_sample_value("scadbuddy_camera_viewers", {"printer": "p1"}) is None
