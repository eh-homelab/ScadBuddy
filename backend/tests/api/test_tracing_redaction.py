"""Nothing forbidden reaches an exported span, on success or failure (spec 2026-10-01 §6, §8)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.api.conftest import set_fake_env, wait_for_job
from tests.conftest import wait_for_span

SENTINEL = "zz-s3ntinel-zz"


def _everything(spans: InMemorySpanExporter) -> str:
    parts: list[str] = []
    for finished in spans.get_finished_spans():
        parts.append(repr(dict(finished.attributes or {})))
        parts.append(finished.status.description or "")
        parts.extend(repr(dict(event.attributes or {})) for event in finished.events)
        parts.extend(repr(dict(link.attributes or {})) for link in finished.links)
    return "\n".join(parts)


def test_a_parameter_value_never_appears(
    client: TestClient, model: str, spans: InMemorySpanExporter
) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"label": SENTINEL}})
    if response.status_code == 202:
        wait_for_job(client, response.json()["job_id"])
        wait_for_span(spans, lambda s: s.name == "RunWorkflow:TemplatePipeline")
    assert SENTINEL not in _everything(spans)


def test_an_invalid_parameter_value_never_appears(
    client: TestClient, model: str, spans: InMemorySpanExporter
) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": SENTINEL}})
    assert response.status_code == 422
    assert SENTINEL not in _everything(spans)


def test_a_failed_renders_log_never_appears(
    client: TestClient, model: str, settings: Settings, spans: InMemorySpanExporter
) -> None:
    set_fake_env(Path(settings.openscad).parent, "FAKE_STDERR", [f"ERROR: {SENTINEL}"])
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 999}})
    assert wait_for_job(client, response.json()["job_id"])["status"] == "failed"
    wait_for_span(spans, lambda s: s.name == "RunWorkflow:TemplatePipeline")
    assert SENTINEL not in _everything(spans)
    # The schema export (format=param) also gets a span and succeeds; the failing one is the render.
    classes = [
        (s.attributes or {}).get("scadbuddy.failure_class")
        for s in spans.get_finished_spans()
        if s.name == "openscad.export"
    ]
    assert "OpenSCADError" in classes


def test_an_unhandled_exception_message_never_appears(
    app: FastAPI, spans: InMemorySpanExporter
) -> None:
    async def explode() -> None:
        raise RuntimeError(SENTINEL)

    app.router.add_api_route("/api/v1/_explode", explode)
    # Ahead of the SPA fallback, if a frontend bundle is mounted.
    app.router.routes.insert(0, app.router.routes.pop())
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/v1/_explode").status_code == 500
    assert SENTINEL not in _everything(spans)


def test_the_bambuddy_api_key_never_appears(
    settings: Settings, spans: InMemorySpanExporter
) -> None:
    keyed = settings.model_copy(
        update={"bambuddy_api_key": SENTINEL, "bambuddy_url": "http://127.0.0.1:9"}
    )
    with TestClient(create_app(keyed)) as client:
        client.get("/api/v1/settings/bambuddy")
    assert SENTINEL not in _everything(spans)
