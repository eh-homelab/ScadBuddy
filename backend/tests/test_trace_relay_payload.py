"""telemetry/payload.py: what the relay forwards of a page's spans (spec 2026-10-01 §5.2, §6)."""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from typing import Any

import pytest

from scadbuddy.api.realtime import origin_allowed
from scadbuddy.telemetry import payload
from scadbuddy.telemetry.payload import BatchTooLargeError, PayloadError
from tests.support.otlp import SENTINEL, SPAN_ID, TRACE_ID, Json, export, span, string

FILES_ROUTE = "/api/v1/models/{slug}/files/{path:path}"
PUBLIC_URL = "https://scadbuddy.example"


def routes(path: str) -> str | None:
    """Stands in for the app's routes: two of them."""
    if path == "/api/v1/models":
        return path
    if re.fullmatch(r"/api/v1/models/[^/]+/files/.+", path):
        return FILES_ROUTE
    return None


def own_origin(origin: str) -> bool:
    """Stands in for the relay's check: the public URL, or loopback."""
    return origin_allowed(origin, PUBLIC_URL)


def prepare(body: bytes) -> bytes | None:
    return payload.prepare(body, routes, own_origin)


def _prepared(body: bytes) -> bytes:
    prepared = prepare(body)
    assert prepared is not None
    return prepared


def forwarded(body: bytes) -> Json:
    forwarded_body = prepare(body)
    assert forwarded_body is not None
    result: Json = json.loads(forwarded_body)
    return result


def only_span(body: bytes) -> Json:
    (scope,) = forwarded(body)["resourceSpans"][0]["scopeSpans"]
    (result,) = scope["spans"]
    return dict(result)


def test_the_resource_is_rebuilt_as_the_web_service() -> None:
    body = export(
        span(),
        resource=[
            string("service.name", "scadbuddy-api"),
            string("service.version", "1.2.3"),
            string("user_agent.original", "Mozilla/5.0"),
            string("host.name", "attacker"),
            string("telemetry.sdk.name", "opentelemetry"),
        ],
    )
    resource = forwarded(body)["resourceSpans"][0]["resource"]
    assert resource == {
        "attributes": [
            string("service.name", "scadbuddy-web"),
            string("service.version", "1.2.3"),
        ]
    }


def test_a_span_keeps_its_ids_times_and_kind_and_nothing_unknown() -> None:
    result = only_span(export(span(parentSpanId="00f067aa0ba902b7", extra=SENTINEL)))
    assert result["traceId"] == TRACE_ID
    assert result["spanId"] == SPAN_ID
    assert result["parentSpanId"] == "00f067aa0ba902b7"
    assert result["kind"] == 1
    assert result["startTimeUnixNano"] == "1700000000000000000"
    assert "extra" not in result


@pytest.mark.parametrize(("count", "kept", "dropped"), [(64, 64, 0), (65, 64, 1)])
def test_attributes_are_capped_and_counted(count: int, kept: int, dropped: int) -> None:
    attributes = [string(f"a{i}", "v") for i in range(count)]
    result = only_span(export(span(attributes=attributes, droppedAttributesCount=2)))
    assert len(result["attributes"]) == kept
    assert result["droppedAttributesCount"] == 2 + dropped


@pytest.mark.parametrize(("length", "kept"), [(1024, 1024), (1025, 1024)])
def test_a_string_value_is_truncated_uncounted(length: int, kept: int) -> None:
    result = only_span(export(span(attributes=[string("a", "x" * length)])))
    assert len(result["attributes"][0]["value"]["stringValue"]) == kept
    assert result["droppedAttributesCount"] == 0


@pytest.mark.parametrize(("length", "kept"), [(32, 32), (33, 32)])
def test_an_array_value_is_cut_to_its_cap(length: int, kept: int) -> None:
    values = [{"intValue": str(i)} for i in range(length)]
    attribute = {"key": "a", "value": {"arrayValue": {"values": values}}}
    result = only_span(export(span(attributes=[attribute])))
    assert len(result["attributes"][0]["value"]["arrayValue"]["values"]) == kept


@pytest.mark.parametrize(("count", "kept", "dropped"), [(16, 16, 0), (17, 16, 1)])
def test_events_are_capped_and_counted(count: int, kept: int, dropped: int) -> None:
    events = [{"name": f"e{i}", "timeUnixNano": "1", "attributes": []} for i in range(count)]
    result = only_span(export(span(events=events)))
    assert len(result["events"]) == kept
    assert result["droppedEventsCount"] == dropped


@pytest.mark.parametrize(("count", "kept", "dropped"), [(16, 16, 0), (17, 16, 1)])
def test_event_attributes_are_capped_and_counted(count: int, kept: int, dropped: int) -> None:
    event = {"name": "e", "attributes": [string(f"a{i}", "v") for i in range(count)]}
    (result,) = only_span(export(span(events=[event])))["events"]
    assert len(result["attributes"]) == kept
    assert result["droppedAttributesCount"] == dropped


@pytest.mark.parametrize(("count", "kept", "dropped"), [(8, 8, 0), (9, 8, 1)])
def test_links_are_capped_and_counted(count: int, kept: int, dropped: int) -> None:
    links = [{"traceId": TRACE_ID, "spanId": SPAN_ID, "attributes": []} for _ in range(count)]
    result = only_span(export(span(links=links)))
    assert len(result["links"]) == kept
    assert result["droppedLinksCount"] == dropped


@pytest.mark.parametrize(("count", "kept", "dropped"), [(16, 16, 0), (17, 16, 1)])
def test_link_attributes_are_capped_and_counted(count: int, kept: int, dropped: int) -> None:
    link = {
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
        "attributes": [string(f"a{i}", "v") for i in range(count)],
    }
    (result,) = only_span(export(span(links=[link])))["links"]
    assert len(result["attributes"]) == kept
    assert result["droppedAttributesCount"] == dropped


@pytest.mark.parametrize(("length", "kept"), [(128, 128), (129, 128)])
def test_the_name_is_truncated(length: int, kept: int) -> None:
    assert len(only_span(export(span(name="n" * length)))["name"]) == kept


def test_a_value_no_page_sends_is_dropped_and_counted() -> None:
    attributes = [
        {"key": "map", "value": {"kvlistValue": {"values": []}}},
        {"key": "bytes", "value": {"bytesValue": "AAAA"}},
        {"key": "nested", "value": {"arrayValue": {"values": [{"arrayValue": {}}]}}},
        {"key": "big", "value": {"intValue": "1" * 30}},
        {"value": {"stringValue": "no key"}},
        "not an attribute",
        string("kept", "v"),
    ]
    result = only_span(export(span(attributes=attributes)))
    assert result["attributes"] == [string("kept", "v")]
    assert result["droppedAttributesCount"] == 6


def test_query_strings_and_user_agents_are_scrubbed_as_the_backend_does() -> None:
    attributes = [
        string("http.url", f"https://scadbuddy.example/api/v1/models?q={SENTINEL}"),
        string("url.query", f"q={SENTINEL}"),
        string("user_agent.original", SENTINEL),
        string("http.user_agent", SENTINEL),
    ]
    result = only_span(export(span(attributes=attributes)))
    assert result["attributes"] == [string("http.url", "https://scadbuddy.example/api/v1/models")]
    assert result["droppedAttributesCount"] == 0


def test_host_names_and_peer_addresses_are_scrubbed_as_the_backend_does() -> None:
    attributes = [
        string(key, SENTINEL)
        for key in (
            "http.host",
            "http.server_name",
            "server.address",
            "net.peer.ip",
            "client.address",
            "network.peer.address",
        )
    ]
    attributes.append(string("http.method", "GET"))
    result = only_span(export(span(attributes=attributes)))
    assert result["attributes"] == [string("http.method", "GET")]
    assert result["droppedAttributesCount"] == 0


def test_a_query_string_is_cut_from_each_item_of_an_array_valued_url() -> None:
    urls = {
        "arrayValue": {
            "values": [
                {"stringValue": f"https://scadbuddy.example/a?q={SENTINEL}"},
                {"stringValue": "https://scadbuddy.example/b"},
                {"intValue": 3},
            ]
        }
    }
    result = only_span(export(span(attributes=[{"key": "url.full", "value": urls}])))
    assert result["attributes"] == [
        {
            "key": "url.full",
            "value": {
                "arrayValue": {
                    "values": [
                        {"stringValue": "https://scadbuddy.example"},
                        {"stringValue": "https://scadbuddy.example"},
                        {"intValue": 3},
                    ]
                }
            },
        }
    ]
    assert SENTINEL.encode() not in _prepared(
        export(span(attributes=[{"key": "url.full", "value": urls}]))
    )


def test_an_exception_keeps_its_type_and_frames_only() -> None:
    stack = (
        f"TypeError: {SENTINEL}\n"
        f"second line of the message {SENTINEL}\n"
        "    at render (https://scadbuddy.example/assets/index-abc.js:10:5)\n"
        "    at https://scadbuddy.example/assets/index-abc.js:20:7\n"
        "onClick@https://scadbuddy.example/assets/index-abc.js:30:9"
    )
    event = {
        "name": "exception",
        "timeUnixNano": "1",
        "attributes": [
            string("exception.type", "TypeError"),
            string("exception.message", SENTINEL),
            string("exception.stacktrace", stack),
        ],
    }
    status = {"code": 2, "message": SENTINEL}
    body = export(span(events=[event], status=status))
    assert SENTINEL not in _prepared(body).decode()
    result = only_span(body)
    assert result["status"] == {"code": 2, "message": "TypeError"}
    (scrubbed,) = result["events"]
    assert scrubbed["attributes"] == [
        string("exception.type", "TypeError"),
        string(
            "exception.stacktrace",
            "at render (https://scadbuddy.example/assets/index-abc.js:10:5)\n"
            "at https://scadbuddy.example/assets/index-abc.js:20:7\n"
            "onClick@https://scadbuddy.example/assets/index-abc.js:30:9",
        ),
    ]


def test_a_status_description_without_an_exception_reads_error() -> None:
    result = only_span(export(span(status={"code": 2, "message": SENTINEL})))
    assert result["status"] == {"code": 2, "message": "error"}


def test_512_spans_pass_and_513_do_not() -> None:
    body = export(*[span() for _ in range(payload.MAX_SPANS)])
    assert len(forwarded(body)["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 512
    with pytest.raises(BatchTooLargeError):
        prepare(export(*[span() for _ in range(payload.MAX_SPANS + 1)]))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe\x00",
        b"[]",
        b"{}",
        b'{"resourceSpans": {}}',
        b'{"resourceSpans": [1]}',
        b'{"resourceSpans": [{"scopeSpans": [{"spans": [1]}]}]}',
        b"[" * 100_000,
    ],
    ids=["text", "not-utf8", "array", "empty", "object", "number", "span-number", "deep"],
)
def test_a_body_that_is_not_an_export_is_refused(body: bytes) -> None:
    with pytest.raises(PayloadError):
        prepare(body)


def test_a_lone_surrogate_is_forwarded_escaped() -> None:
    ids = f'"traceId":"{TRACE_ID}","spanId":"{SPAN_ID}"'
    body = (
        '{"resourceSpans":[{"scopeSpans":[{"spans":[{' + ids + ',"name":"\\ud800"}]}]}]}'
    ).encode()
    assert b"\\ud800" in _prepared(body)


def _flat(body: bytes) -> str:
    text = _prepared(body).decode()
    json.loads(text)
    assert SENTINEL not in text
    assert "NaN" not in text
    assert "Infinity" not in text
    return text


def test_invalid_passthrough_fields_never_reach_the_collector() -> None:
    link = {"traceId": {"x": SENTINEL}, "spanId": SPAN_ID}
    bad = span(
        parentSpanId=SENTINEL,
        kind=SENTINEL,
        traceState={"x": SENTINEL},
        flags=SENTINEL,
        startTimeUnixNano={"x": SENTINEL},
        endTimeUnixNano="9" * 19,
        events=[{"name": "e", "timeUnixNano": {"x": SENTINEL}}],
        links=[link],
        status={"code": SENTINEL},
    )
    _flat(export(bad))
    result = only_span(export(bad))
    for field in ("parentSpanId", "kind", "traceState", "flags", "startTimeUnixNano"):
        assert field not in result
    assert "endTimeUnixNano" not in result
    assert result["links"] == []
    assert result["droppedLinksCount"] == 1
    assert "timeUnixNano" not in result["events"][0]
    assert result["status"] == {}


@pytest.mark.parametrize("field", ["traceId", "spanId"])
@pytest.mark.parametrize("value", [{"a": SENTINEL}, SENTINEL, "0" * 32, "0" * 16, "ab"])
def test_a_span_without_valid_ids_is_dropped(field: str, value: object) -> None:
    body = export(span(**{field: value}), span(name="kept"))
    assert [s["name"] for s in forwarded(body)["resourceSpans"][0]["scopeSpans"][0]["spans"]] == [
        "kept"
    ]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_double_is_dropped_and_counted(value: float) -> None:
    attribute = {"key": "d", "value": {"doubleValue": value}}
    body = export(span(attributes=[attribute, {"key": "e", "value": {"doubleValue": 1.5}}]))
    _flat(body)
    result = only_span(body)
    assert result["attributes"] == [{"key": "e", "value": {"doubleValue": 1.5}}]
    assert result["droppedAttributesCount"] == 1


def test_dropped_counts_are_clamped() -> None:
    attributes = [string(f"a{i}", "v") for i in range(70)]
    result = only_span(
        export(
            span(
                attributes=attributes,
                droppedAttributesCount=10**30,
                droppedEventsCount=2**32 - 1,
                events=[{"name": f"e{i}"} for i in range(20)],
            )
        )
    )
    assert result["droppedAttributesCount"] == 2**32 - 1
    assert result["droppedEventsCount"] == 2**32 - 1


def test_valid_values_pass_unchanged() -> None:
    result = only_span(
        export(
            span(
                traceId=TRACE_ID.upper(),
                parentSpanId="00f067aa0ba902b7",
                traceState="a=b",
                flags=1,
                kind=5,
                endTimeUnixNano=1700000000100000000,
                status={"code": "STATUS_CODE_OK"},
            )
        )
    )
    assert result["traceId"] == TRACE_ID.upper()
    assert result["traceState"] == "a=b"
    assert result["flags"] == 1
    assert result["kind"] == 5
    assert result["endTimeUnixNano"] == 1700000000100000000
    assert result["status"] == {"code": 1}


@pytest.mark.parametrize("field", ["parentSpanId"])
def test_a_trailing_newline_is_not_a_valid_id(field: str) -> None:
    result = only_span(export(span(**{field: "c" * 16 + "\n"})))
    assert field not in result


def test_a_trailing_newline_is_not_a_valid_id_or_time() -> None:
    assert prepare(export(span(spanId="c" * 16 + "\n"))) is None
    assert prepare(export(span(traceId="c" * 32 + "\n"))) is None
    result = only_span(export(span(startTimeUnixNano="12\n")))
    assert "startTimeUnixNano" not in result
    link = {"traceId": TRACE_ID, "spanId": "c" * 16 + "\n"}
    assert only_span(export(span(links=[link])))["links"] == []


@pytest.mark.parametrize(
    "value",
    [
        {"intValue": "12\n"},
        {"intValue": 2**63},
        {"intValue": "9223372036854775808"},
        {"intValue": -(2**63) - 1},
        {"intValue": 10**400},
        {"doubleValue": 10**400},
        {"doubleValue": -(10**400)},
    ],
)
def test_an_unrepresentable_number_is_dropped_and_counted(value: Json) -> None:
    result = only_span(export(span(attributes=[{"key": "n", "value": value}])))
    assert result["attributes"] == []
    assert result["droppedAttributesCount"] == 1


@pytest.mark.parametrize(
    "value",
    [{"intValue": 2**63 - 1}, {"intValue": "-9223372036854775808"}, {"doubleValue": 10**300}],
)
def test_representable_numbers_pass(value: Json) -> None:
    attribute = {"key": "n", "value": value}
    assert only_span(export(span(attributes=[attribute])))["attributes"] == [attribute]


HOSTILE = [
    b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"traceId":"%s","spanId":"%s","attributes":'
    b'[{"key":"a","value":{"doubleValue":1'
    % (TRACE_ID.encode(), SPAN_ID.encode())
    + b"0" * 400
    + b"}}]}]}]}]}",
    b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"kind":1e999,"status":{"code":1e999}}]}]}]}',
    b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"droppedAttributesCount":1'
    + b"0" * 5000
    + b"}]}]}]}",
    b'{"resourceSpans":[{"resource":{"attributes":[{"key":"service.version","value":null}]},'
    b'"scopeSpans":[{"scope":[],"spans":[{"events":[1,null,{"attributes":"x"}],"links":{}}]}]}]}',
    b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"attributes":[{"key":"k","value":{"arrayValue":'
    b'{"values":[{"doubleValue":1e999},{"intValue":1e999}]}}}]}]}]}]}',
    b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"status":{"code":2,"message":1}}]}]}]}',
]


@pytest.mark.parametrize("body", HOSTILE)
def test_prepare_raises_only_its_own_errors(body: bytes) -> None:
    try:
        out = prepare(body)
    except (PayloadError, BatchTooLargeError):
        return
    if out is not None:
        json.loads(out)


def test_more_than_16_resource_spans_are_too_large() -> None:
    body = json.dumps({"resourceSpans": [{} for _ in range(17)]}).encode()
    with pytest.raises(BatchTooLargeError, match="at most 16 resourceSpans"):
        prepare(body)
    assert prepare(json.dumps({"resourceSpans": [{} for _ in range(16)]}).encode()) is None


def test_more_than_64_scope_spans_in_total_are_too_large() -> None:
    scopes: list[Json] = [{} for _ in range(33)]
    body = json.dumps({"resourceSpans": [{"scopeSpans": scopes}, {"scopeSpans": scopes}]}).encode()
    with pytest.raises(BatchTooLargeError, match="at most 64 scopeSpans"):
        prepare(body)


def test_a_body_whose_spans_are_all_invalid_forwards_nothing() -> None:
    assert prepare(export(span(traceId="0" * 32), span(spanId="nope"))) is None
    assert prepare(json.dumps({"resourceSpans": [{}, {"scopeSpans": [{}]}]}).encode()) is None


def test_a_scope_or_resource_with_no_surviving_span_is_dropped() -> None:
    body = json.dumps(
        {
            "resourceSpans": [
                {"scopeSpans": [{"spans": [span(traceId="0" * 32)]}, {"spans": [span()]}]},
                {"scopeSpans": [{"spans": [span(traceId="0" * 32)]}]},
            ]
        }
    ).encode()
    result = forwarded(body)
    (resource,) = result["resourceSpans"]
    (scope,) = resource["scopeSpans"]
    assert len(scope["spans"]) == 1


def test_the_output_of_a_largest_legal_body_stays_proportional() -> None:
    scopes: list[Json] = [{} for _ in range(4)]
    body = json.dumps({"resourceSpans": [{"scopeSpans": scopes} for _ in range(16)]}).encode()
    assert prepare(body) is None
    spans = [span() for _ in range(100)]
    body = json.dumps(
        {"resourceSpans": [{"scopeSpans": [{"spans": spans}]} for _ in range(5)]}
    ).encode()
    out = prepare(body)
    assert out is not None
    assert len(out) < 2 * len(body)


def test_a_message_smuggled_into_the_stack_is_removed_before_frames_are_kept() -> None:
    message = "a\n    at SECRET:1:2"
    stack = "Error: a\n    at SECRET:1:2\n    at real (https://scadbuddy.example/assets/a.js:1:2)"
    event = {
        "name": "exception",
        "attributes": [
            string("exception.message", message),
            string("exception.stacktrace", stack),
        ],
    }
    (scrubbed,) = only_span(export(span(events=[event])))["events"]
    assert scrubbed["attributes"] == [
        string("exception.stacktrace", "at real (https://scadbuddy.example/assets/a.js:1:2)")
    ]


_FRAMES = (
    "    at f (https://scadbuddy.example/assets/index-a1b2.js:112:15)\n"
    "    at https://scadbuddy.example/assets/index-a1b2.js:20:7\n"
    "g@https://scadbuddy.example/assets/index-a1b2.js:30:9"
)


@pytest.mark.parametrize("message", ["1", "at", "assets", "host"])
def test_a_message_found_inside_a_frame_leaves_the_frames_byte_identical(message: str) -> None:
    event = {
        "name": "exception",
        "attributes": [
            string("exception.type", "Error"),
            string("exception.message", message),
            string("exception.stacktrace", f"Error: {message}\n{_FRAMES}"),
        ],
    }
    (scrubbed,) = only_span(export(span(events=[event])))["events"]
    assert scrubbed["attributes"] == [
        string("exception.type", "Error"),
        string("exception.stacktrace", "\n".join(line.strip() for line in _FRAMES.splitlines())),
    ]


def test_a_stack_without_a_message_header_keeps_every_frame() -> None:
    """Firefox and Safari write no ``<type>: <message>`` line: nothing is removed."""
    stack = "f@https://scadbuddy.example/assets/index-a1b2.js:1:2\ng@https://scadbuddy.example/assets/index-a1b2.js:3:4"
    event = {
        "name": "exception",
        "attributes": [
            string("exception.message", "1"),
            string("exception.stacktrace", stack),
        ],
    }
    (scrubbed,) = only_span(export(span(events=[event])))["events"]
    assert scrubbed["attributes"] == [string("exception.stacktrace", stack)]


def test_an_exception_message_is_removed_from_a_span_every_event_and_a_link() -> None:
    """The page's input is untrusted: a message is dropped wherever it is put, not only
    from an ``exception`` event (spec §6)."""
    attributes = [string("exception.message", SENTINEL), string("http.method", "GET")]
    events = [
        {"name": "exception", "attributes": attributes},
        {"name": "fetch", "attributes": attributes},
    ]
    link = {"traceId": TRACE_ID, "spanId": SPAN_ID, "attributes": attributes}
    body = export(span(attributes=attributes, events=events, links=[link]))
    assert SENTINEL not in _prepared(body).decode()
    result = only_span(body)
    for scrubbed in (result, *result["events"], result["links"][0]):
        assert scrubbed["attributes"] == [string("http.method", "GET")]
        assert scrubbed["droppedAttributesCount"] == 0


def _leaky_stack() -> str:
    return f"TypeError: {SENTINEL}\n{_FRAMES}"


def _frames_only() -> str:
    return "\n".join(line.strip() for line in _FRAMES.splitlines())


@pytest.mark.parametrize("place", ["span", "other_event", "link"])
def test_a_stacktrace_outside_an_exception_event_loses_its_message(place: str) -> None:
    """Review 6 of #1090: the stack repeats the message in its V8 head, wherever the
    page puts it (spec §6)."""
    attributes = [
        string("exception.type", "TypeError"),
        string("exception.message", SENTINEL),
        string("exception.stacktrace", _leaky_stack()),
    ]
    link = {"traceId": TRACE_ID, "spanId": SPAN_ID, "attributes": attributes}
    event = {"name": "fetch", "attributes": attributes}
    kwargs: dict[str, Any] = {
        "span": {"attributes": attributes},
        "other_event": {"events": [event]},
        "link": {"links": [link]},
    }[place]
    body = export(span(**kwargs))
    assert SENTINEL not in _prepared(body).decode()
    result = only_span(body)
    holders = {"span": [result], "other_event": result["events"], "link": result["links"]}
    (target,) = holders[place]
    assert target["attributes"] == [
        string("exception.type", "TypeError"),
        string("exception.stacktrace", _frames_only()),
    ]


def test_a_stacktrace_with_no_message_beside_it_keeps_frames_only() -> None:
    attributes = [string("exception.stacktrace", _leaky_stack())]
    result = only_span(export(span(attributes=attributes)))
    assert result["attributes"] == [string("exception.stacktrace", _frames_only())]


def test_a_non_string_stacktrace_is_not_forwarded() -> None:
    attributes = [{"key": "exception.stacktrace", "value": {"intValue": 1}}]
    assert only_span(export(span(attributes=attributes)))["attributes"] == []


def test_http_status_text_is_removed_from_a_span_an_event_and_a_link() -> None:
    """A status line's text is the server's words, not the page's: dropped everywhere."""
    attributes = [string("http.status_text", SENTINEL), string("http.method", "GET")]
    event = {"name": "fetch", "attributes": attributes}
    link = {"traceId": TRACE_ID, "spanId": SPAN_ID, "attributes": attributes}
    body = export(span(attributes=attributes, events=[event], links=[link]))
    assert SENTINEL not in _prepared(body).decode()
    result = only_span(body)
    for scrubbed in (result, result["events"][0], result["links"][0]):
        assert scrubbed["attributes"] == [string("http.method", "GET")]
        assert scrubbed["droppedAttributesCount"] == 0


def test_header_attributes_are_removed_from_a_span_an_event_and_a_link() -> None:
    headers = [
        string("http.request.header.cookie", SENTINEL),
        string("http.response.header.set_cookie", SENTINEL),
        string("http.method", "GET"),
    ]
    event = {"name": "fetch", "attributes": headers}
    link = {"traceId": TRACE_ID, "spanId": SPAN_ID, "attributes": headers}
    body = export(span(attributes=headers, events=[event], links=[link]))
    assert SENTINEL not in _prepared(body).decode()
    result = only_span(body)
    for scrubbed in (result, result["events"][0], result["links"][0]):
        assert scrubbed["attributes"] == [string("http.method", "GET")]
        assert scrubbed["droppedAttributesCount"] == 0


@pytest.mark.parametrize("key", ["url.full", "http.url"])
def test_a_url_on_a_route_keeps_its_origin_and_the_route_template(key: str) -> None:
    url = f"https://scadbuddy.example/api/v1/models/box/files/{SENTINEL}.scad?x={SENTINEL}#f"
    result = only_span(export(span(attributes=[string(key, url)])))
    assert result["attributes"] == [string(key, f"https://scadbuddy.example{FILES_ROUTE}")]


@pytest.mark.parametrize("key", ["http.target", "url.path"])
def test_a_path_on_a_route_becomes_the_route_template(key: str) -> None:
    path = f"/api/v1/models/box/files/{SENTINEL}.scad?x={SENTINEL}"
    result = only_span(export(span(attributes=[string(key, path)])))
    assert result["attributes"] == [string(key, FILES_ROUTE)]


def test_a_url_on_no_route_keeps_only_its_origin() -> None:
    """A document-load span's page URL: the SPA's routes are the browser's own."""
    url = f"https://user:{SENTINEL}@scadbuddy.example:8443/m/{SENTINEL}?q=1"
    result = only_span(export(span(attributes=[string("url.full", url)])))
    assert result["attributes"] == [string("url.full", "https://scadbuddy.example:8443")]


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        (f"https://bambuddy.lan/api/v1/models/a/files/{SENTINEL}", "https://bambuddy.lan"),
        (
            f"https://scadbuddy.example.evil/api/v1/models?q={SENTINEL}",
            "https://scadbuddy.example.evil",
        ),
        (f"//bambuddy.lan/api/v1/models/a/files/{SENTINEL}", "//bambuddy.lan"),
    ],
)
def test_a_url_on_another_host_keeps_only_its_origin_though_its_path_matches_a_route(
    url: str, kept: str
) -> None:
    """Another host has none of ScadBuddy's routes: a template would name one it lacks."""
    result = only_span(export(span(attributes=[string("url.full", url)])))
    assert result["attributes"] == [string("url.full", kept)]


@pytest.mark.parametrize(
    "url", ["http://localhost:5173/api/v1/models", "http://127.0.0.1:8080/api/v1/models"]
)
def test_a_loopback_url_on_a_route_keeps_the_route_template(url: str) -> None:
    result = only_span(export(span(attributes=[string("url.full", url)])))
    origin = url.removesuffix("/api/v1/models")
    assert result["attributes"] == [string("url.full", f"{origin}/api/v1/models")]


@pytest.mark.parametrize(
    "value", [f"/m/{SENTINEL}", f"m/{SENTINEL}", f"javascript:{SENTINEL}", f"?{SENTINEL}", ""]
)
def test_a_relative_url_on_no_route_is_dropped_uncounted(value: str) -> None:
    attributes = [string("http.target", value), string("http.method", "GET")]
    result = only_span(export(span(attributes=attributes)))
    assert result["attributes"] == [string("http.method", "GET")]
    assert result["droppedAttributesCount"] == 0


def test_each_url_of_an_array_is_reduced_and_an_unmatched_relative_one_left_out() -> None:
    urls = {
        "arrayValue": {
            "values": [
                {"stringValue": f"https://scadbuddy.example/api/v1/models/a/files/{SENTINEL}"},
                {"stringValue": f"/api/v1/models/b/files/{SENTINEL}"},
                {"stringValue": f"/m/{SENTINEL}"},
                {"boolValue": True},
            ]
        }
    }
    result = only_span(export(span(attributes=[{"key": "url.full", "value": urls}])))
    assert result["attributes"] == [
        {
            "key": "url.full",
            "value": {
                "arrayValue": {
                    "values": [
                        {"stringValue": f"https://scadbuddy.example{FILES_ROUTE}"},
                        {"stringValue": FILES_ROUTE},
                        {"boolValue": True},
                    ]
                }
            },
        }
    ]


def test_urls_on_events_and_links_are_reduced_too() -> None:
    urls = [
        string("url.full", f"http://localhost:5173/api/v1/models/a/files/{SENTINEL}"),
        string("http.target", f"/m/{SENTINEL}"),
    ]
    event = {"name": "fetch", "attributes": urls}
    link = {"traceId": TRACE_ID, "spanId": SPAN_ID, "attributes": urls}
    body = export(span(attributes=urls, events=[event], links=[link]))
    assert SENTINEL not in (prepare(body) or b"").decode()
    result = only_span(body)
    for scrubbed in (result, result["events"][0], result["links"][0]):
        assert scrubbed["attributes"] == [string("url.full", f"http://localhost:5173{FILES_ROUTE}")]


def test_the_matcher_is_given_the_decoded_path_without_query_or_fragment() -> None:
    seen: list[str] = []

    def spy(path: str) -> str | None:
        seen.append(path)
        return None

    url = "https://scadbuddy.example/api/v1/models/a%20b/files/x?y=1#z"
    payload.prepare(export(span(attributes=[string("url.full", url)])), spy, own_origin)
    assert seen == ["/api/v1/models/a b/files/x"]


def _stack_after_prepare(stack: str) -> str:
    event = {
        "name": "exception",
        "attributes": [string("exception.type", "Error"), string("exception.stacktrace", stack)],
    }
    (scrubbed,) = only_span(export(span(events=[event])))["events"]
    attribute = scrubbed["attributes"][-1]
    assert attribute["key"] == "exception.stacktrace"
    return str(attribute["value"]["stringValue"])


def test_a_frame_url_on_an_spa_path_keeps_its_origin_without_path_or_query() -> None:
    """An inline script or an eval frame reports the document's URL (spec §6)."""
    stack = (
        f"Error: x\n    at f (https://scadbuddy.example/models/{SENTINEL}/customize?q={SENTINEL}:12:3)\n"
        f"    at https://scadbuddy.example/m/{SENTINEL}#{SENTINEL}:4:5\n"
        f"g@https://scadbuddy.example/models/{SENTINEL}?q={SENTINEL}:6:7"
    )
    out = _stack_after_prepare(stack)
    assert SENTINEL not in out
    assert out == (
        "at f (https://scadbuddy.example:12:3)\n"
        "at https://scadbuddy.example:4:5\n"
        "g@https://scadbuddy.example:6:7"
    )


def test_an_eval_frame_url_is_reduced() -> None:
    stack = (
        "Error: x\n    at eval (eval at g (https://scadbuddy.example/models/"
        f"{SENTINEL}/customize?q={SENTINEL}:12:3), <anonymous>:1:1)"
    )
    out = _stack_after_prepare(stack)
    assert SENTINEL not in out
    assert out == "at eval (eval at g (https://scadbuddy.example:12:3), <anonymous>:1:1)"


def test_a_frame_url_on_a_backend_route_becomes_its_template() -> None:
    out = _stack_after_prepare(
        f"    at f (https://scadbuddy.example/api/v1/models/box/files/{SENTINEL}.scad?x=1:1:2)"
    )
    assert out == f"at f (https://scadbuddy.example{FILES_ROUTE}:1:2)"


def test_a_bundle_frame_keeps_its_asset_path_without_the_query() -> None:
    stack = (
        "    at f (https://scadbuddy.example/assets/index-a1b2.js?v=9:112:15)\n"
        "g@https://scadbuddy.example/assets/index-a1b2.js:30:9"
    )
    assert _stack_after_prepare(stack) == (
        "at f (https://scadbuddy.example/assets/index-a1b2.js:112:15)\n"
        "g@https://scadbuddy.example/assets/index-a1b2.js:30:9"
    )


def test_a_frame_url_on_another_host_keeps_only_its_origin() -> None:
    out = _stack_after_prepare(f"    at f (https://cdn.example/assets/{SENTINEL}.js?q=1:1:2)")
    assert out == "at f (https://cdn.example:1:2)"


#: The pattern the frame URLs were found with before #1090's gate: right, but
#: quadratic on a long token, so it is the oracle on short ones only.
_QUADRATIC_FRAME_URL = re.compile(r"(?P<url>[a-z][a-z0-9+.-]*://\S+):(?P<position>\d+:\d+)", re.I)
#: Generous for linear work on a few hundred kilobytes; the quadratic pattern took
#: seconds on a fifth of the first input below.
_LINEAR_BUDGET_SECONDS = 0.1


def _timed(work: Callable[[], object]) -> float:
    started = time.perf_counter()
    work()
    return time.perf_counter() - started


def _reduce_all(url: str, *, frame: bool = False) -> str:
    return "R"


@pytest.mark.parametrize(
    "token",
    [
        "a" * 100_000 + ":1:1",
        "@" + "a://" * 25_000 + ":1",
        "a://" + ":1" * 50_000,
        "a://x" + ":" + "1" * 50_000 + ":" * 50_000,
        "(" + "1://" * 25_000 + "a",
    ],
    ids=["letters", "schemes", "positions", "digits-then-colons", "no-scheme-letter"],
)
def test_finding_a_frame_url_is_linear_in_the_token(token: str) -> None:
    """The gate's ReDoS finding on #1090: the URL in a frame line was found by a pattern
    that rescanned the rest of the token from every letter."""
    assert _timed(lambda: payload._reduced_frame(token, _reduce_all)) < _LINEAR_BUDGET_SECONDS


def test_frame_urls_are_found_where_the_pattern_found_them() -> None:
    rng = random.Random(1090)
    alphabet = "ab1:/ ()@.-+é"
    for _ in range(20_000):
        line = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 30)))
        expected = _QUADRATIC_FRAME_URL.sub(lambda match: f"R:{match['position']}", line)
        assert payload._reduced_frame(line, _reduce_all) == expected, line


def _stack_body(stack: str) -> bytes:
    event = {"name": "exception", "attributes": [string("exception.stacktrace", stack)]}
    return export(span(events=[event]))


def test_a_pathological_stack_line_is_dropped_in_linear_time() -> None:
    body = _stack_body("    at " + "a" * 100_000 + ":1:1")
    assert _timed(lambda: prepare(body)) < _LINEAR_BUDGET_SECONDS
    assert _stack_after_prepare("    at " + "a" * 100_000 + ":1:1") == ""


def test_a_stack_of_long_frame_lines_is_read_in_linear_time() -> None:
    line = "    at " + "a" * (payload.MAX_FRAME_LINE_CHARS - 11) + ":1:1"
    body = _stack_body("\n".join([line] * 500))
    assert _timed(lambda: prepare(body)) < _LINEAR_BUDGET_SECONDS


def test_a_frame_line_over_the_cap_is_dropped_and_one_at_it_kept() -> None:
    url = "https://cdn.example/"
    at_cap = "    at f (" + url + "a" * (payload.MAX_FRAME_LINE_CHARS - 15 - len(url)) + ":1:2)"
    assert len(at_cap) == payload.MAX_FRAME_LINE_CHARS
    over = at_cap.replace("(", "((", 1)
    assert _stack_after_prepare(f"{over}\n{at_cap}") == "at f (https://cdn.example:1:2)"


def test_frames_stop_where_the_string_is_cut() -> None:
    frame = "g@https://scadbuddy.example/assets/index-a1b2.js:30:9"
    out = _stack_after_prepare("\n".join([frame] * 10_000))
    assert out == "\n".join([frame] * 10_000)[: payload.MAX_STRING_CHARS]


def test_a_long_url_attribute_is_reduced_in_linear_time() -> None:
    urls = [
        "https://scadbuddy.example/" + "a" * 200_000,
        "https://" + "a@" * 100_000 + "x/y",
        "a" * 200_000 + "://x",
    ]
    body = export(span(attributes=[string("url.full", url) for url in urls]))
    assert _timed(lambda: prepare(body)) < _LINEAR_BUDGET_SECONDS
