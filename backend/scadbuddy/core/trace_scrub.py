"""The scrub every exported span passes through (spec 2026-10-01 §6).

Exception messages carry what must never be recorded: `ParameterValueError` puts the
raw value in its message (`got {value!r}`), `map_response` puts Bambuddy's own
`detail` in an `ApiError`'s. The FastAPI instrumentation and Temporal's
`TracingInterceptor` record exceptions and status descriptions on their own, so the
rule is enforced here, once, rather than at each call site."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status
from opentelemetry.util.types import AttributeValue

_FRAME: Final = re.compile(r'^ {2}(?:\| +)*(File "[^"\n]*", line \d+, in [\w.<>]+)$')
#: Attributes the HTTP instrumentation fills from the request's own text: a query
#: string can carry anything a user typed, a user agent is a header value.
_DROPPED: Final = frozenset({"url.query", "http.user_agent", "user_agent.original"})
_CUT_AT_QUERY: Final = frozenset({"http.url", "url.full", "http.target"})


def frames_only(stacktrace: str) -> str:
    """The ``File "…", line N, in f`` lines of a formatted traceback, and nothing else:
    a traceback ends with, and for a chained exception repeats, the messages, and its
    code lines are source text."""
    kept = (match.group(1) for line in stacktrace.splitlines() if (match := _FRAME.match(line)))
    return "\n".join(kept)


def _scrub_event(event: Event) -> Event:
    if event.name != "exception" or not event.attributes:
        return event
    attributes = {
        key: value for key, value in event.attributes.items() if key != "exception.message"
    }
    stacktrace = attributes.get("exception.stacktrace")
    if isinstance(stacktrace, str):
        attributes["exception.stacktrace"] = frames_only(stacktrace)
    return Event(event.name, attributes, event.timestamp)


def _exception_type(events: Sequence[Event]) -> str | None:
    for event in events:
        if event.name == "exception" and event.attributes:
            value = event.attributes.get("exception.type")
            if isinstance(value, str):
                return value
    return None


def _scrub_attributes(attributes: Mapping[str, AttributeValue] | None) -> dict[str, AttributeValue]:
    kept: dict[str, AttributeValue] = {}
    for key, value in (attributes or {}).items():
        if key in _DROPPED:
            continue
        if key in _CUT_AT_QUERY and isinstance(value, str):
            value = value.split("?", 1)[0]
        kept[key] = value
    return kept


def scrub(span: ReadableSpan) -> ReadableSpan:
    events = [_scrub_event(event) for event in span.events]
    status = span.status
    if status.description:
        status = Status(status.status_code, _exception_type(events) or "error")
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_scrub_attributes(span.attributes),
        events=events,
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class ScrubbingSpanExporter(SpanExporter):
    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([scrub(span) for span in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


__all__ = ["ScrubbingSpanExporter", "frames_only", "scrub"]
