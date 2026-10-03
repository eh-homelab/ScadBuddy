"""The scrub every exported span passes through (spec 2026-10-01 §6).

Exception messages carry what must never be recorded: `ParameterValueError` puts the
raw value in its message (`got {value!r}`), `map_response` puts Bambuddy's own
`detail` in an `ApiError`'s. The FastAPI instrumentation and Temporal's
`TracingInterceptor` record exceptions and status descriptions on their own, so the
rule is enforced here, once, rather than at each call site."""

from __future__ import annotations

import importlib.util
import linecache
import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Final

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status

if TYPE_CHECKING:
    # tracing imports this module at run time; the alias is needed only by mypy.
    from scadbuddy.core.tracing import AttributeValue

#: An exception group's margin (``  | ``, ``  + ``) in front of each of its lines.
_MARGIN: Final = re.compile(r"^ *[|+] ?")
_TRACEBACK: Final = re.compile(r"^(?:Exception Group )?Traceback \(most recent call last\):$")
_FRAME: Final = re.compile(r'^ {2}File "(?P<path>[^"\n]*)", line (?P<line>\d+), in (?P<name>.*)$')
_PSEUDO_FILE: Final = re.compile(r"^<(?:frozen (?P<frozen>[\w.]+)|string|stdin)>$")
_NAME: Final = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
#: Code objects that are not named by a ``def`` or ``class``.
_ANONYMOUS: Final = frozenset(
    {"<module>", "<lambda>", "<genexpr>", "<listcomp>", "<dictcomp>", "<setcomp>"}
)
_DEFINITION: Final = re.compile(r"\b(?:def|class)\s+([A-Za-z_]\w*)")
#: Attributes the HTTP instrumentation fills from the request's own text: a query
#: string can carry anything a user typed, a user agent is a header value.
_DROPPED: Final = frozenset({"url.query", "http.user_agent", "user_agent.original"})
_CUT_AT_QUERY: Final = frozenset({"http.url", "url.full", "http.target"})


@lru_cache(maxsize=256)
def _defined_names(path: str) -> frozenset[str]:
    """Every name a ``def``, ``async def`` or ``class`` in ``path`` binds."""
    return frozenset(
        match.group(1) for line in linecache.getlines(path) for match in _DEFINITION.finditer(line)
    )


@lru_cache(maxsize=256)
def _is_stdlib_module(name: str) -> bool:
    """``<frozen X>`` names a standard-library module, never anything else."""
    if not _NAME.match(name) or name.split(".", 1)[0] not in sys.stdlib_module_names:
        return False
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _frame(path: str, line: str, name: str) -> str | None:
    if pseudo := _PSEUDO_FILE.match(path):
        frozen = pseudo.group("frozen")
        if frozen is not None and not _is_stdlib_module(frozen):
            return None
        # No source to check the name against: keep where, never what.
        return f'File "{path}", line {line}'
    if not os.path.isfile(path) or not linecache.getline(path, int(line)):
        return None
    if name in _ANONYMOUS or (
        _NAME.match(name) and name.rsplit(".", 1)[-1] in _defined_names(path)
    ):
        return f'File "{path}", line {line}, in {name}'
    return None


def _frames(stacktrace: str) -> Iterator[str]:
    in_traceback = False
    for line in stacktrace.splitlines():
        inner = _MARGIN.sub("", line, count=1)
        if _TRACEBACK.match(inner):
            in_traceback = True
        elif not inner.startswith(" "):
            # The exception line, and every line of its message after it, until the
            # next traceback header: message text, whatever it looks like.
            in_traceback = False
        elif in_traceback and (frame := _FRAME.match(inner)):
            kept = _frame(frame.group("path"), frame.group("line"), frame.group("name"))
            if kept is not None:
                yield kept


def frames_only(stacktrace: str) -> str:
    """The ``File "…", line N, in f`` lines of a formatted traceback, and nothing else:
    a traceback ends with, and for a chained exception repeats, the messages, and its
    code lines are source text. A message can hold newlines (Bambuddy's ``detail``
    does), so a line counts as a frame only inside a traceback block, before that
    block's exception line, and only when it names a file that exists. A message can
    also hold a traceback header of its own, so the frame's text is checked too: its
    function name must be ``<module>``, a lambda or comprehension, or a name the file
    itself defines with ``def``/``class`` (a dotted name by its last segment). So
    every exported frame is a real path, one of its line numbers, and an identifier
    taken from that file's own source, never message text. A pseudo-file
    (``<frozen …>`` naming a standard-library module, ``<string>``, ``<stdin>``) has no
    source to check against and keeps only its path and line."""
    return "\n".join(_frames(stacktrace))


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
