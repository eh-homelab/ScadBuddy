"""Requests are traced; the infrastructure paths are not (spec 2026-10-01 §6)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from tests.conftest import wait_for_span

#: Every attribute a server span carries today (instrumentation 0.66b0, the default
#: semconv). The scrub (`core/trace_scrub.py`) is a denylist of exact names, so a
#: release that renames one (the query string moving to a new key) would export it
#: unscrubbed: this set makes that rename fail here instead, and the bump that
#: changes it is reviewed alongside `_DROPPED`, `_PATH_ONLY` and `_WITH_ORIGIN`
#: (review 2 of #1064).
KNOWN_SERVER_ATTRIBUTES = frozenset(
    {
        "http.flavor",
        "http.method",
        "http.route",
        "http.scheme",
        "http.status_code",
        "http.target",
        "net.host.port",
    }
)


def _server_spans(spans: InMemorySpanExporter) -> list[str]:
    return [
        str((s.attributes or {}).get("http.route"))
        for s in spans.get_finished_spans()
        if s.kind is SpanKind.SERVER
    ]


def test_an_api_request_is_a_server_span_named_by_its_route(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    assert client.get("/api/v1/models").status_code == 200
    server = wait_for_span(spans, lambda s: s.kind is SpanKind.SERVER)
    assert (server.attributes or {}).get("http.route") == "/api/v1/models"
    assert "?" not in server.name


def test_its_queries_are_children_of_the_request(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    # A job lookup reads render_jobs (projection.read) before answering 404.
    missing = "0123456789abcdef0123456789abcdef"
    assert client.get(f"/api/v1/jobs/{missing}").status_code == 404
    server = wait_for_span(spans, lambda s: s.kind is SpanKind.SERVER)
    queries = [
        s
        for s in spans.get_finished_spans()
        if s.kind is SpanKind.CLIENT and s.context.trace_id == server.context.trace_id
    ]
    assert queries, "the lookup's query is a child of the request"
    for query in queries:
        # Statement text only, never the bound values (sqlcommenter off, spec §6).
        assert missing not in repr(query.attributes)


def test_a_server_span_carries_only_known_attributes(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    missing = "0123456789abcdef0123456789abcdef"
    headers = {
        "User-Agent": "agent-s3ntinel",
        "X-Forwarded-For": "192.0.2.1",
        "Host": "host-s3ntinel.example",
    }
    assert client.get("/api/v1/models?q=s3ntinel", headers=headers).status_code == 200
    assert client.get(f"/api/v1/jobs/{missing}?q=s3ntinel", headers=headers).status_code == 404
    wait_for_span(
        spans,
        lambda s: s.kind is SpanKind.SERVER and (s.attributes or {}).get("http.status_code") == 404,
    )
    server = [s for s in spans.get_finished_spans() if s.kind is SpanKind.SERVER]
    assert len(server) == 2
    for finished in server:
        assert set(finished.attributes or {}) <= KNOWN_SERVER_ATTRIBUTES, finished.attributes
        assert "s3ntinel" not in repr(finished.attributes)


@pytest.mark.parametrize(
    ("path", "status"),
    # The relay route (spec §5.2) does not exist yet: its 404 is still not traced.
    [("/healthz", 200), ("/metrics", 200), ("/telemetry/v1/traces", 404)],
)
def test_the_infrastructure_paths_are_not_traced(
    client: TestClient, spans: InMemorySpanExporter, path: str, status: int
) -> None:
    assert client.get(path).status_code == status
    assert _server_spans(spans) == []


def test_a_path_that_only_contains_an_excluded_one_is_traced(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    assert client.get("/api/v1/models/metrics-x").status_code == 404
    wait_for_span(spans, lambda s: s.kind is SpanKind.SERVER)


def test_body_chunks_and_messages_are_not_spans(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    assert client.get("/api/v1/models").status_code == 200
    wait_for_span(spans, lambda s: s.kind is SpanKind.SERVER)
    names = [s.name for s in spans.get_finished_spans()]
    assert not [n for n in names if n.endswith((" http send", " http receive"))], names


def test_a_garbage_traceparent_starts_a_fresh_trace(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    response = client.get("/api/v1/models", headers={"traceparent": "00-garbage-xx-01"})
    assert response.status_code == 200
    server = wait_for_span(spans, lambda s: s.kind is SpanKind.SERVER)
    assert server.parent is None
