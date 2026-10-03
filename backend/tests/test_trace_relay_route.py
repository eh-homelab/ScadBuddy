# backend/tests/test_trace_relay_route.py
"""``POST /telemetry/v1/traces`` over HTTP (spec 2026-10-01 §5.2, §8).

The route in a small app composed as ``main.py`` composes it (the problem handlers, the
body gate with the relay's limit, the SPA's mount last), with the relay's component
overridden: no database or Temporal needed. ``tests/api/test_trace_relay_app.py``
checks the same wiring in the real app."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import telemetry
from scadbuddy.api.components import getter_for
from scadbuddy.api.limits import BODY_LIMITS, BodySizeGate
from scadbuddy.api.static import SPAStaticFiles
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.telemetry import forwarder as forwarder_module
from scadbuddy.telemetry.admission import RelayLimits
from scadbuddy.telemetry.component import TRACE_RELAY, TraceRelay
from scadbuddy.telemetry.forwarder import TraceForwarder
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS
from tests.support.otlp import SENTINEL, export, span, string

PATH = "/telemetry/v1/traces"
ORIGIN = "https://scadbuddy.example"
UI = {"Origin": ORIGIN, "Content-Type": "application/json"}

type Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


class Collector:
    def __init__(self, status: int = 200, *, hang: bool = False) -> None:
        self.status = status
        self.hang = hang
        self.bodies: list[bytes] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        if self.hang:
            await asyncio.sleep(60)
        return httpx.Response(self.status)


def relay_settings(trusted_proxies: str = "") -> Settings:
    return Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        public_url=ORIGIN,
        trusted_proxies=trusted_proxies,
    )


def make_relay(
    collector: Handler | None = None,
    *,
    endpoint: str | None = "http://collector.test:4318",
    limits: RelayLimits | None = None,
    trusted_proxies: str = "",
    drain_seconds: float = 5.0,
) -> TraceRelay:
    settings = relay_settings(trusted_proxies)
    return TraceRelay(
        forwarder=TraceForwarder(
            metrics=Metrics(),
            endpoint=endpoint,
            transport=httpx.MockTransport(collector or Collector()),
            drain_seconds=drain_seconds,
        ),
        limits=limits or RelayLimits(),
        settings=lambda: settings,
    )


def relay_app(relay: TraceRelay, frontend: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with relay.forwarder.running():
            yield

    app = FastAPI(lifespan=lifespan)
    install_problem_handlers(app)
    app.add_middleware(BodySizeGate, limits=BODY_LIMITS, routes=[telemetry.RELAY_ROUTE_LIMIT])
    app.include_router(telemetry.router)
    if frontend is not None:
        app.mount("/", SPAStaticFiles(frontend), name="frontend")
    app.dependency_overrides[getter_for(TRACE_RELAY)] = lambda: relay
    return app


@pytest.fixture
def frontend(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>ScadBuddy</title>", encoding="utf-8")
    return dist


def outcome(relay: TraceRelay, name: str) -> float:
    value = relay.forwarder.metrics.registry.get_sample_value(
        "scadbuddy_trace_relay_batches_total", {"outcome": name}
    )
    assert value is not None
    return value


def until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def assert_problem(response: httpx.Response, status: int, detail: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["status"] == status
    assert response.json()["detail"] == detail
    assert not [name for name in response.headers if name.lower().startswith("access-control-")]


@pytest.fixture
def collector() -> Collector:
    return Collector()


@pytest.fixture
def relay(collector: Collector) -> TraceRelay:
    return make_relay(collector)


@pytest.fixture
def client(relay: TraceRelay) -> Iterator[TestClient]:
    with TestClient(relay_app(relay)) as test_client:
        yield test_client


def test_an_accepted_batch_is_answered_204_and_forwarded_rewritten(
    client: TestClient, relay: TraceRelay, collector: Collector
) -> None:
    body = export(span(), resource=[string("service.name", "scadbuddy-api")])
    response = client.post(PATH, content=body, headers=UI)
    assert response.status_code == 204
    assert "x-scadbuddy-tracing" not in response.headers
    until(lambda: outcome(relay, "forwarded") == 1)
    (sent,) = collector.bodies
    resource = json.loads(sent)["resourceSpans"][0]["resource"]
    assert resource["attributes"][0] == string("service.name", "scadbuddy-web")


def test_a_browser_exception_reaches_the_collector_without_its_message(
    client: TestClient, relay: TraceRelay, collector: Collector
) -> None:
    event = {
        "name": "exception",
        "attributes": [
            string("exception.type", "Error"),
            string("exception.message", SENTINEL),
            string("exception.stacktrace", f"Error: {SENTINEL}\n    at f (https://x/a.js:1:2)"),
        ],
    }
    body = export(span(events=[event], status={"code": 2, "message": SENTINEL}))
    assert client.post(PATH, content=body, headers=UI).status_code == 204
    until(lambda: outcome(relay, "forwarded") == 1)
    assert SENTINEL not in collector.bodies[0].decode()


def test_tracing_off_answers_off_and_forwards_nothing(collector: Collector) -> None:
    relay = make_relay(collector, endpoint=None)
    with TestClient(relay_app(relay)) as client:
        response = client.post(PATH, content=export(span()), headers=UI)
    assert response.status_code == 204
    assert response.headers["x-scadbuddy-tracing"] == "off"
    assert collector.bodies == []


@pytest.mark.parametrize(
    ("headers", "detail"),
    [
        (
            {"Content-Type": "application/json"},
            "the relay accepts requests from ScadBuddy's own pages, which send Origin",
        ),
        ({**UI, "Origin": "https://evil.example"}, "Origin not allowed"),
        ({**UI, "Sec-Fetch-Site": "cross-site"}, "Sec-Fetch-Site must be same-origin"),
    ],
    ids=["no-origin", "foreign-origin", "cross-site"],
)
def test_a_request_not_from_the_page_is_403_before_its_body_is_read(
    client: TestClient, collector: Collector, headers: dict[str, str], detail: str
) -> None:
    # Not JSON at all: a 403 rather than a 400 shows the body was never parsed.
    response = client.post(PATH, content=b"not json", headers=headers)
    assert_problem(response, 403, detail)
    assert collector.bodies == []


def test_a_preflight_is_not_answered(client: TestClient) -> None:
    response = client.options(
        PATH,
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert_problem(response, 405, "the relay accepts POST only")


def test_another_content_type_is_415(client: TestClient) -> None:
    response = client.post(
        PATH, content=export(span()), headers={**UI, "Content-Type": "text/plain"}
    )
    assert_problem(response, 415, "the relay accepts application/json only")


def test_a_body_over_256_kib_is_413_on_its_headers(client: TestClient) -> None:
    response = client.post(PATH, content=b" " * (256 * 1024 + 1), headers=UI)
    assert response.status_code == 413
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["detail"].startswith("a trace batch is at most 0.25 MB")


def test_a_chunked_body_over_256_kib_is_413_as_it_streams(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(5):
            yield b" " * (64 * 1024)

    response = client.post(PATH, content=chunks(), headers=UI)
    assert response.status_code == 413
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE


def test_more_than_512_spans_is_413(client: TestClient) -> None:
    body = json.dumps(
        {"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "s"}] * 513}]}]}
    ).encode()
    assert len(body) < 256 * 1024
    response = client.post(PATH, content=body, headers=UI)
    assert_problem(response, 413, "a trace batch holds at most 512 spans")


def test_a_body_that_is_not_an_export_is_400(client: TestClient) -> None:
    response = client.post(PATH, content=b"{", headers=UI)
    assert_problem(response, 400, "the body is not an OTLP/JSON trace export")


def test_empty_resource_spans_are_400(client: TestClient) -> None:
    body = json.dumps({"resourceSpans": [{} for _ in range(17)]}).encode()
    response = client.post(PATH, content=body, headers=UI)
    assert_problem(response, 400, "the body is not an OTLP/JSON trace export")


def test_a_batch_with_no_valid_span_is_204_and_queues_nothing(
    client: TestClient, relay: TraceRelay, collector: Collector
) -> None:
    response = client.post(PATH, content=export(span(spanId="nope")), headers=UI)
    assert response.status_code == 204
    assert not relay.forwarder._pending
    assert collector.bodies == []
    for name in ("forwarded", "failed", "queue_full", "shutdown"):
        assert outcome(relay, name) == 0


def test_the_payload_is_prepared_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_loop: list[bool] = []
    real = telemetry.prepare

    def spy(body: bytes) -> bytes | None:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(body)

    monkeypatch.setattr(telemetry, "prepare", spy)
    assert client.post(PATH, content=export(span()), headers=UI).status_code == 204
    assert on_loop == [False]


def test_the_per_client_limit_is_429_with_retry_after() -> None:
    relay = make_relay(limits=RelayLimits(client_burst=1, client_per_second=0.5))
    with TestClient(relay_app(relay)) as client:
        assert client.post(PATH, content=export(span()), headers=UI).status_code == 204
        response = client.post(PATH, content=export(span()), headers=UI)
    assert_problem(response, 429, "this client is over the relay's rate limit")
    assert response.headers["retry-after"] in {"1", "2"}


def test_the_per_process_limit_is_429_for_every_client() -> None:
    relay = make_relay(limits=RelayLimits(process_burst=1, process_per_second=0.5))
    app = relay_app(relay)
    with TestClient(app, client=("192.0.2.1", 1000)) as first:
        assert first.post(PATH, content=export(span()), headers=UI).status_code == 204
    with TestClient(app, client=("192.0.2.2", 1000)) as second:
        response = second.post(PATH, content=export(span()), headers=UI)
    assert_problem(response, 429, "the relay is over its overall rate limit")
    assert "retry-after" in response.headers


def test_a_trusted_proxy_names_the_client_the_bucket_counts() -> None:
    relay = make_relay(
        limits=RelayLimits(client_burst=1, client_per_second=0.01),
        trusted_proxies="10.42.0.0/16",
    )
    with TestClient(relay_app(relay), client=("10.42.0.5", 1000)) as gateway:

        def post(*forwarded_for: str) -> int:
            headers = [*UI.items(), *(("X-Forwarded-For", v) for v in forwarded_for)]
            response: httpx.Response = gateway.post(PATH, content=export(span()), headers=headers)
            return response.status_code

        assert post("198.51.100.1, 203.0.113.9") == 204
        assert post("203.0.113.10") == 204
        # Two header lines are one list: its last value is the client already counted.
        assert post("198.51.100.7", "203.0.113.9") == 429
        # An empty last value is the gateway itself, a client of its own.
        assert post("203.0.113.9, ") == 204


def test_an_untrusted_peer_cannot_name_another_client() -> None:
    relay = make_relay(limits=RelayLimits(client_burst=1, client_per_second=0.01))
    with TestClient(relay_app(relay), client=("10.42.0.5", 1000)) as client:
        first = [*UI.items(), ("X-Forwarded-For", "203.0.113.9")]
        second = [*UI.items(), ("X-Forwarded-For", "203.0.113.10")]
        assert client.post(PATH, content=export(span()), headers=first).status_code == 204
        assert client.post(PATH, content=export(span()), headers=second).status_code == 429


def test_a_failing_collector_never_fails_the_browser() -> None:
    relay = make_relay(Collector(500))
    with TestClient(relay_app(relay)) as client:
        assert client.post(PATH, content=export(span()), headers=UI).status_code == 204
        until(lambda: outcome(relay, "failed") == 1)


def test_a_full_queue_never_fails_the_browser(
    client: TestClient, relay: TraceRelay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(forwarder_module, "MAX_QUEUED_BATCHES", 0)
    assert client.post(PATH, content=export(span()), headers=UI).status_code == 204
    assert outcome(relay, "queue_full") == 1


def test_batches_still_queued_at_shutdown_are_counted_not_refused() -> None:
    relay = make_relay(Collector(hang=True), drain_seconds=0.1)
    with TestClient(relay_app(relay)) as client:
        for _ in range(3):
            assert client.post(PATH, content=export(span()), headers=UI).status_code == 204
    assert outcome(relay, "shutdown") >= 1
    assert outcome(relay, "failed") + outcome(relay, "shutdown") == 3


def test_a_closing_relay_answers_503(client: TestClient, relay: TraceRelay) -> None:
    relay.forwarder.closing = True
    response = client.post(PATH, content=export(span()), headers=UI)
    assert_problem(response, 503, "the relay is shutting down")


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE"])
def test_another_method_is_405_never_the_page(
    relay: TraceRelay, frontend: Path, method: str
) -> None:
    with TestClient(relay_app(relay, frontend)) as client:
        response = client.request(method, PATH, headers=UI)
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert "text/html" not in response.headers["content-type"]


@pytest.mark.parametrize(
    "path", ["/telemetry", "/telemetry/", "/telemetry/v1/traces/", "/telemetry/v1/metrics"]
)
def test_no_other_telemetry_path_serves_the_page(
    relay: TraceRelay, frontend: Path, path: str
) -> None:
    with TestClient(relay_app(relay, frontend)) as client:
        response = client.get(path)
    assert_problem(response, 404, "the only telemetry route is POST /telemetry/v1/traces")


def test_the_page_is_still_served_beside_it(relay: TraceRelay, frontend: Path) -> None:
    with TestClient(relay_app(relay, frontend)) as client:
        assert "text/html" in client.get("/models/demo").headers["content-type"]
