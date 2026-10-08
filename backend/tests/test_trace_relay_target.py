"""telemetry/target.py: where the relay posts and the headers it sends (spec 2026-10-01
§5.2), by the SDK's precedence."""

from __future__ import annotations

import ssl
from typing import cast

import pytest

from scadbuddy.telemetry.target import (
    OtlpClientOptions,
    otlp_traces_client_options,
    otlp_traces_target,
)

_OTEL_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_TRACES_EXPORTER",
    "OTEL_SDK_DISABLED",
    "OTEL_EXPORTER_OTLP_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_CLIENT_KEY",
    "OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY",
    "OTEL_EXPORTER_OTLP_TIMEOUT",
    "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
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


class _Context:
    """Stands in for `ssl.create_default_context`'s result, recording what it loads."""

    def __init__(self, cafile: str | None) -> None:
        self.cafile = cafile
        self.chain: tuple[str, str | None] | None = None

    def load_cert_chain(self, certfile: str, keyfile: str | None = None) -> None:
        self.chain = (certfile, keyfile)


@pytest.fixture
def contexts(monkeypatch: pytest.MonkeyPatch) -> list[_Context]:
    made: list[_Context] = []

    def create_default_context(*, cafile: str | None = None) -> _Context:
        made.append(_Context(cafile))
        return made[-1]

    monkeypatch.setattr(ssl, "create_default_context", create_default_context)
    _clear_otel(monkeypatch)
    return made


def test_no_tls_variable_keeps_the_default_verification(contexts: list[_Context]) -> None:
    assert otlp_traces_client_options() == OtlpClientOptions(verify=True, timeout=None)
    assert contexts == []


def test_the_ca_and_client_certificate_prefer_the_traces_variables(
    monkeypatch: pytest.MonkeyPatch, contexts: list[_Context]
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_CERTIFICATE", "/general/ca.pem")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", "/general/client.pem")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_CLIENT_KEY", "/general/client.key")
    options = otlp_traces_client_options()
    (context,) = contexts
    assert cast(object, options.verify) is context
    assert (context.cafile, context.chain) == (
        "/general/ca.pem",
        ("/general/client.pem", "/general/client.key"),
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE", "/t/ca.pem")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE", "/t/client.pem")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY", "/t/client.key")
    otlp_traces_client_options()
    assert (contexts[1].cafile, contexts[1].chain) == (
        "/t/ca.pem",
        ("/t/client.pem", "/t/client.key"),
    )


def test_a_client_certificate_without_a_key_is_loaded_alone(
    monkeypatch: pytest.MonkeyPatch, contexts: list[_Context]
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE", "/t/both.pem")
    otlp_traces_client_options()
    (context,) = contexts
    assert (context.cafile, context.chain) == (None, ("/t/both.pem", None))


def test_the_timeout_prefers_the_traces_variable_in_seconds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "7")
    assert otlp_traces_client_options().timeout == 7.0
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "2.5")
    assert otlp_traces_client_options().timeout == 2.5
    # As the SDK's exporter does: a value that is not a number is ignored, with a warning.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "soon")
    assert otlp_traces_client_options().timeout is None
    assert "timeout" in caplog.text
