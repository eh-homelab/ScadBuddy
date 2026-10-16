"""OTLP/JSON trace exports as a browser's exporter writes them, for the relay's tests."""

from __future__ import annotations

import json
from typing import Any

type Json = dict[str, Any]

#: Put where a value must never reach the collector; asserted absent from what is sent.
SENTINEL = "SENTINEL-7f3a9c"
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"


def string(key: str, value: str) -> Json:
    return {"key": key, "value": {"stringValue": value}}


def span(**fields: Any) -> Json:
    return {
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
        "name": "Generate",
        "kind": 1,
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000100000000",
        "attributes": [],
        "events": [],
        "links": [],
        "status": {"code": 0},
        **fields,
    }


def export(*spans: Json, resource: list[Json] | None = None) -> bytes:
    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {"attributes": resource or []},
                    "scopeSpans": [{"scope": {"name": "scadbuddy-web"}, "spans": list(spans)}],
                }
            ]
        }
    ).encode()
