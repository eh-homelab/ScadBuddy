"""The workers' CRUD client (#5298, plan D4, D5): a missing route retries, a refusal
raises, a stale credential is replaced once, and a retry keeps its idempotency key."""

from __future__ import annotations

import hashlib
import http.server
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry
from temporalio import activity

from scadbuddy.auth.service_credentials import ExchangeReader, ServiceAuth
from scadbuddy.core.metrics import Metrics
from scadbuddy.crud.client import (
    CrudClient,
    CrudRefusedError,
    CrudSyncClient,
    CrudUnavailableError,
    activity_key,
)

HEADER = {"X-ScadBuddy-Crud": "1"}
PATH = "/api/v1/internal/crud/render/jobs/abc"


class Source:
    def __init__(self) -> None:
        self.tokens = ["t1", "t2", "t3"]
        self.invalidated: list[str] = []
        self.current = 0

    def token(self) -> str | None:
        return self.tokens[min(self.current, len(self.tokens) - 1)]

    def invalidate(self, token: str) -> None:
        self.invalidated.append(token)
        self.current += 1


class Script:
    """A transport answering from a list, one per request, and keeping the requests."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True}, headers=HEADER)


def problem(status: int, **extra: Any) -> httpx.Response:
    body = {"type": "about:blank", "title": "x", "status": status, "detail": "d", **extra}
    return httpx.Response(status, json=body, headers=HEADER)


def make(
    script: Script, registry: Metrics | None = None, budget: float = 0.2
) -> tuple[CrudClient, Source]:
    source = Source()
    client = CrudClient(
        "http://api",
        "render",
        ServiceAuth(source),
        transport=httpx.MockTransport(script),
        metrics=registry,
        unavailable_budget=budget,
        backoff=0.01,
    )
    return client, source


def count(metrics: Metrics, outcome: str) -> float:
    value = metrics.registry.get_sample_value(
        "scadbuddy_crud_client_seconds_count",
        {"caller": "render", "area": "jobs", "outcome": outcome},
    )
    return value or 0.0


async def test_ok_carries_the_credential() -> None:
    script = Script(ok())
    client, _ = make(script)
    response = await client.request("GET", PATH)
    assert response.json() == {"ok": True}
    assert script.requests[0].headers["Authorization"] == "Bearer t1"


async def test_a_404_without_the_header_retries_then_is_unavailable() -> None:
    script = Script(httpx.Response(404, text="no"))
    metrics = Metrics(CollectorRegistry())
    client, _ = make(script, metrics)
    with pytest.raises(CrudUnavailableError):
        await client.request("GET", PATH)
    assert len(script.requests) > 1
    assert count(metrics, "route_missing") == len(script.requests)


async def test_a_route_missing_then_found_succeeds() -> None:
    script = Script(httpx.Response(404, text="no"), ok())
    client, _ = make(script)
    assert (await client.request("GET", PATH)).status_code == 200


async def test_a_404_with_the_header_is_refused_with_its_problem() -> None:
    script = Script(problem(404, reason="gone"))
    metrics = Metrics(CollectorRegistry())
    client, _ = make(script, metrics)
    with pytest.raises(CrudRefusedError) as raised:
        await client.request("GET", PATH)
    assert raised.value.status == 404
    assert raised.value.problem["reason"] == "gone"
    assert len(script.requests) == 1
    assert count(metrics, "refused") == 1


async def test_a_403_out_of_scope_refreshes_once_and_retries() -> None:
    script = Script(problem(403, reason="out_of_scope"), ok())
    metrics = Metrics(CollectorRegistry())
    client, source = make(script, metrics)
    response = await client.request("GET", PATH)
    assert response.status_code == 200
    assert source.invalidated == ["t1"]
    assert script.requests[1].headers["Authorization"] == "Bearer t2"
    assert count(metrics, "reexchange") == 1


async def test_a_403_out_of_scope_that_never_clears_is_unavailable() -> None:
    script = Script(problem(403, reason="out_of_scope"))
    client, _ = make(script)
    with pytest.raises(CrudUnavailableError):
        await client.request("GET", PATH)
    assert len(script.requests) > 2


@pytest.mark.parametrize("reason", ["other", "wrong_caller"])
async def test_another_403_is_refused_without_a_refresh(reason: str) -> None:
    """Another caller's routes (`wrong_caller`) are refused at once: no new credential
    of this caller clears them."""
    script = Script(problem(403, reason=reason))
    client, source = make(script)
    with pytest.raises(CrudRefusedError):
        await client.request("GET", PATH)
    assert len(script.requests) == 1
    assert source.invalidated == []


async def test_a_401_is_retried_with_the_new_credential() -> None:
    script = Script(problem(401), ok())
    client, source = make(script)
    assert (await client.request("GET", PATH)).status_code == 200
    assert source.invalidated == ["t1"]
    assert script.requests[1].headers["Authorization"] == "Bearer t2"


async def test_5xx_and_connect_errors_retry_with_the_same_key() -> None:
    script = Script(problem(503), httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), ok())
    metrics = Metrics(CollectorRegistry())
    client, _ = make(script, metrics, budget=5.0)
    response = await client.request("POST", PATH, json={"a": 1}, idempotency_key="k-1")
    assert response.status_code == 200
    assert [r.headers["Idempotency-Key"] for r in script.requests] == ["k-1"] * 4
    assert count(metrics, "unavailable") == 1
    assert count(metrics, "transport") == 2
    assert count(metrics, "ok") == 1


async def test_a_persistent_5xx_stops_at_the_deadline() -> None:
    script = Script(problem(500))
    client, _ = make(script, budget=0.2)
    started = time.monotonic()
    with pytest.raises(CrudUnavailableError):
        await client.request("GET", PATH)
    assert 0.2 <= time.monotonic() - started < 1.0
    assert 2 <= len(script.requests) <= 25


class PortServer:
    """A keep-alive HTTP/1.1 server: the first request is a bare 404 (no CRUD header),
    later ones are 200 with it; it records each request's client port."""

    def __init__(self) -> None:
        self.ports: list[int] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                outer.ports.append(self.client_address[1])
                first = len(outer.ports) == 1
                body = b"{}"
                self.send_response(404 if first else 200)
                if not first:
                    self.send_header("X-ScadBuddy-Crud", "1")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


async def test_a_retry_after_a_missing_route_uses_a_new_connection() -> None:
    server = PortServer()
    client = CrudClient(
        server.url, "render", ServiceAuth(Source()), unavailable_budget=5.0, backoff=0.01
    )
    try:
        assert (await client.request("GET", PATH)).status_code == 200
    finally:
        await client.aclose()
        server.stop()
    assert len(server.ports) == 2
    assert server.ports[0] != server.ports[1]


def test_the_sync_retry_after_a_missing_route_uses_a_new_connection() -> None:
    server = PortServer()
    client = CrudSyncClient(
        server.url, "render", ServiceAuth(Source()), unavailable_budget=5.0, backoff=0.01
    )
    try:
        assert client.request("GET", PATH).status_code == 200
    finally:
        client.close()
        server.stop()
    assert server.ports[0] != server.ports[1]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def exchange_and_api(clock: FakeClock, scopes_ok_after: int) -> tuple[Any, list[str]]:
    """The exchange route and a CRUD route. Tokens before the ``scopes_ok_after``-th
    (an old API pod's) lack the scope: 403. A request with no token: 401. Each request
    takes 1 s."""
    issued = 0
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        clock.now += 1.0
        if request.url.path == "/api/v1/auth/service-token":
            issued += 1
            token = f"sbsvc_{issued:016x}." + "a" * 43
            return httpx.Response(200, json={"access_token": token, "expires_in": 900})
        header = request.headers.get("Authorization")
        seen.append(header or "none")
        if header is None:
            return problem(401)
        if int(header.removeprefix("Bearer sbsvc_")[:16], 16) < scopes_ok_after:
            return problem(403, reason="out_of_scope")
        return ok()

    return handler, seen


def real_reader(tmp_path: Path, clock: FakeClock, handler: Any) -> ExchangeReader:
    proof = tmp_path / "proof"
    proof.write_text("jwt\n")
    return ExchangeReader(
        "render",
        "http://api",
        proof,
        "k8s",
        clock=clock,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("scopes_ok_after", [2, 3])
async def test_a_stale_scope_waits_out_the_exchange_hold_off(
    tmp_path: Path, scopes_ok_after: int
) -> None:
    """A new worker exchanged with an old API pod: the 403 drops the credential, the
    reader holds the next exchange off for 10 s, the retries wait, and the call lands."""
    clock = FakeClock()
    handler, seen = exchange_and_api(clock, scopes_ok_after)
    reader = real_reader(tmp_path, clock, handler)
    client = CrudClient(
        "http://api",
        "render",
        ServiceAuth(reader),
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
        backoff=0.01,
    )
    try:
        response = await client.request("GET", PATH)
    except CrudUnavailableError:
        assert scopes_ok_after > 2
        return
    assert response.status_code == 200
    assert seen[0] != seen[-1]
    await client.aclose()


async def test_an_old_pod_that_keeps_answering_ends_unavailable_not_refused(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    handler, _ = exchange_and_api(clock, 10**6)
    client = CrudClient(
        "http://api",
        "render",
        ServiceAuth(real_reader(tmp_path, clock, handler)),
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
        backoff=0.01,
    )
    with pytest.raises(CrudUnavailableError):
        await client.request("GET", PATH)


async def test_no_key_sends_no_header() -> None:
    script = Script(ok())
    client, _ = make(script)
    await client.request("GET", PATH)
    assert "Idempotency-Key" not in script.requests[0].headers


def test_the_sync_client_retries_alike() -> None:
    script = Script(httpx.ConnectError("refused"), problem(403, reason="out_of_scope"), ok())
    source = Source()
    client = CrudSyncClient(
        "http://api",
        "render",
        ServiceAuth(source),
        transport=httpx.MockTransport(script),
        unavailable_budget=1.0,
        backoff=0.01,
    )
    assert client.request("PUT", PATH, json={}, idempotency_key="k").status_code == 200
    assert source.invalidated == ["t1"]
    assert {r.headers["Idempotency-Key"] for r in script.requests} == {"k"}
    assert script.requests[0].headers["Authorization"] == "Bearer t1"


def test_the_sync_client_counts_each_outcome() -> None:
    metrics = Metrics(CollectorRegistry())
    client = CrudSyncClient(
        "http://api",
        "render",
        ServiceAuth(Source()),
        transport=httpx.MockTransport(Script(httpx.ConnectError("x"), problem(503), ok())),
        metrics=metrics,
        unavailable_budget=1.0,
        backoff=0.01,
    )
    client.request("GET", PATH)
    assert [count(metrics, o) for o in ("transport", "unavailable", "ok")] == [1, 1, 1]


def test_a_sync_refusal_raises() -> None:
    client = CrudSyncClient(
        "http://api",
        "render",
        ServiceAuth(Source()),
        transport=httpx.MockTransport(Script(problem(409, reason="x"))),
    )
    with pytest.raises(CrudRefusedError):
        client.request("POST", PATH, json={})


def test_activity_key_is_a_hash_of_the_activity_and_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = SimpleNamespace(workflow_id="wf", activity_id="7")
    monkeypatch.setattr(activity, "info", lambda: info)
    key = activity_key(2)
    assert key == hashlib.sha256(b"wf\x007\x002").hexdigest()
    assert key != activity_key(3)


def _clocked(script: Script, clock: FakeClock, budget: float) -> CrudSyncClient:
    def sleep(seconds: float) -> None:
        clock.now += seconds

    return CrudSyncClient(
        "http://api",
        "render",
        ServiceAuth(Source()),
        transport=httpx.MockTransport(script),
        unavailable_budget=budget,
        backoff=0.5,
        clock=clock,
        sleep=sleep,
    )


def test_a_calls_budget_replaces_the_clients() -> None:
    clock = FakeClock()
    script = Script(problem(503))
    client = _clocked(script, clock, budget=30.0)
    with pytest.raises(CrudUnavailableError) as failed:
        client.request("GET", PATH, budget=1.0)
    assert clock.now == pytest.approx(1.0)
    assert failed.value.outcome == "unavailable" and not failed.value.unreachable
    clock.now = 0.0
    with pytest.raises(CrudUnavailableError):
        client.request("GET", PATH)
    assert clock.now == pytest.approx(30.0)


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (httpx.ConnectError("refused"), "transport"),
        (httpx.Response(404), "route_missing"),
    ],
)
def test_an_api_out_of_reach_is_unreachable(
    answer: httpx.Response | Exception, outcome: str
) -> None:
    client = _clocked(Script(answer), FakeClock(), budget=1.0)
    with pytest.raises(CrudUnavailableError) as failed:
        client.request("GET", PATH)
    assert failed.value.outcome == outcome and failed.value.unreachable


async def test_blocking_shares_the_api_credential_and_options() -> None:
    script = Script(problem(503), ok())
    client, source = make(script, budget=1.0)
    try:
        blocking = client.blocking()
        try:
            assert blocking.request("PUT", PATH, json={}, idempotency_key="k").status_code == 200
        finally:
            blocking.close()
    finally:
        await client.aclose()
    assert {str(r.url) for r in script.requests} == {f"http://api{PATH}"}
    assert {r.headers["Authorization"] for r in script.requests} == {"Bearer t1"}
    assert source.invalidated == []


class _AsyncOnly(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return ok()


async def test_blocking_refuses_a_transport_that_serves_no_blocking_request() -> None:
    client = CrudClient("http://api", "render", ServiceAuth(Source()), transport=_AsyncOnly())
    try:
        with pytest.raises(TypeError, match="_AsyncOnly"):
            client.blocking()
    finally:
        await client.aclose()
