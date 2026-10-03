"""telemetry/payload.py: what the relay forwards of a page's spans (spec 2026-10-01 §5.2, §6)."""

from __future__ import annotations

import json

import pytest

from scadbuddy.telemetry import payload
from scadbuddy.telemetry.payload import PayloadError, TooManySpansError, prepare
from tests.support.otlp import SENTINEL, SPAN_ID, TRACE_ID, Json, export, span, string


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
    except (PayloadError, TooManySpansError):
        return
    if out is not None:
        json.loads(out)


def test_more_than_16_resource_spans_are_refused() -> None:
    body = json.dumps({"resourceSpans": [{} for _ in range(17)]}).encode()
    with pytest.raises(PayloadError):
        prepare(body)


def test_more_than_64_scope_spans_in_total_are_refused() -> None:
    scopes: list[Json] = [{} for _ in range(33)]
    body = json.dumps({"resourceSpans": [{"scopeSpans": scopes}, {"scopeSpans": scopes}]}).encode()
    with pytest.raises(PayloadError):
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
    stack = "Error: a\n    at SECRET:1:2\n    at real (http://h/a.js:1:2)"
    event = {
        "name": "exception",
        "attributes": [
            string("exception.message", message),
            string("exception.stacktrace", stack),
        ],
    }
    (scrubbed,) = only_span(export(span(events=[event])))["events"]
    assert scrubbed["attributes"] == [string("exception.stacktrace", "at real (http://h/a.js:1:2)")]
