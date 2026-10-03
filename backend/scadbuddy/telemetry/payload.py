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
import math
import re
from typing import Any, Final

from scadbuddy.core.trace_scrub import CUT_AT_QUERY, DROPPED_ATTRIBUTES

MAX_SPANS: Final = 512
#: Bound what an empty ``{}`` entry can be rewritten into: each resource and scope is
#: rebuilt with a resource and a scope object, so unbounded they amplify a body ~36x.
MAX_RESOURCES: Final = 16
MAX_SCOPES: Final = 64
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

_MAX_UINT32: Final = 2**32 - 1
_MAX_INT64: Final = 2**63 - 1
_MAX_TRACE_STATE_CHARS: Final = 512
_TRACE_ID: Final = re.compile(r"^[0-9a-fA-F]{32}$")
_SPAN_ID: Final = re.compile(r"^[0-9a-fA-F]{16}$")
_STATUS_CODES: Final = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}
#: OTLP/JSON writes a 64-bit integer as a decimal string.
_INT_STRING: Final = re.compile(r"^-?[0-9]{1,19}$")
#: A browser stack frame: V8's ``    at f (https://…/x.js:1:2)``, or Firefox and
#: Safari's ``f@https://…/x.js:1:2``. Every other line of a stack is the message.
_BROWSER_FRAME: Final = re.compile(r"^(?:\s+at \S.*:\d+:\d+\)?|[^\s@]*@\S+:\d+:\d+)$")

type Json = dict[str, Any]


def _id(value: object, pattern: re.Pattern[str]) -> str | None:
    """A trace or span id: hex of the right length, not all zeros (OTLP's invalid id)."""
    if isinstance(value, str) and pattern.fullmatch(value) and value.strip("0"):
        return value
    return None


def _uint(value: object, limit: int) -> int | str | None:
    """An integer in ``0..limit``, as a number or as OTLP/JSON's decimal string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= limit else None
    if isinstance(value, str) and _INT_STRING.fullmatch(value) and not value.startswith("-"):
        return value if int(value) <= limit else None
    return None


def _is_int64(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -(2**63) <= value <= _MAX_INT64
    if isinstance(value, str) and _INT_STRING.fullmatch(value):
        return -(2**63) <= int(value) <= _MAX_INT64
    return False


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _set_valid(target: Json, key: str, value: object) -> None:
    if value is not None:
        target[key] = value


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
    scopes = 0
    resources = _list(payload.get("resourceSpans"))
    if len(resources) > MAX_RESOURCES:
        raise PayloadError
    for resource_spans in resources:
        if not isinstance(resource_spans, dict):
            raise PayloadError
        scope_list = _list(resource_spans.get("scopeSpans", []))
        scopes += len(scope_list)
        if scopes > MAX_SCOPES:
            raise PayloadError
        for scope_spans in scope_list:
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
    return "\n".join(line.strip() for line in stack.splitlines() if _BROWSER_FRAME.fullmatch(line))


def _count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return min(value, _MAX_UINT32)
    return 0


def _total(raw: object, local: int) -> int:
    return min(_count(raw) + local, _MAX_UINT32)


def _scalar(value: object) -> Json | None:
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((kind, inner),) = value.items()
    if kind == "stringValue" and isinstance(inner, str):
        return {kind: inner[:MAX_STRING_CHARS]}
    if kind == "boolValue" and isinstance(inner, bool):
        return {kind: inner}
    if kind == "intValue" and _is_int64(inner):
        return {kind: inner}
    if kind == "doubleValue" and _is_finite_number(inner):
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


def _string_attribute(attributes: list[Any], name: str) -> str | None:
    for item in attributes:
        if isinstance(item, dict) and item.get("key") == name:
            value = item.get("value")
            inner = value.get("stringValue") if isinstance(value, dict) else None
            if isinstance(inner, str):
                return inner
    return None


def _without_message(stack: str, error_type: str | None, message: str | None) -> str:
    """The stack without the message at its head, where V8 writes ``<type>: <message>``
    (which can span lines). Firefox and Safari write no head, so nothing is removed; nor
    is the message anywhere else, where it can be text inside a frame."""
    if not message:
        return stack
    heads = [f"{error_type}: {message}"] if error_type else []
    colon = stack.find(": ")
    if colon != -1 and "\n" not in stack[:colon]:
        heads.append(stack[: colon + 2] + message)
    heads.append(message)
    for head in heads:
        if stack.startswith(head):
            return stack[len(head) :]
    return stack


def _scrub_exception(raw: object) -> list[Any]:
    """An ``exception`` event's attributes without the message, and with only the frame
    lines of the stack (spec §6)."""
    scrubbed: list[Any] = []
    attributes = raw if isinstance(raw, list) else []
    message = _string_attribute(attributes, "exception.message")
    error_type = _string_attribute(attributes, "exception.type")
    for item in attributes:
        key = item.get("key") if isinstance(item, dict) else None
        if key == "exception.message":
            continue
        if key == "exception.stacktrace":
            stack = _string_attribute([item], key)
            if stack is None:
                continue
            # A multi-line message can carry a line that looks like a frame.
            stack = _without_message(stack, error_type, message)
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
    _set_valid(event, "timeUnixNano", _uint(raw.get("timeUnixNano"), _MAX_INT64))
    event["droppedAttributesCount"] = _total(raw.get("droppedAttributesCount"), dropped)
    return event


def _trace_state(value: object) -> str | None:
    return value[:_MAX_TRACE_STATE_CHARS] if isinstance(value, str) else None


def _link(raw: Json) -> Json | None:
    trace_id = _id(raw.get("traceId"), _TRACE_ID)
    span_id = _id(raw.get("spanId"), _SPAN_ID)
    if trace_id is None or span_id is None:
        return None
    kept, dropped = _attributes(raw.get("attributes"), MAX_LINK_ATTRIBUTES)
    link: Json = {"traceId": trace_id, "spanId": span_id}
    _set_valid(link, "traceState", _trace_state(raw.get("traceState")))
    _set_valid(link, "flags", _uint(raw.get("flags"), _MAX_UINT32))
    link["attributes"] = kept
    link["droppedAttributesCount"] = _total(raw.get("droppedAttributesCount"), dropped)
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
    if isinstance(code, str):
        code = _STATUS_CODES.get(code)
    if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 2:
        status["code"] = code
    message = raw.get("message")
    if isinstance(message, str) and message:
        status["message"] = _exception_type(events) or "error"
    return status


def _span(raw: Json) -> Json | None:
    trace_id = _id(raw.get("traceId"), _TRACE_ID)
    span_id = _id(raw.get("spanId"), _SPAN_ID)
    if trace_id is None or span_id is None:
        return None
    span: Json = {"traceId": trace_id, "spanId": span_id}
    _set_valid(span, "parentSpanId", _id(raw.get("parentSpanId"), _SPAN_ID))
    _set_valid(span, "traceState", _trace_state(raw.get("traceState")))
    _set_valid(span, "flags", _uint(raw.get("flags"), _MAX_UINT32))
    kind = raw.get("kind")
    if isinstance(kind, int) and not isinstance(kind, bool) and 0 <= kind <= 5:
        span["kind"] = kind
    for field in ("startTimeUnixNano", "endTimeUnixNano"):
        _set_valid(span, field, _uint(raw.get(field), _MAX_INT64))
    span["name"] = _name(raw.get("name"))
    attributes, dropped = _attributes(raw.get("attributes"), MAX_ATTRIBUTES)
    span["attributes"] = attributes
    span["droppedAttributesCount"] = _total(raw.get("droppedAttributesCount"), dropped)
    events, dropped_events = _capped(raw.get("events"), MAX_EVENTS)
    span["events"] = [_event(event) for event in events]
    span["droppedEventsCount"] = _total(raw.get("droppedEventsCount"), dropped_events)
    links, dropped_links = _capped(raw.get("links"), MAX_LINKS)
    valid_links = [link for link in map(_link, links) if link is not None]
    span["links"] = valid_links
    span["droppedLinksCount"] = _total(
        raw.get("droppedLinksCount"), dropped_links + len(links) - len(valid_links)
    )
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


def _scope_spans(raw: Json) -> Json | None:
    spans = [span for span in map(_span, raw.get("spans", [])) if span is not None]
    return {"scope": _scope(raw.get("scope")), "spans": spans} if spans else None


def rewrite(payload: Json) -> Json | None:
    """The export rebuilt from what `parse` accepted: only the fields listed here
    survive. A scope with no surviving span is dropped, and a resource with no surviving
    scope; None when no span is left."""
    resources: list[Json] = []
    for resource_spans in payload["resourceSpans"]:
        scopes = [
            scope
            for scope in map(_scope_spans, resource_spans.get("scopeSpans", []))
            if scope is not None
        ]
        if scopes:
            resources.append(
                {"resource": _resource(resource_spans.get("resource")), "scopeSpans": scopes}
            )
    return {"resourceSpans": resources} if resources else None


def prepare(body: bytes) -> bytes | None:
    """The body as the relay forwards it, or None when no span survives. ASCII JSON, so
    a lone surrogate the page escaped stays an escape rather than failing to encode."""
    rewritten = rewrite(parse(body))
    if rewritten is None:
        return None
    return json.dumps(rewritten, separators=(",", ":")).encode("ascii")


__all__ = [
    "KEPT_RESOURCE_ATTRIBUTES",
    "MAX_ARRAY_ITEMS",
    "MAX_ATTRIBUTES",
    "MAX_EVENTS",
    "MAX_EVENT_ATTRIBUTES",
    "MAX_LINKS",
    "MAX_LINK_ATTRIBUTES",
    "MAX_NAME_CHARS",
    "MAX_RESOURCES",
    "MAX_SCOPES",
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
