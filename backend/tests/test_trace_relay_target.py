"""telemetry/target.py: where the relay posts and the headers it sends (spec 2026-10-01
§5.2), by the SDK's precedence."""

from __future__ import annotations

import pytest

from scadbuddy.telemetry.target import otlp_traces_target

_OTEL_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_TRACES_EXPORTER",
    "OTEL_SDK_DISABLED",
)


def _clear_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _OTEL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_otel_traces_exporter_other_than_otlp_is_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    for value in ("none", "NONE", "console", "zipkin,jaeger"):
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", value)
        assert otlp_traces_target() is None
    for value in ("otlp", " OTLP ", "console, otlp"):
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", value)
        assert otlp_traces_target() == ("http://collector:4318/v1/traces", {})


def test_target_is_none_without_an_endpoint_or_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_otel(monkeypatch)
    assert otlp_traces_target() is None
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    assert otlp_traces_target() is None
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert otlp_traces_target() is None


def test_target_appends_v1_traces_with_one_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4318")
    assert otlp_traces_target() == ("http://c:4318/v1/traces", {})
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4318/")
    assert otlp_traces_target() == ("http://c:4318/v1/traces", {})


def test_the_traces_endpoint_wins_and_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://general:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://t:4318/custom/")
    assert otlp_traces_target() == ("http://t:4318/custom/", {})


def test_target_headers_prefer_the_traces_variable_and_are_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://c:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "general=1")
    target = otlp_traces_target()
    assert target is not None and target[1] == {"general": "1"}
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        " Authorization = Basic%20abc%3D%3D , bad, =nokey, x-k=a=b,%6Bey=v%2Cw",
    )
    target = otlp_traces_target()
    assert target is not None
    assert target[1] == {"Authorization": "Basic abc==", "x-k": "a=b", "key": "v,w"}
