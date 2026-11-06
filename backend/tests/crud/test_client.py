"""The workers' CRUD client (#5298, plan D4, D5): a missing route retries, a refusal
raises, a stale credential is replaced once, and a retry keeps its idempotency key."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry
from temporalio import activity

from scadbuddy.auth.service_credentials import ServiceAuth
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


async def test_a_second_403_out_of_scope_is_refused() -> None:
    script = Script(problem(403, reason="out_of_scope"))
    client, source = make(script)
    with pytest.raises(CrudRefusedError):
        await client.request("GET", PATH)
    assert len(script.requests) == 2
    assert source.invalidated == ["t1"]


async def test_another_403_is_refused_without_a_refresh() -> None:
    script = Script(problem(403, reason="other"))
    client, source = make(script)
    with pytest.raises(CrudRefusedError):
        await client.request("GET", PATH)
    assert len(script.requests) == 1
    assert source.invalidated == []


async def test_a_401_is_retried_once_with_the_new_credential() -> None:
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


async def test_a_persistent_5xx_is_unavailable() -> None:
    client, _ = make(Script(problem(500)))
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
