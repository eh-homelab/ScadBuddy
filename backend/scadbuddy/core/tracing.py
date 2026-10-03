"""OpenTelemetry tracing for the API and the render worker (spec 2026-10-01 §3, §4, §6).

Only the standard ``OTEL_*`` variables configure it. With no
``OTEL_EXPORTER_OTLP_ENDPOINT`` the provider is installed with no exporter: spans are
created, so context still propagates, and dropped. ``OTEL_SDK_DISABLED=true`` installs
nothing at all."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Final

from opentelemetry import context, propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes

from scadbuddy.core.problems import ApiError
from scadbuddy.core.trace_scrub import ScrubbingSpanExporter

TRACER_NAME: Final = "scadbuddy"
_PROPAGATOR: Final = TraceContextTextMapPropagator()

# opentelemetry.util.types.AttributeValue is a chained assignment, which mypy rejects as a type.
type AttributeValue = (
    str | bool | int | float | Sequence[str] | Sequence[bool] | Sequence[int] | Sequence[float]
)


class NoParentlessClients(Sampler):
    """The root sampler: a ``CLIENT`` span with no parent is dropped (spec §6). psycopg
    makes one per query, and the background loops (the reconciler's poll, the event
    bus, the pool) have no parent span; each would otherwise be a trace of its own."""

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: trace.TraceState | None = None,
    ) -> SamplingResult:
        if kind is SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)

    def get_description(self) -> str:
        return "NoParentlessClients"


DEFAULT_SAMPLER: Final[Sampler] = ParentBased(root=NoParentlessClients())


def tracing_disabled() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"


def build_provider(
    service_name: str,
    *,
    version: str,
    revision: str,
    inprocess_worker: bool = False,
    exporter: SpanExporter | None = None,
) -> TracerProvider:
    """The process's provider. ``exporter`` is for tests; otherwise the OTLP/HTTP one,
    and only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. Whatever exports is behind
    `ScrubbingSpanExporter`."""
    attributes: dict[str, AttributeValue] = {
        "service.name": service_name,
        "service.version": version,
        "service.instance.id": socket.gethostname(),
        "scadbuddy.revision": revision,
    }
    if inprocess_worker:
        attributes["scadbuddy.worker.inprocess"] = True
    # Resource.create merges OTEL_RESOURCE_ATTRIBUTES; the attributes given here win.
    resource = Resource.create(attributes)
    sampler = None if os.environ.get("OTEL_TRACES_SAMPLER") else DEFAULT_SAMPLER
    provider = TracerProvider(resource=resource, sampler=sampler)
    if exporter is None and os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(ScrubbingSpanExporter(exporter)))
    return provider


def _instrument_libraries() -> None:
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    instrumentor = PsycopgInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(enable_commenter=False)


def configure_tracing(
    service_name: str, *, version: str, revision: str, inprocess_worker: bool = False
) -> None:
    """Once per process, before anything that traces is built. A provider already set
    (the tests' own, or an earlier `create_app` in the same process) is kept."""
    propagate.set_global_textmap(_PROPAGATOR)
    if tracing_disabled():
        return
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(
            build_provider(
                service_name,
                version=version,
                revision=revision,
                inprocess_worker=inprocess_worker,
            )
        )
    _instrument_libraries()


def failure_class(error: BaseException) -> str:
    """What a span says failed (spec §6): never the message, which carries values."""
    if isinstance(error, ApiError):
        return f"http-{error.status}" if error.type == "about:blank" else error.type
    return type(error).__name__


def _record_failure(current: Span, error: Exception) -> None:
    current.set_attribute("scadbuddy.failure_class", failure_class(error))


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, AttributeValue] | None = None,
    links: Sequence[Link] = (),
) -> Iterator[Span]:
    """A span of our own: on an exception it adds `scadbuddy.failure_class`; the SDK
    records the exception and sets ERROR, and the scrubbing exporter drops its message."""
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(
        name, kind=kind, attributes=attributes, links=links
    ) as current:
        try:
            yield current
        except Exception as error:
            _record_failure(current, error)
            raise


@contextmanager
def detached_span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> Iterator[Span]:
    """Like `span`, but the span is never made current: its parent is the current span
    at entry and nothing else sees it as current. That makes it safe to exit from another
    task or context than the one that entered it, which a span held across the
    ``yield`` of an async context manager (a streamed response closed by the response
    body) needs: detaching a context token in a different context fails."""
    tracer = trace.get_tracer(TRACER_NAME)
    current = tracer.start_span(name, kind=kind, attributes=attributes)
    try:
        yield current
    except Exception as error:
        _record_failure(current, error)
        current.record_exception(error)
        current.set_status(Status(StatusCode.ERROR))
        raise
    finally:
        current.end()


def current_traceparent() -> str | None:
    """The current span's ``traceparent``, when it is valid and sampled (spec §4)."""
    span_context = trace.get_current_span().get_span_context()
    if not (span_context.is_valid and span_context.trace_flags.sampled):
        return None
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get("traceparent")


def _context_from(traceparent: str | None) -> Context | None:
    if not traceparent:
        return None
    extracted = _PROPAGATOR.extract({"traceparent": traceparent})
    if not trace.get_current_span(extracted).get_span_context().is_valid:
        return None
    return extracted


def link_to(traceparent: str | None) -> Link | None:
    extracted = _context_from(traceparent)
    if extracted is None:
        return None
    return Link(trace.get_current_span(extracted).get_span_context())


@contextmanager
def use_traceparent(traceparent: str | None) -> Iterator[None]:
    """Run the block as a child of ``traceparent``, or unchanged without a valid one."""
    extracted = _context_from(traceparent)
    if extracted is None:
        yield
        return
    token = context.attach(extracted)
    try:
        yield
    finally:
        context.detach(token)


__all__ = [
    "DEFAULT_SAMPLER",
    "TRACER_NAME",
    "build_provider",
    "configure_tracing",
    "current_traceparent",
    "detached_span",
    "failure_class",
    "link_to",
    "span",
    "tracing_disabled",
    "use_traceparent",
]
