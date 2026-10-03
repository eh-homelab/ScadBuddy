"""The browser trace relay as ``create_app`` mounts it (spec 2026-10-01 §5.2, §6).

The route's own behaviour is in ``tests/test_trace_relay_route.py``; this checks the
wiring only the real app has: the root mount, the body gate's route limit, the SPA
mount after it, the excluded server span, the component and its counter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.support.otlp import export, span

PATH = "/telemetry/v1/traces"
UI = {"Origin": "http://localhost:5173", "Content-Type": "application/json"}
#: Nothing listens on the discard port: a forwarded batch fails, which no test waits on.
UNREACHABLE_COLLECTOR = "http://127.0.0.1:9"


@contextmanager
def app_client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str | None
) -> Iterator[TestClient]:
    """The real app, with a bundle so the SPA's fallback is mounted. The endpoint is read
    when the app is built; the tests' own span provider is kept either way."""
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    if endpoint is None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    else:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>ScadBuddy</title>", encoding="utf-8")
    with TestClient(create_app(settings.model_copy(update={"frontend_dir": dist}))) as client:
        yield client


@pytest.fixture
def relay_client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with app_client(settings, tmp_path, monkeypatch, UNREACHABLE_COLLECTOR) as client:
        yield client


def test_without_a_collector_the_relay_answers_off(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app_client(settings, tmp_path, monkeypatch, None) as client:
        response = client.post(PATH, content=export(span()), headers=UI)
    assert response.status_code == 204
    assert response.headers["x-scadbuddy-tracing"] == "off"


def test_with_a_collector_a_batch_is_accepted(relay_client: TestClient) -> None:
    response = relay_client.post(PATH, content=export(span()), headers=UI)
    assert response.status_code == 204
    assert "x-scadbuddy-tracing" not in response.headers


def test_the_body_gate_holds_it_to_256_kib(relay_client: TestClient) -> None:
    declared = relay_client.post(PATH, content=b" " * (256 * 1024 + 1), headers=UI)
    assert declared.status_code == 413
    assert declared.json()["detail"].startswith("a trace batch is at most 0.25 MB")

    def chunks() -> Iterator[bytes]:
        for _ in range(5):
            yield b" " * (64 * 1024)

    streamed = relay_client.post(PATH, content=chunks(), headers=UI)
    assert streamed.status_code == 413
    assert streamed.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("path", [PATH, "/telemetry/v1/traces/", "/telemetry"])
def test_the_page_is_never_served_for_it(relay_client: TestClient, path: str) -> None:
    response = relay_client.get(path)
    assert response.status_code in (404, 405)
    assert response.headers["content-type"] == "application/problem+json"


def test_it_is_not_traced(relay_client: TestClient, spans: InMemorySpanExporter) -> None:
    assert relay_client.post(PATH, content=export(span()), headers=UI).status_code == 204
    servers = [s for s in spans.get_finished_spans() if s.kind is SpanKind.SERVER]
    assert servers == []


def test_its_counter_is_scraped_from_zero(relay_client: TestClient) -> None:
    text = relay_client.get("/metrics").text
    for outcome in ("forwarded", "failed", "queue_full", "shutdown"):
        assert f'scadbuddy_trace_relay_batches_total{{outcome="{outcome}"}} 0.0' in text


def test_it_is_not_in_the_openapi_schema(relay_client: TestClient) -> None:
    paths = relay_client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/telemetry")]
