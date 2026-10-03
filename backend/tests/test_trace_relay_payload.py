"""telemetry/payload.py: what the relay forwards of a page's spans (spec 2026-10-01 §5.2, §6)."""

from __future__ import annotations

import json

import pytest

from scadbuddy.telemetry import payload
from scadbuddy.telemetry.payload import PayloadError, TooManySpansError, prepare
from tests.support.otlp import SENTINEL, SPAN_ID, TRACE_ID, Json, export, span, string


def forwarded(body: bytes) -> Json:
    result: Json = json.loads(prepare(body))
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
            string("user_agent.original", "Mozilla/5.0"),
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
    assert SENTINEL not in prepare(body).decode()
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
    with pytest.raises(TooManySpansError):
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
    body = b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"name":"\\ud800"}]}]}]}'
    assert b"\\ud800" in prepare(body)
