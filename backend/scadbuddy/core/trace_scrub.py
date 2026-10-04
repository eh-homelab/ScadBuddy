"""The scrub every exported span passes through (spec 2026-10-01 §6).

Exception messages carry what must never be recorded: `ParameterValueError` puts the
raw value in its message (`got {value!r}`), `map_response` puts Bambuddy's own
`detail` in an `ApiError`'s. The FastAPI instrumentation and Temporal's
`TracingInterceptor` record exceptions and status descriptions on their own, so the
rule is enforced here, once, rather than at each call site."""

from __future__ import annotations

import importlib.util
import os
import re
import site
import sys
import sysconfig
from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Final

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, SpanKind, Status

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
#: string can carry anything a user typed, a user agent and a host name are header
#: values (the Host header). The client's address is never recorded either; the
#: server's own port is kept.
_DROPPED: Final = frozenset(
    {
        "url.query",
        "http.user_agent",
        "user_agent.original",
        "http.host",
        "http.server_name",
        "server.address",
        "net.peer.ip",
        "net.peer.port",
        "client.address",
        "client.port",
        "net.sock.peer.addr",
        "net.sock.peer.port",
        "network.peer.address",
        "network.peer.port",
    }
)
#: Attributes that hold the request's path, which is data too: a file path a user
#: chose, a photo filename Bambuddy returned, whatever the SPA fallback was asked for.
#: The route's template stands in for it; with no route, the attribute is dropped.
_PATH_ONLY: Final = frozenset({"http.target", "url.path"})
#: An absolute URL keeps its origin, then the route, except on a server span: there the
#: host is the request's Host header, so the URL is not exported at all.
_WITH_ORIGIN: Final = frozenset({"http.url", "url.full"})
#: An absolute URL's ``scheme://host[:port]``, kept in front of the route.
_ORIGIN: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*")
#: Headers the instrumentation captures when a deployment sets
#: ``OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*``: cookies, credentials, anything.
_HEADER_PREFIXES: Final = ("http.request.header.", "http.response.header.")


#: A source file larger than this is not read: its frames are dropped.
_MAX_SOURCE_BYTES: Final = 2 * 1024 * 1024


def _code_roots() -> tuple[str, ...]:
    """The directories the interpreter's own code lives in: this package, the standard
    library and site-packages, each resolved and ending in a separator."""
    paths = sysconfig.get_paths()
    candidates = [
        os.path.dirname(os.path.dirname(__file__)),
        *(paths[key] for key in ("stdlib", "platstdlib", "purelib", "platlib") if key in paths),
        *site.getsitepackages(),
    ]
    return tuple(sorted({os.path.join(os.path.realpath(path), "") for path in candidates}))


#: A frame is checked against its file only under these, so a path in a message (a
#: data file, a blob) is never opened by the exporter.
_CODE_ROOTS: tuple[str, ...] = _code_roots()


@lru_cache(maxsize=256)
def _source(path: str) -> tuple[int, frozenset[str]] | None:
    """``path``'s line count and every name a ``def``, ``async def`` or ``class`` in it
    binds; ``None`` when it cannot be read or is larger than ``_MAX_SOURCE_BYTES``.
    Only these are kept, never the text, and never in ``linecache``."""
    try:
        with open(path, "rb") as file:
            data = file.read(_MAX_SOURCE_BYTES + 1)
    except OSError:
        return None
    if len(data) > _MAX_SOURCE_BYTES:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    names = frozenset(match.group(1) for line in lines for match in _DEFINITION.finditer(line))
    return len(lines), names


def _code_file(path: str) -> str | None:
    """``path`` resolved, when it is a regular file under one of ``_CODE_ROOTS``."""
    resolved = os.path.realpath(path)
    if not resolved.startswith(_CODE_ROOTS) or not os.path.isfile(resolved):
        return None
    return resolved


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
    resolved = _code_file(path)
    source = _source(resolved) if resolved is not None else None
    if source is None:
        return None
    count, names = source
    if not 1 <= int(line) <= count:
        return None
    if name in _ANONYMOUS or (_NAME.match(name) and names.issuperset(name.split("."))):
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
    block's exception line, and only when it names a file that exists under the
    interpreter's own code (this package, the standard library, site-packages): a path
    anywhere else is never opened. A message can
    also hold a traceback header of its own, so the frame's text is checked too: its
    function name must be ``<module>``, a lambda or comprehension, or a name the file
    itself defines with ``def``/``class`` (a dotted name, every segment). So
    every exported frame is a real path, one of its line numbers, and an identifier
    taken from that file's own source, never message text. A pseudo-file
    (``<frozen …>`` naming a standard-library module, ``<string>``, ``<stdin>``) has no
    source to check against and keeps only its path and line."""
    return "\n".join(_frames(stacktrace))


def _scrub_event(event: Event) -> Event:
    attributes = _scrub_attributes(event.attributes)
    if event.name == "exception":
        attributes.pop("exception.message", None)
        stacktrace = attributes.get("exception.stacktrace")
        if isinstance(stacktrace, str):
            attributes["exception.stacktrace"] = frames_only(stacktrace)
    return Event(event.name, attributes, event.timestamp)


def _scrub_link(link: Link) -> Link:
    return Link(link.context, _scrub_attributes(link.attributes))


def _exception_type(events: Sequence[Event]) -> str | None:
    for event in events:
        if event.name == "exception" and event.attributes:
            value = event.attributes.get("exception.type")
            if isinstance(value, str):
                return value
    return None


def _scrub_attributes(
    attributes: Mapping[str, AttributeValue] | None, *, server: bool = False
) -> dict[str, AttributeValue]:
    attributes = attributes or {}
    route = attributes.get("http.route")
    kept: dict[str, AttributeValue] = {}
    for key, value in attributes.items():
        if key in _DROPPED or key.startswith(_HEADER_PREFIXES):
            continue
        if server and key in _WITH_ORIGIN:
            continue
        if key in _PATH_ONLY or key in _WITH_ORIGIN:
            if not isinstance(route, str) or not isinstance(value, str):
                continue
            if key in _PATH_ONLY:
                value = route
            elif origin := _ORIGIN.match(value):
                value = origin.group(0) + route
            else:
                continue
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
        attributes=_scrub_attributes(span.attributes, server=span.kind is SpanKind.SERVER),
        events=events,
        links=[_scrub_link(link) for link in span.links],
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
