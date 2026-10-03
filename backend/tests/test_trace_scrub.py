"""The scrub in front of every exporter (spec 2026-10-01 §6): no exception message,
in any form, leaves the process."""

from __future__ import annotations

import traceback
from typing import cast

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from scadbuddy.core.problems import ApiError
from scadbuddy.core.trace_scrub import ScrubbingSpanExporter, frames_only
from scadbuddy.render.runner import ParameterValueError

SENTINEL = "s3ntinel-9f1c"


def _exported(raise_it: bool = True, description: str | None = None) -> InMemorySpanExporter:
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(inner)))
    tracer = provider.get_tracer("t")
    try:
        with tracer.start_as_current_span("work") as current:
            if description is not None:
                current.set_status(Status(StatusCode.ERROR, description))
            if raise_it:
                try:
                    raise ParameterValueError("name", f"got {SENTINEL!r}")
                except ParameterValueError as cause:
                    raise ApiError(422, f"bad value {SENTINEL}") from cause
    except ApiError:
        pass
    return inner


def _everything(exported: InMemorySpanExporter) -> str:
    parts: list[str] = []
    for span in exported.get_finished_spans():
        parts.append(repr(dict(span.attributes or {})))
        parts.append(span.status.description or "")
        for event in span.events:
            parts.append(repr(dict(event.attributes or {})))
    return "\n".join(parts)


def test_a_chained_exception_leaves_no_message_anywhere() -> None:
    exported = _exported()
    assert SENTINEL not in _everything(exported)
    (span,) = exported.get_finished_spans()
    (event,) = span.events
    assert event.name == "exception"
    assert event.attributes is not None
    exc_type = cast(str, event.attributes["exception.type"])
    assert exc_type.endswith("ApiError")
    assert "exception.message" not in event.attributes
    assert 'File "' in str(event.attributes["exception.stacktrace"])


def test_a_status_description_becomes_the_exception_type() -> None:
    exported = _exported()
    (span,) = exported.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is not None
    assert SENTINEL not in span.status.description
    assert span.status.description.endswith("ApiError")


def test_a_status_description_without_an_exception_becomes_error() -> None:
    exported = _exported(raise_it=False, description=f"failed: {SENTINEL}")
    (span,) = exported.get_finished_spans()
    assert span.status.description == "error"


def test_frames_only_keeps_file_lines_and_drops_messages_and_code() -> None:
    try:
        try:
            raise ValueError(SENTINEL)
        except ValueError as cause:
            raise RuntimeError(SENTINEL) from cause
    except RuntimeError as error:
        formatted = "".join(traceback.format_exception(error))
    kept = frames_only(formatted)
    assert SENTINEL not in kept
    assert kept
    assert all(line.startswith('File "') for line in kept.splitlines())


def test_a_clean_span_passes_through_unchanged() -> None:
    exported = _exported(raise_it=False)
    (span,) = exported.get_finished_spans()
    assert span.name == "work"
    assert span.status.status_code is StatusCode.UNSET


def _exported_attributes(attributes: dict[str, str]) -> dict[str, object]:
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(inner)))
    with provider.get_tracer("t").start_as_current_span("work", attributes=attributes):
        pass
    (span,) = inner.get_finished_spans()
    return dict(span.attributes or {})


def test_no_query_string_or_user_agent_survives() -> None:
    kept = _exported_attributes(
        {
            "http.url": f"http://h/api/v1/x?q={SENTINEL}",
            "url.full": f"http://h/api/v1/x?q={SENTINEL}",
            "http.target": f"/api/v1/x?q={SENTINEL}",
            "url.query": f"q={SENTINEL}",
            "http.user_agent": SENTINEL,
            "user_agent.original": SENTINEL,
            "http.route": "/api/v1/x",
        }
    )
    assert SENTINEL not in repr(kept)
    assert kept["http.url"] == "http://h/api/v1/x"
    assert kept["url.full"] == "http://h/api/v1/x"
    assert kept["http.target"] == "/api/v1/x"
    assert kept["http.route"] == "/api/v1/x"


def test_a_frame_line_with_anything_but_a_function_name_does_not_survive() -> None:
    text = (
        "Traceback (most recent call last):\n"
        '  File "/ok.py", line 3, in fine_name\n'
        f'  File "/x", line 1, in {SENTINEL}-detail with spaces\n'
        "ValueError: boom"
    )
    kept = frames_only(text)
    assert SENTINEL not in kept
    assert 'File "/ok.py", line 3, in fine_name' in kept
