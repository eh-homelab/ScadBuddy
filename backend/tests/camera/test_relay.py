from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
import pytest

from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.camera.follow import ChangeFollower
from scadbuddy.camera.mediamtx import MediaMtx
from scadbuddy.camera.relay import (
    HOOK_URL,
    Relay,
    SourceError,
    rtsps_source,
    serve_hooks,
)
from scadbuddy.crud.client import CrudClient
from tests.camera.test_follow import Token, frame, sse, until

BASE = "/api/v1/internal/crud/camera-relay"
FINGERPRINT = "ab" * 32
CRUD = {"X-ScadBuddy-Crud": "1"}


def source(printer_id: str, host: str = "10.0.0.5", code: str = "12345678") -> dict[str, Any]:
    return {
        "printer_id": printer_id,
        "host": host,
        "port": 322,
        "path": "/streaming/live/1",
        "cert_sha256": FINGERPRINT,
        "access_code": code,
    }


class FakeApi:
    def __init__(self, sources: list[dict[str, Any]]) -> None:
        self.sources = sources
        self.feeds: list[tuple[str, dict[str, Any]]] = []
        self.stream: list[bytes] = []
        self.hold = asyncio.Event()
        #: The next feed POSTs that answer 503.
        self.feed_failures = 0
        #: The next feed POSTs that the API takes, and whose answer is then lost.
        self.lost_answers = 0
        #: The next feed POSTs that the API refuses (422).
        self.feed_refusals = 0
        self.feed_posts = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(BASE)
        if path == "/changes":
            return sse(self.stream, self.hold)
        if path == "/printers":
            return httpx.Response(200, headers=CRUD, json=self.sources)
        parts = path.split("/")
        if request.method == "POST" and parts[-1] == "feed":
            self.feed_posts += 1
            if self.feed_refusals > 0:
                self.feed_refusals -= 1
                return httpx.Response(422, headers=CRUD, json={"detail": "refused"})
            if self.feed_failures > 0:
                self.feed_failures -= 1
                return httpx.Response(503, headers=CRUD, json={"detail": "down"})
            self.feeds.append((parts[2], json.loads(request.content)))
            if self.lost_answers > 0:
                self.lost_answers -= 1
                raise httpx.ReadTimeout("answer lost", request=request)
            return httpx.Response(204, headers=CRUD)
        found = [s for s in self.sources if s.get("printer_id") == parts[2]]
        if not found:
            problem = {"detail": "This printer is not recorded.", "reason": "not_recorded"}
            return httpx.Response(404, headers=CRUD, json=problem)
        return httpx.Response(200, headers=CRUD, json=found[0])

    def client(self) -> CrudClient:
        return CrudClient(
            "http://api",
            "camera-relay",
            ServiceAuth(Token()),
            transport=httpx.MockTransport(lambda request: self.handle(request)),
            unavailable_budget=0.0,
        )

    def states(self, printer_id: str) -> list[str]:
        return [body["state"] for pid, body in self.feeds if pid == printer_id]


class FakeMtx:
    def __init__(self, paths: dict[str, dict[str, Any]] | None = None) -> None:
        self.paths: dict[str, dict[str, Any]] = paths or {}
        self.calls: list[tuple[str, str]] = []
        self.fail = False
        #: The runtime ``ready`` flag per path; false when absent.
        self.ready: dict[str, bool] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        name = path.rsplit("/", 1)[-1]
        if path == "/v3/paths/list":
            # The runtime list's fields, measured on MediaMTX v1.21.4.
            runtime = [
                {"name": n, "confName": n, "ready": self.ready.get(n, False), "online": True}
                for n in self.paths
            ]
            return httpx.Response(200, json={"pageCount": 1, "items": runtime})
        if path.endswith("/list"):
            # As MediaMTX 1.x does (measured 2026-11-24, bluenviron/mediamtx:1).
            items = [
                {
                    "name": n,
                    **c,
                    "source": re.sub(r"//[^@/]*@", "//<redacted>:<redacted>@", c["source"]),
                }
                for n, c in self.paths.items()
            ]
            return httpx.Response(200, json={"pageCount": 1, "items": items})
        verb = path.split("/")[-2]
        self.calls.append((verb, name))
        if self.fail:
            return httpx.Response(400, json={"error": "invalid", "echo": request.content.decode()})
        if verb == "replace" and name not in self.paths:
            return httpx.Response(404)
        if verb in ("replace", "add"):
            self.paths[name] = json.loads(request.content)
        elif verb == "delete":
            if name not in self.paths:
                return httpx.Response(404)
            del self.paths[name]
        return httpx.Response(200)

    def client(self) -> MediaMtx:
        return MediaMtx("http://mtx", transport=httpx.MockTransport(self.handle))


def relay(api: FakeApi, mtx: FakeMtx) -> Relay:
    return Relay(api.client(), mtx.client(), retry_min=0.01, retry_max=0.02)


def test_source_url_embeds_the_code() -> None:
    from scadbuddy.camera.models import CameraSourceWithCode

    parsed = CameraSourceWithCode.model_validate(source("p1"))
    assert rtsps_source(parsed) == "rtsps://bblp:12345678@10.0.0.5:322/streaming/live/1"


async def test_reconcile_all_adds_and_removes() -> None:
    api = FakeApi([source("p1"), source("p2", host="10.0.0.6")])
    mtx = FakeMtx({"old": {"source": "rtsps://bblp:x@10.0.0.9:322/streaming/live/1"}})
    subject = relay(api, mtx)
    await subject.reconcile_all()
    assert set(mtx.paths) == {"p1", "p2"}
    conf = mtx.paths["p1"]
    assert conf["source"] == "rtsps://bblp:12345678@10.0.0.5:322/streaming/live/1"
    assert conf["sourceFingerprint"] == FINGERPRINT
    assert conf["sourceOnDemand"] is False
    assert conf["runOnAvailable"] == f"curl -fsS -X POST {HOOK_URL}/$MTX_PATH/ready"
    assert conf["runOnUnavailable"] == f"curl -fsS -X POST {HOOK_URL}/$MTX_PATH/not_ready"
    await subject.flushed()
    assert api.states("old") == ["stopped"]


async def test_hooks_call_the_given_url() -> None:
    """A test runs two camera processes on one host: each one's hooks name its own port."""
    api, mtx = FakeApi([source("p1")]), FakeMtx()
    subject = Relay(api.client(), mtx.client(), hook_url="http://127.0.0.1:40001/hook")
    await subject.reconcile_all()
    conf = mtx.paths["p1"]
    assert conf["runOnAvailable"] == "curl -fsS -X POST http://127.0.0.1:40001/hook/$MTX_PATH/ready"
    assert conf["runOnUnavailable"] == (
        "curl -fsS -X POST http://127.0.0.1:40001/hook/$MTX_PATH/not_ready"
    )
    await subject.aclose()


async def test_reconcile_all_is_idempotent() -> None:
    api = FakeApi([source("p1")])
    mtx = FakeMtx()
    subject = relay(api, mtx)
    await subject.reconcile_all()
    await subject.flushed()
    calls, feeds = list(mtx.calls), list(api.feeds)
    await subject.reconcile_all()
    await subject.reconcile("p1")
    assert mtx.calls == calls
    await subject.flushed()
    assert api.feeds == feeds


async def test_reconcile_replaces_on_a_new_code() -> None:
    # MediaMTX lists the source redacted, so only the relay's own copy shows the change.
    api = FakeApi([source("p1")])
    mtx = FakeMtx()
    subject = relay(api, mtx)
    await subject.reconcile_all()
    api.sources = [source("p1", code="87654321")]
    await subject.reconcile_all()
    assert mtx.paths["p1"]["source"] == "rtsps://bblp:87654321@10.0.0.5:322/streaming/live/1"


async def test_reconcile_removes_unqualified() -> None:
    api = FakeApi([source("p1")])
    mtx = FakeMtx()
    subject = relay(api, mtx)
    await subject.reconcile("p1")
    await subject.on_hook("p1", "ready")
    api.sources = []
    await subject.reconcile("p1")
    assert mtx.paths == {}
    assert ("delete", "p1") in mtx.calls
    await subject.flushed()
    assert api.states("p1")[-1] == "stopped"
    # A late hook for the removed path reports nothing.
    await subject.on_hook("p1", "not_ready")
    await subject.flushed()
    assert api.states("p1")[-1] == "stopped"


async def test_stopped_is_retried_until_it_lands() -> None:
    api = FakeApi([source("p1")])
    mtx = FakeMtx()
    subject = relay(api, mtx)
    await subject.reconcile_all()
    await subject.flushed()
    api.sources = []
    api.feed_failures = 3
    await subject.reconcile_all()
    assert mtx.paths == {}
    await subject.flushed()
    assert api.states("p1") == ["retrying", "stopped"]
    await subject.reconcile_all()
    await subject.flushed()
    assert api.states("p1") == ["retrying", "stopped"]


async def test_failed_hook_report_is_retried() -> None:
    api = FakeApi([source("p1")])
    subject = relay(api, FakeMtx())
    await subject.reconcile_all()
    await subject.flushed()
    api.feed_failures = 2
    await subject.on_hook("p1", "ready")
    await subject.flushed()
    assert api.states("p1") == ["retrying", "pulling"]


async def test_newer_state_replaces_a_pending_retry() -> None:
    api = FakeApi([source("p1")])
    subject = Relay(api.client(), FakeMtx().client(), retry_min=0.2, retry_max=0.2)
    await subject.reconcile_all()
    await subject.flushed()
    api.feed_failures = 1
    await subject.on_hook("p1", "ready")
    await until(lambda: api.feed_failures == 0)
    await subject.on_hook("p1", "not_ready")
    await subject.flushed()
    # ``pulling`` failed and is never sent again: ``retrying`` replaced it. A failed post
    # forgets the state the API took, so ``retrying`` is posted once more.
    assert api.states("p1") == ["retrying", "retrying"]


async def test_waiting_sender_posts_a_newer_state() -> None:
    api = FakeApi([source("p1")])
    subject = Relay(api.client(), FakeMtx().client(), retry_min=0.2, retry_max=0.2)
    await subject.reconcile_all()
    await subject.flushed()
    api.feed_failures = 1
    await subject.on_hook("p1", "ready")
    await until(lambda: api.feed_failures == 0)
    api.sources = []
    await subject.reconcile_all()
    await subject.flushed()
    assert api.states("p1") == ["retrying", "stopped"]


async def test_lost_answer_does_not_hide_a_state() -> None:
    api = FakeApi([source("p1")])
    subject = Relay(api.client(), FakeMtx().client(), retry_min=0.2, retry_max=0.2)
    await subject.reconcile_all()
    await subject.flushed()
    # The API takes ``pulling``, and its answer is lost.
    api.lost_answers = 1
    await subject.on_hook("p1", "ready")
    await until(lambda: api.lost_answers == 0)
    await subject.on_hook("p1", "not_ready")
    await subject.flushed()
    assert api.states("p1")[-1] == "retrying"


async def test_refused_report_is_not_retried(caplog: pytest.LogCaptureFixture) -> None:
    api = FakeApi([source("p1")])
    subject = relay(api, FakeMtx())
    await subject.reconcile_all()
    await subject.flushed()
    api.feed_refusals = 1
    posts = api.feed_posts
    await subject.on_hook("p1", "ready")
    await subject.flushed()
    await asyncio.sleep(0.1)
    assert api.feed_posts == posts + 1
    assert api.states("p1") == ["retrying"]
    assert "422" in caplog.text
    # A later report tries again.
    await subject.on_hook("p1", "ready")
    await subject.flushed()
    assert api.states("p1") == ["retrying", "pulling"]


async def test_reconcile_reports_runtime_state() -> None:
    api = FakeApi([source("p1")])
    mtx = FakeMtx()
    subject = relay(api, mtx)
    await subject.reconcile_all()
    await subject.flushed()
    await subject.on_hook("p1", "ready")
    await subject.flushed()
    # The not_ready hook was lost: the runtime list says not ready.
    await subject.reconcile_all()
    await subject.flushed()
    assert api.states("p1") == ["retrying", "pulling", "retrying"]
    # A lost ready hook, the other way.
    mtx.ready["p1"] = True
    await subject.reconcile_all()
    await subject.flushed()
    assert api.states("p1") == ["retrying", "pulling", "retrying", "pulling"]


async def test_reconcile_replaces_on_change() -> None:
    api = FakeApi([source("p1")])
    mtx = FakeMtx()
    subject = relay(api, mtx)
    await subject.reconcile("p1")
    api.sources = [source("p1", host="10.0.0.7")]
    await subject.reconcile("p1")
    assert mtx.paths["p1"]["source"] == "rtsps://bblp:12345678@10.0.0.7:322/streaming/live/1"
    assert mtx.calls.count(("replace", "p1")) == 2


async def test_hook_reports_pulling_and_retrying() -> None:
    api = FakeApi([source("p1")])
    subject = relay(api, FakeMtx())
    await subject.reconcile_all()
    await subject.flushed()
    for event in ("ready", "ready", "not_ready", "ready"):
        await subject.on_hook("p1", event)
        await subject.flushed()
    assert api.states("p1") == ["retrying", "pulling", "retrying", "pulling"]
    assert all(body["part"] == "relay" for _, body in api.feeds)


async def test_hook_server_calls_on_hook() -> None:
    api = FakeApi([source("p1")])
    subject = relay(api, FakeMtx())
    await subject.reconcile_all()
    server = await serve_hooks(subject, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient() as http:
            ok = await http.post(f"http://127.0.0.1:{port}/hook/p1/ready")
            bad = await http.post(f"http://127.0.0.1:{port}/hook/p1/sideways")
    finally:
        server.close()
    assert ok.status_code == 204
    assert bad.status_code == 404
    await subject.flushed()
    assert api.states("p1") == ["retrying", "pulling"]


async def test_source_for_seam() -> None:
    api = FakeApi([source("p1")])
    mtx = FakeMtx()
    subject = Relay(
        api.client(), mtx.client(), source_for=lambda s: f"rtsp://127.0.0.1:8554/{s.printer_id}"
    )
    await subject.reconcile_all()
    assert mtx.paths["p1"]["source"] == "rtsp://127.0.0.1:8554/p1"


async def test_code_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    malformed = source("p2", code="SECRETBAD")
    del malformed["host"]
    api = FakeApi([source("p1", code="SECRETOK1"), malformed])
    mtx = FakeMtx()
    subject = relay(api, mtx)
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
    # MediaMTX refuses the path and echoes the body back.
    api.sources = [source("p1", code="SECRETMTX")]
    mtx.fail = True
    await until(lambda: len(resyncs) >= 4)
    task.cancel()
    mtx.fail = False
    with pytest.raises(SourceError) as raised:
        api.sources = [malformed]
        await subject.reconcile_all()
    logging.getLogger("test").exception("reconcile failed", exc_info=raised.value)
    assert "p2" in caplog.text
    assert "MediaMTX" in caplog.text
    for code in ("SECRETOK1", "SECRETBAD", "SECRETMTX"):
        assert code not in caplog.text


def _sample(name: str, printer_id: str) -> float | None:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, {"printer": printer_id})


async def test_reconnects_count_each_ready_after_a_not_ready() -> None:
    printer = "metrics-reconnects"
    api = FakeApi([source(printer)])
    subject = relay(api, FakeMtx())
    await subject.reconcile_all()
    assert _sample("scadbuddy_camera_relay_pulling", printer) == 0
    before = _sample("scadbuddy_camera_relay_reconnects_total", printer) or 0.0
    await subject.on_hook(printer, "ready")
    await subject.on_hook(printer, "ready")
    assert _sample("scadbuddy_camera_relay_pulling", printer) == 1
    # The first ready is no reconnect.
    assert (_sample("scadbuddy_camera_relay_reconnects_total", printer) or 0.0) == before
    for event in ("not_ready", "ready", "not_ready", "ready"):
        await subject.on_hook(printer, event)
    assert _sample("scadbuddy_camera_relay_reconnects_total", printer) == before + 2
    assert _sample("scadbuddy_camera_relay_pulling", printer) == 1
    await subject.on_hook(printer, "not_ready")
    assert _sample("scadbuddy_camera_relay_pulling", printer) == 0
    await subject.aclose()


async def test_pulling_gauge_goes_with_a_removed_path() -> None:
    printer = "metrics-removed"
    api = FakeApi([source(printer)])
    mtx = FakeMtx()
    subject = relay(api, mtx)
    await subject.reconcile_all()
    for event in ("ready", "not_ready", "ready"):
        await subject.on_hook(printer, event)
    assert _sample("scadbuddy_camera_relay_reconnects_total", printer) == 1
    assert _sample("scadbuddy_camera_relay_pulling", printer) == 1
    api.sources = []
    await subject.reconcile_all()
    assert printer not in mtx.paths
    assert _sample("scadbuddy_camera_relay_pulling", printer) is None
    assert _sample("scadbuddy_camera_relay_reconnects_total", printer) is None
    await subject.aclose()


async def test_wanted_names_the_paths_put() -> None:
    api = FakeApi([source("p1"), source("p2", host="10.0.0.6")])
    subject = relay(api, FakeMtx())
    assert subject.wanted() == set()
    await subject.reconcile_all()
    assert subject.wanted() == {"p1", "p2"}
    api.sources = [source("p2", host="10.0.0.6")]
    await subject.reconcile_all()
    assert subject.wanted() == {"p2"}
    await subject.aclose()


async def test_ready_once_a_full_reconcile_put_the_paths() -> None:
    """``/readyz``: the relay's paths are in MediaMTX, whether their pulls run or not (a
    printer that is off must not hold back the relay's Service)."""
    api = FakeApi([source("p1")])
    subject = relay(api, FakeMtx())
    assert not subject.ready()
    await subject.reconcile_all()
    assert subject.ready()
    await subject.aclose()
