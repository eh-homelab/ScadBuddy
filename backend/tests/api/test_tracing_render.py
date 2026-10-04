"""One render is one trace (spec 2026-10-01 §4)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from tests.api.conftest import wait_for_job
from tests.conftest import wait_for_span


def test_a_render_is_one_trace_from_request_to_activities(
    client: TestClient, model: str, spans: InMemorySpanExporter
) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert response.status_code == 202
    assert wait_for_job(client, response.json()["job_id"])["status"] == "done"

    request = wait_for_span(
        spans,
        lambda s: (
            s.kind is SpanKind.SERVER
            and str((s.attributes or {}).get("http.route", "")).endswith("/render")
        ),
    )
    # By trace id: a worker left over from an earlier test can end its own workflow
    # span after this test began.
    wait_for_span(
        spans,
        lambda s: (
            s.name == "RunWorkflow:TemplatePipeline"
            and s.context.trace_id == request.context.trace_id
        ),
    )
    in_trace = [
        s.name for s in spans.get_finished_spans() if s.context.trace_id == request.context.trace_id
    ]
    assert "StartWorkflow:TemplatePipeline" in in_trace
    assert "RunWorkflow:TemplatePipeline" in in_trace
    assert any(name.startswith("RunActivity:") for name in in_trace)
    assert "openscad.export" in in_trace
    assert "render.render" in in_trace
