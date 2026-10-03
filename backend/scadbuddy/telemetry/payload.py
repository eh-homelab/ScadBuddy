"""Browser spans made safe to forward (spec 2026-10-01 §5.2, §6).

The relay's input is untrusted: any script on the page writes it. So nothing passes
through by default. The resource is rebuilt with ``service.name`` forced to
``scadbuddy-web``; each span is rebuilt from the OTLP/JSON fields it may carry; every
list and string is capped at the limits the browser SDK's provider is configured with
(the frontend ``RelayExporter``'s ``spanLimits``), so a well-behaved page never meets
them; and the backend's own scrub (`core/trace_scrub.py`) is applied: no exception
message, no query string, no user agent on a span.

What is dropped for a cap is counted in OTLP's own field for its kind, as the SDK's
limits count it: ``droppedAttributesCount`` on the span, an event or a link,
``droppedEventsCount``, ``droppedLinksCount``. A string or an array cut to its cap is
not counted, as the SDK does not count one.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from scadbuddy.core.trace_scrub import CUT_AT_QUERY, DROPPED_ATTRIBUTES

MAX_SPANS: Final = 512
MAX_NAME_CHARS: Final = 128
MAX_ATTRIBUTES: Final = 64
MAX_STRING_CHARS: Final = 1024
MAX_ARRAY_ITEMS: Final = 32
MAX_EVENTS: Final = 16
MAX_EVENT_ATTRIBUTES: Final = 16
MAX_LINKS: Final = 8
#: The spec names no cap for a link's attributes; the event cap, which the frontend's
#: ``spanLimits`` sets as ``attributePerLinkCountLimit`` too.
MAX_LINK_ATTRIBUTES: Final = 16

WEB_SERVICE_NAME: Final = "scadbuddy-web"
#: Every other resource attribute the page sends is dropped.
KEPT_RESOURCE_ATTRIBUTES: Final = ("service.version", "user_agent.original")

#: Span and link fields forwarded as they came: ids, times, kind and flags. A wrong
#: value makes the collector refuse the batch, which the relay counts as ``failed``.
_SPAN_FIELDS: Final = (
    "traceId",
    "spanId",
    "parentSpanId",
    "traceState",
    "flags",
    "kind",
    "startTimeUnixNano",
    "endTimeUnixNano",
)
_LINK_FIELDS: Final = ("traceId", "spanId", "traceState", "flags")
#: OTLP/JSON writes a 64-bit integer as a decimal string.
_INT_STRING: Final = re.compile(r"^-?[0-9]{1,19}$")
#: A browser stack frame: V8's ``    at f (https://…/x.js:1:2)``, or Firefox and
#: Safari's ``f@https://…/x.js:1:2``. Every other line of a stack is the message.
_BROWSER_FRAME: Final = re.compile(r"^(?:\s+at \S.*:\d+:\d+\)?|[^\s@]*@\S+:\d+:\d+)$")

type Json = dict[str, Any]


class PayloadError(ValueError):
    """The body is not an OTLP/JSON trace export."""


class TooManySpansError(ValueError):
    """The batch holds more than :data:`MAX_SPANS` spans."""


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise PayloadError
    return value


def parse(body: bytes) -> Json:
    """The export's JSON, checked for the shape `rewrite` walks, and its span count."""
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        # ValueError covers bad JSON and bytes that are not UTF-8; RecursionError, a
        # body nested deeper than the decoder recurses.
        raise PayloadError from error
    if not isinstance(payload, dict):
        raise PayloadError
    count = 0
    for resource_spans in _list(payload.get("resourceSpans")):
        if not isinstance(resource_spans, dict):
            raise PayloadError
        for scope_spans in _list(resource_spans.get("scopeSpans", [])):
            if not isinstance(scope_spans, dict):
                raise PayloadError
            spans = _list(scope_spans.get("spans", []))
            if not all(isinstance(span, dict) for span in spans):
                raise PayloadError
            count += len(spans)
    if count > MAX_SPANS:
        raise TooManySpansError
    return payload


def browser_frames_only(stack: str) -> str:
    """The frame lines of a browser stack, and nothing else: the message is the rest."""
    return "\n".join(line.strip() for line in stack.splitlines() if _BROWSER_FRAME.match(line))


def _count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _scalar(value: object) -> Json | None:
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((kind, inner),) = value.items()
    if kind == "stringValue" and isinstance(inner, str):
        return {kind: inner[:MAX_STRING_CHARS]}
    if kind == "boolValue" and isinstance(inner, bool):
        return {kind: inner}
    if kind == "intValue" and (
        (isinstance(inner, int) and not isinstance(inner, bool))
        or (isinstance(inner, str) and _INT_STRING.match(inner))
    ):
        return {kind: inner}
    if kind == "doubleValue" and isinstance(inner, int | float) and not isinstance(inner, bool):
        return {kind: inner}
    return None


def _value(value: object) -> Json | None:
    """A scalar, or an array of at most :data:`MAX_ARRAY_ITEMS` scalars; anything else
    (a key-value list, bytes, a nested array) is not a value a page sends."""
    scalar = _scalar(value)
    if scalar is not None:
        return scalar
    if not isinstance(value, dict) or set(value) != {"arrayValue"}:
        return None
    array = value["arrayValue"]
    items = array.get("values", []) if isinstance(array, dict) else None
    if not isinstance(items, list):
        return None
    kept: list[Json] = []
    for item in items[:MAX_ARRAY_ITEMS]:
        inner = _scalar(item)
        if inner is None:
            return None
        kept.append(inner)
    return {"arrayValue": {"values": kept}}


def _attributes(raw: object, limit: int) -> tuple[list[Json], int]:
    """At most ``limit`` attributes, scrubbed, and how many were dropped for the cap or
    for a value no page sends. A scrubbed key is removed without being counted."""
    kept: list[Json] = []
    dropped = 0
    for item in raw if isinstance(raw, list) else []:
        key = item.get("key") if isinstance(item, dict) else None
        if not isinstance(key, str):
            dropped += 1
            continue
        if key in DROPPED_ATTRIBUTES:
            continue
        value = _value(item.get("value"))
        if value is None or len(kept) >= limit:
            dropped += 1
            continue
        if key in CUT_AT_QUERY and "stringValue" in value:
            value = {"stringValue": value["stringValue"].split("?", 1)[0]}
        kept.append({"key": key[:MAX_STRING_CHARS], "value": value})
    return kept, dropped


def _scrub_exception(raw: object) -> list[Any]:
    """An ``exception`` event's attributes without the message, and with only the frame
    lines of the stack (spec §6)."""
    scrubbed: list[Any] = []
    for item in raw if isinstance(raw, list) else []:
        key = item.get("key") if isinstance(item, dict) else None
        if key == "exception.message":
            continue
        if key == "exception.stacktrace":
            value = item.get("value")
            stack = value.get("stringValue") if isinstance(value, dict) else None
            if not isinstance(stack, str):
                continue
            item = {"key": key, "value": {"stringValue": browser_frames_only(stack)}}
        scrubbed.append(item)
    return scrubbed


def _name(value: object) -> str:
    return value[:MAX_NAME_CHARS] if isinstance(value, str) else ""


def _event(raw: Json) -> Json:
    name = _name(raw.get("name"))
    attributes = raw.get("attributes")
    if name == "exception":
        attributes = _scrub_exception(attributes)
    kept, dropped = _attributes(attributes, MAX_EVENT_ATTRIBUTES)
    event: Json = {"name": name, "attributes": kept}
    if "timeUnixNano" in raw:
        event["timeUnixNano"] = raw["timeUnixNano"]
    event["droppedAttributesCount"] = _count(raw.get("droppedAttributesCount")) + dropped
    return event


def _link(raw: Json) -> Json:
    kept, dropped = _attributes(raw.get("attributes"), MAX_LINK_ATTRIBUTES)
    link: Json = {field: raw[field] for field in _LINK_FIELDS if field in raw}
    link["attributes"] = kept
    link["droppedAttributesCount"] = _count(raw.get("droppedAttributesCount")) + dropped
    return link


def _capped(raw: object, limit: int) -> tuple[list[Json], int]:
    """The first ``limit`` objects of a list, and how many entries were left out."""
    items = raw if isinstance(raw, list) else []
    kept = [item for item in items if isinstance(item, dict)][:limit]
    return kept, len(items) - len(kept)


def _exception_type(events: list[Json]) -> str | None:
    for event in events:
        if event["name"] != "exception":
            continue
        for attribute in event["attributes"]:
            if attribute["key"] == "exception.type" and "stringValue" in attribute["value"]:
                return str(attribute["value"]["stringValue"])
    return None


def _status(raw: object, events: list[Json]) -> Json:
    """The status code as sent; a description, which carries the message, becomes the
    exception's type or ``error``, as `core/trace_scrub.py` does."""
    if not isinstance(raw, dict):
        return {}
    status: Json = {}
    code = raw.get("code")
    if isinstance(code, int | str) and not isinstance(code, bool):
        status["code"] = code
    message = raw.get("message")
    if isinstance(message, str) and message:
        status["message"] = _exception_type(events) or "error"
    return status


def _span(raw: Json) -> Json:
    span: Json = {field: raw[field] for field in _SPAN_FIELDS if field in raw}
    span["name"] = _name(raw.get("name"))
    attributes, dropped = _attributes(raw.get("attributes"), MAX_ATTRIBUTES)
    span["attributes"] = attributes
    span["droppedAttributesCount"] = _count(raw.get("droppedAttributesCount")) + dropped
    events, dropped_events = _capped(raw.get("events"), MAX_EVENTS)
    span["events"] = [_event(event) for event in events]
    span["droppedEventsCount"] = _count(raw.get("droppedEventsCount")) + dropped_events
    links, dropped_links = _capped(raw.get("links"), MAX_LINKS)
    span["links"] = [_link(link) for link in links]
    span["droppedLinksCount"] = _count(raw.get("droppedLinksCount")) + dropped_links
    span["status"] = _status(raw.get("status"), span["events"])
    return span


def _resource(raw: object) -> Json:
    kept: list[Json] = [{"key": "service.name", "value": {"stringValue": WEB_SERVICE_NAME}}]
    seen: set[str] = set()
    attributes = raw.get("attributes") if isinstance(raw, dict) else None
    for item in attributes if isinstance(attributes, list) else []:
        key = item.get("key") if isinstance(item, dict) else None
        if key not in KEPT_RESOURCE_ATTRIBUTES or key in seen:
            continue
        value = _scalar(item.get("value"))
        if value is not None and "stringValue" in value:
            seen.add(key)
            kept.append({"key": key, "value": value})
    return {"attributes": kept}


def _scope(raw: object) -> Json:
    if not isinstance(raw, dict):
        return {}
    return {
        field: raw[field][:MAX_NAME_CHARS]
        for field in ("name", "version")
        if isinstance(raw.get(field), str)
    }


def rewrite(payload: Json) -> Json:
    """The export rebuilt from what `parse` accepted: only the fields listed here survive."""
    return {
        "resourceSpans": [
            {
                "resource": _resource(resource_spans.get("resource")),
                "scopeSpans": [
                    {
                        "scope": _scope(scope_spans.get("scope")),
                        "spans": [_span(span) for span in scope_spans.get("spans", [])],
                    }
                    for scope_spans in resource_spans.get("scopeSpans", [])
                ],
            }
            for resource_spans in payload["resourceSpans"]
        ]
    }


def prepare(body: bytes) -> bytes:
    """The body as the relay forwards it. ASCII JSON, so a lone surrogate the page
    escaped stays an escape rather than failing to encode."""
    return json.dumps(rewrite(parse(body)), separators=(",", ":")).encode("ascii")


__all__ = [
    "KEPT_RESOURCE_ATTRIBUTES",
    "MAX_ARRAY_ITEMS",
    "MAX_ATTRIBUTES",
    "MAX_EVENTS",
    "MAX_EVENT_ATTRIBUTES",
    "MAX_LINKS",
    "MAX_LINK_ATTRIBUTES",
    "MAX_NAME_CHARS",
    "MAX_SPANS",
    "MAX_STRING_CHARS",
    "WEB_SERVICE_NAME",
    "PayloadError",
    "TooManySpansError",
    "browser_frames_only",
    "parse",
    "prepare",
    "rewrite",
]
