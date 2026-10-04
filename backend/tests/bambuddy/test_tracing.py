"""Bambuddy calls are traced on our side only (spec 2026-10-01 §4)."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import AsyncExitStack
from typing import Any, get_args

import httpx
import pytest
import respx
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from scadbuddy.bambuddy.client import Operation
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
    (call,) = [s for s in spans.get_finished_spans() if s.name == "bambuddy.printers.list"]
    assert call.kind is SpanKind.CLIENT
    attributes = call.attributes or {}
    assert attributes["http.request.method"] == "GET"
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
    (call,) = [s for s in finished if s.name == "bambuddy.printers.list"]
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
        await stack.enter_async_context(
            bambuddy.stream("/archives/1/video", operation="media.timelapse", what="read it")
        )

    with caplog.at_level(logging.WARNING, logger="opentelemetry.context"):
        with trace.get_tracer("t").start_as_current_span("request"):
            await asyncio.create_task(enter())
        await asyncio.create_task(stack.aclose())
    assert "Failed to detach context" not in caplog.text
    (call,) = [s for s in spans.get_finished_spans() if s.name == "bambuddy.media.timelapse"]
    assert call.kind is SpanKind.CLIENT
    assert (call.attributes or {})["http.response.status_code"] == 200


@respx.mock
async def test_a_transport_error_records_its_class_and_error_status(
    bambuddy: Any, spans: InMemorySpanExporter
) -> None:
    respx.get(f"{BASE_URL}/api/v1/printers/").mock(side_effect=httpx.ConnectError("no route"))
    with trace.get_tracer("t").start_as_current_span("request"), pytest.raises(ApiError):
        await bambuddy.printers()
    (call,) = [s for s in spans.get_finished_spans() if s.name == "bambuddy.printers.list"]
    assert "scadbuddy.failure_class" in (call.attributes or {})
    assert call.status.status_code is trace.StatusCode.ERROR


class _ConsumerError(Exception):
    pass


@respx.mock
async def test_the_consumers_error_inside_a_stream_does_not_fail_bambuddys_span(
    bambuddy: Any, spans: InMemorySpanExporter
) -> None:
    # Review 2 of #1064: Bambuddy answered 2xx; a failure writing the bytes on (a
    # browser gone away) is ours, not Bambuddy's.
    respx.get(f"{BASE_URL}/api/v1/archives/1/video").mock(
        return_value=httpx.Response(200, content=b"bytes")
    )
    with trace.get_tracer("t").start_as_current_span("request"), pytest.raises(_ConsumerError):
        async with bambuddy.stream(
            "/archives/1/video", operation="media.timelapse", what="read it"
        ):
            raise _ConsumerError
    (call,) = [s for s in spans.get_finished_spans() if s.name == "bambuddy.media.timelapse"]
    assert call.status.status_code is trace.StatusCode.UNSET
    assert "scadbuddy.failure_class" not in (call.attributes or {})
    assert [event.name for event in call.events] == []
    assert (call.attributes or {})["http.response.status_code"] == 200


@respx.mock
async def test_a_refused_stream_still_fails_its_span(
    bambuddy: Any, spans: InMemorySpanExporter
) -> None:
    respx.get(f"{BASE_URL}/api/v1/archives/1/video").mock(
        return_value=httpx.Response(404, json={"detail": "gone"})
    )
    with trace.get_tracer("t").start_as_current_span("request"), pytest.raises(ApiError):
        async with bambuddy.stream(
            "/archives/1/video", operation="media.timelapse", what="read it"
        ):
            pass
    (call,) = [s for s in spans.get_finished_spans() if s.name == "bambuddy.media.timelapse"]
    assert call.status.status_code is trace.StatusCode.ERROR
    assert "scadbuddy.failure_class" in (call.attributes or {})


@respx.mock
async def test_two_different_reads_are_two_different_span_names(
    bambuddy: Any, spans: InMemorySpanExporter
) -> None:
    # Review 5 of #1064: a span is named by what the call does (spec §4), so listing
    # printers and reading a slice job are not both `bambuddy.GET`.
    respx.get(f"{BASE_URL}/api/v1/printers/").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE_URL}/api/v1/slice-jobs/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "status": "running"})
    )
    with trace.get_tracer("t").start_as_current_span("request"):
        await bambuddy.printers()
        await bambuddy.slice_job(7)
    calls = [s for s in spans.get_finished_spans() if s.kind is SpanKind.CLIENT]
    assert [s.name for s in calls] == ["bambuddy.printers.list", "bambuddy.slice.job.status"]
    assert {(s.attributes or {})["http.request.method"] for s in calls} == {"GET"}


def test_the_operations_are_a_closed_set_of_plain_names() -> None:
    operations = get_args(Operation)
    assert len(operations) == len(set(operations)) > 0
    for operation in operations:
        assert re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)*", operation), operation
