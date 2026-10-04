"""Where the browser trace relay posts (spec 2026-10-01 §5.2): the collector's URL and
the headers sent, resolved here from the same variables the SDK's OTLP/HTTP exporter
reads, by its precedence. The relay posts with its own client, not the exporter, so it
needs the URL and headers themselves, which the backend's provider never does."""

from __future__ import annotations

import os
from urllib.parse import unquote

from scadbuddy.core.tracing import traces_export_enabled


def otlp_traces_target() -> tuple[str, dict[str, str]] | None:
    """Where traces go and the headers sent, by the SDK's precedence, or ``None`` when
    the backend would not export either (`traces_export_enabled`: no endpoint,
    ``OTEL_SDK_DISABLED``, or ``OTEL_TRACES_EXPORTER`` naming anything but ``otlp``).
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` is used as is; ``OTEL_EXPORTER_OTLP_ENDPOINT``
    gets ``/v1/traces``."""
    if not traces_export_enabled():
        return None
    traces = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    general = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if traces:
        url = traces
    elif general:
        url = general.rstrip("/") + "/v1/traces"
    else:
        return None
    raw = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or os.environ.get(
        "OTEL_EXPORTER_OTLP_HEADERS", ""
    )
    return url, _parse_headers(raw)


def _parse_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for entry in raw.split(","):
        name, separator, value = entry.partition("=")
        name = unquote(name.strip())
        if separator and name:
            headers[name] = unquote(value.strip())
    return headers


__all__ = ["otlp_traces_target"]
