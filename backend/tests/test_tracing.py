"""core/tracing.py: the provider, the sampler and the context helpers (spec 2026-10-01 §3, §6)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span, SpanKind

from scadbuddy.core import tracing
from scadbuddy.core.problems import ApiError

_OTEL_VARS = (
    "OTEL_TRACES_SAMPLER",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_TRACES_EXPORTER",
    "OTEL_SDK_DISABLED",
    "OTEL_RESOURCE_ATTRIBUTES",
)


def _clear_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _OTEL_VARS:
        monkeypatch.delenv(name, raising=False)


def _processors(provider: TracerProvider) -> int:
    return len(provider._active_span_processor._span_processors)


def _provider(
    monkeypatch: pytest.MonkeyPatch, **env: str
) -> tuple[TracerProvider, InMemorySpanExporter]:
    _clear_otel(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    exported = InMemorySpanExporter()
    provider = tracing.build_provider("scadbuddy-api", version="1.2.3", revision="abc123")
    provider.add_span_processor(SimpleSpanProcessor(exported))
    return provider, exported


def test_the_resource_names_the_service_build_and_host(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch, OTEL_RESOURCE_ATTRIBUTES="deployment.environment=prod")
    attributes = provider.resource.attributes
    assert attributes["service.name"] == "scadbuddy-api"
    assert attributes["service.version"] == "1.2.3"
    assert attributes["scadbuddy.revision"] == "abc123"
    assert attributes["service.instance.id"]
    assert attributes["deployment.environment"] == "prod"
    assert "scadbuddy.worker.inprocess" not in attributes


def test_the_inprocess_worker_is_marked_on_the_api_resource() -> None:
    provider = tracing.build_provider(
        "scadbuddy-api", version="v", revision="r", inprocess_worker=True
    )
    assert provider.resource.attributes["scadbuddy.worker.inprocess"] is True


def test_a_parentless_client_span_is_dropped_and_a_child_one_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, exported = _provider(monkeypatch)
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT):
        pass
    with (
        tracer.start_as_current_span("request", kind=SpanKind.SERVER),
        tracer.start_as_current_span("SELECT", kind=SpanKind.CLIENT),
    ):
        pass
    names = [(s.name, s.kind) for s in exported.get_finished_spans()]
    assert names == [("SELECT", SpanKind.CLIENT), ("request", SpanKind.SERVER)]


def test_otel_traces_sampler_replaces_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, exported = _provider(monkeypatch, OTEL_TRACES_SAMPLER="always_on")
    with provider.get_tracer("t").start_as_current_span("SELECT", kind=SpanKind.CLIENT):
        pass
    assert [s.name for s in exported.get_finished_spans()] == ["SELECT"]


def test_no_endpoint_means_no_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch)
    # Only the test's own processor: build_provider added none.
    assert len(provider._active_span_processor._span_processors) == 1


def test_disabled_is_read_from_otel_sdk_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "TRUE")
    assert tracing.tracing_disabled()
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    assert not tracing.tracing_disabled()


def test_the_traceparent_round_trips_into_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(monkeypatch)
    with provider.get_tracer("t").start_as_current_span("submit") as current:
        traceparent = tracing.current_traceparent()
    assert traceparent is not None
    link = tracing.link_to(traceparent)
    assert link is not None
    assert link.context.trace_id == current.get_span_context().trace_id
    assert link.context.span_id == current.get_span_context().span_id


def test_no_span_no_traceparent_and_garbage_no_link() -> None:
    assert tracing.current_traceparent() is None
    assert tracing.link_to(None) is None
    assert tracing.link_to("not-a-traceparent") is None


def test_use_traceparent_parents_the_next_span(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, exported = _provider(monkeypatch)
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("first") as first:
        traceparent = tracing.current_traceparent()
    with tracing.use_traceparent(traceparent), tracer.start_as_current_span("later"):
        pass
    later = next(s for s in exported.get_finished_spans() if s.name == "later")
    assert later.context.trace_id == first.get_span_context().trace_id


def test_an_unreachable_collector_raises_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review Focus 1: nothing listens on port 9 (discard); export errors stay in the SDK.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    provider = tracing.build_provider("scadbuddy-api", version="v", revision="r")
    with provider.get_tracer("t").start_as_current_span("request"):
        pass
    provider.force_flush(timeout_millis=2000)
    provider.shutdown()


def test_failure_class_names_the_problem_or_the_class() -> None:
    assert tracing.failure_class(ApiError(409, "x")) == "http-409"
    assert (
        tracing.failure_class(ApiError(422, "x", type_="/problems/bad-param"))
        == "/problems/bad-param"
    )
    assert tracing.failure_class(ValueError("x")) == "ValueError"


def test_span_records_the_failure_class_and_reraises(spans: InMemorySpanExporter) -> None:
    # `spans` is the tests' global provider (tests/conftest.py, Task 3), which
    # `tracing.span` uses.
    with pytest.raises(ValueError), tracing.span("render.render"):
        raise ValueError("boom")
    (finished,) = spans.get_finished_spans()
    assert finished.attributes is not None
    assert finished.attributes["scadbuddy.failure_class"] == "ValueError"
    assert finished.status.status_code is trace.StatusCode.ERROR
    assert [event.name for event in finished.events] == ["exception"]


@pytest.mark.parametrize("opener", [tracing.span, tracing.detached_span])
def test_a_cancelled_span_is_not_an_error(
    spans: InMemorySpanExporter, opener: Callable[[str], AbstractContextManager[Span]]
) -> None:
    # Review 2 of #1064: a superseded render is cancelled, and its row settles
    # `cancelled`, not `failed` (spec §6), so its spans must not end in ERROR.
    with pytest.raises(asyncio.CancelledError), opener("render.render"):
        raise asyncio.CancelledError
    (finished,) = spans.get_finished_spans()
    assert finished.status.status_code is trace.StatusCode.UNSET
    assert [event.name for event in finished.events] == []
    assert "scadbuddy.failure_class" not in (finished.attributes or {})


def test_detached_span_is_never_current_and_records_the_failure(
    spans: InMemorySpanExporter,
) -> None:
    with trace.get_tracer("t").start_as_current_span("outer") as outer:
        with tracing.detached_span("inner") as inner:
            assert trace.get_current_span() is outer
        with pytest.raises(ValueError), tracing.detached_span("failing"):
            raise ValueError("boom")
    finished = {s.name: s for s in spans.get_finished_spans()}
    assert inner.get_span_context().trace_id == outer.get_span_context().trace_id
    assert finished["inner"].parent is not None
    assert finished["inner"].parent.span_id == outer.get_span_context().span_id
    failing = finished["failing"]
    assert (failing.attributes or {})["scadbuddy.failure_class"] == "ValueError"
    assert failing.status.status_code is trace.StatusCode.ERROR


def test_a_span_made_without_the_spans_fixture() -> None:
    with tracing.span("leftover"):
        pass


def test_is_gone_by_the_next_test(spans: InMemorySpanExporter) -> None:
    # Review of #1064: the session exporter is cleared after every test, not only
    # after tests that ask for `spans`, so it never grows across the session.
    assert [s.name for s in spans.get_finished_spans()] == []


def test_a_traces_only_endpoint_turns_export_on(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _provider(
        monkeypatch, OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://collector:4318/v1/traces"
    )
    assert _processors(provider) == 2


def test_otel_traces_exporter_none_exports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("none", "NONE"):
        provider, _ = _provider(
            monkeypatch,
            OTEL_EXPORTER_OTLP_ENDPOINT="http://collector:4318",
            OTEL_TRACES_EXPORTER=value,
        )
        assert _processors(provider) == 1
        assert tracing.otlp_traces_target() is None


def test_target_is_none_without_an_endpoint_or_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_otel(monkeypatch)
    assert tracing.otlp_traces_target() is None
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    assert tracing.otlp_traces_target() is None
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert tracing.otlp_traces_target() is None


def test_target_appends_v1_traces_with_one_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4318")
    assert tracing.otlp_traces_target() == ("http://c:4318/v1/traces", {})
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4318/")
    assert tracing.otlp_traces_target() == ("http://c:4318/v1/traces", {})


def test_the_traces_endpoint_wins_and_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://general:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://t:4318/custom/")
    assert tracing.otlp_traces_target() == ("http://t:4318/custom/", {})


def test_target_headers_prefer_the_traces_variable_and_are_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "general=1")
    target = tracing.otlp_traces_target()
    assert target is not None and target[1] == {"general": "1"}
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        " Authorization = Basic%20abc%3D%3D , bad, =nokey, x-k=a=b,%6Bey=v%2Cw",
    )
    target = tracing.otlp_traces_target()
    assert target is not None
    assert target[1] == {"Authorization": "Basic abc==", "x-k": "a=b", "key": "v,w"}
