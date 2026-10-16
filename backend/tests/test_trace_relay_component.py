"""telemetry/component.py: the forwarder is built with the OTLP client options (#1160)."""

from __future__ import annotations

import ssl
from types import SimpleNamespace
from typing import cast

import pytest

from scadbuddy.core.components import Components, Core
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.settings import Settings
from scadbuddy.telemetry import component
from scadbuddy.telemetry.forwarder import FORWARD_TIMEOUT
from scadbuddy.telemetry.target import OtlpClientOptions
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS


def build() -> component.TraceRelay:
    settings = Settings(database_url=UNUSED_DATABASE_URL, temporal_address=UNUSED_TEMPORAL_ADDRESS)
    core = cast(Core, SimpleNamespace(metrics=Metrics(), settings=settings))
    return component._build(core, cast(Components, None))


def test_the_forwarder_takes_the_tls_context_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    context = ssl.create_default_context()
    monkeypatch.setattr(component, "otlp_traces_target", lambda: ("http://c/v1/traces", {}))
    monkeypatch.setattr(
        component,
        "otlp_traces_client_options",
        lambda: OtlpClientOptions(verify=context, timeout=2.5),
    )
    forwarder = build().forwarder
    assert forwarder._verify is context
    assert forwarder._forward_timeout == 2.5


def test_without_a_timeout_the_relays_own_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(component, "otlp_traces_target", lambda: ("http://c/v1/traces", {}))
    monkeypatch.setattr(component, "otlp_traces_client_options", lambda: OtlpClientOptions())
    assert build().forwarder._forward_timeout == FORWARD_TIMEOUT


def test_tracing_off_reads_no_tls_file(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable() -> OtlpClientOptions:
        raise AssertionError("read while tracing is off")

    monkeypatch.setattr(component, "otlp_traces_target", lambda: None)
    monkeypatch.setattr(component, "otlp_traces_client_options", unreadable)
    assert build().forwarder.off
