"""Bambuddy calls are traced on our side only (spec 2026-10-01 §4)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

import httpx
import pytest
import respx
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL

SENTINEL = "detail-s3ntinel"


@respx.mock
async def test_a_call_is_a_client_span_and_sends_no_trace_headers(
    bambuddy: Any, spans: InMemorySpanExporter
) -> None:
    route = respx.get(f"{BASE_URL}/api/v1/printers/").mock(
        return_value=httpx.Response(200, json=[])
    )
    with trace.get_tracer("t").start_as_current_span("request"):
        await bambuddy.printers()
    sent = route.calls.last.request
    assert "traceparent" not in sent.headers
    assert "baggage" not in sent.headers
    (call,) = [s for s in spans.get_finished_spans() if s.name == "bambuddy.GET"]
    assert call.kind is SpanKind.CLIENT
    attributes = call.attributes or {}
    assert attributes["http.response.status_code"] == 200
    assert attributes["scadbuddy.bambuddy.scope"] == "Read Status"
    assert "s3cret" not in repr(attributes)


@respx.mock
async def test_a_refusal_records_its_class_and_never_bambuddys_detail(
    bambuddy: Any, spans: InMemorySpanExporter
) -> None:
    respx.get(f"{BASE_URL}/api/v1/printers/").mock(
        return_value=httpx.Response(409, json={"detail": SENTINEL})
    )
    with trace.get_tracer("t").start_as_current_span("request"), pytest.raises(ApiError):
        await bambuddy.printers()
    finished = spans.get_finished_spans()
    everything = repr(
        [(s.attributes, s.status.description, [e.attributes for e in s.events]) for s in finished]
    )
    assert SENTINEL not in everything
    (call,) = [s for s in finished if s.name == "bambuddy.GET"]
    assert (call.attributes or {})["http.response.status_code"] == 409
    assert (call.attributes or {})["scadbuddy.failure_class"] == (
        "https://scadbuddy.dev/problems/bambuddy-conflict"
    )


@respx.mock
async def test_a_stream_closed_from_another_task_is_a_client_span_and_detaches_cleanly(
    bambuddy: Any, spans: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(f"{BASE_URL}/api/v1/archives/1/video").mock(
        return_value=httpx.Response(200, content=b"bytes")
    )
    stack = AsyncExitStack()

    async def enter() -> None:
        await stack.enter_async_context(bambuddy.stream("/archives/1/video", what="read it"))

    with caplog.at_level(logging.WARNING, logger="opentelemetry.context"):
        with trace.get_tracer("t").start_as_current_span("request"):
            await asyncio.create_task(enter())
        await asyncio.create_task(stack.aclose())
    assert "Failed to detach context" not in caplog.text
    (call,) = [s for s in spans.get_finished_spans() if s.name == "bambuddy.GET"]
    assert call.kind is SpanKind.CLIENT
    assert (call.attributes or {})["http.response.status_code"] == 200


@respx.mock
async def test_a_transport_error_records_its_class_and_error_status(
    bambuddy: Any, spans: InMemorySpanExporter
) -> None:
    respx.get(f"{BASE_URL}/api/v1/printers/").mock(side_effect=httpx.ConnectError("no route"))
    with trace.get_tracer("t").start_as_current_span("request"), pytest.raises(ApiError):
        await bambuddy.printers()
    (call,) = [s for s in spans.get_finished_spans() if s.name == "bambuddy.GET"]
    assert "scadbuddy.failure_class" in (call.attributes or {})
    assert call.status.status_code is trace.StatusCode.ERROR
