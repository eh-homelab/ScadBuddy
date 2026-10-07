"""Where the browser trace relay posts (spec 2026-10-01 §5.2): the collector's URL, the
headers sent, the TLS files and the timeout, resolved here from the same variables the
SDK's OTLP/HTTP exporter reads, by its precedence (the ``_TRACES_`` one first). The relay
posts with its own client, not the exporter, so it needs them itself, which the
backend's provider never does. Compression is not read: the relay never compresses."""

from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass
from urllib.parse import unquote

from scadbuddy.core.tracing import traces_export_enabled

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class OtlpClientOptions:
    #: An `ssl.SSLContext` with the CA and client certificate, or True (the defaults).
    verify: ssl.SSLContext | bool = True
    #: Seconds, as the Python SDK reads it; None when unset.
    timeout: float | None = None


def _variable(name: str) -> str | None:
    return os.environ.get(f"OTEL_EXPORTER_OTLP_TRACES_{name}") or os.environ.get(
        f"OTEL_EXPORTER_OTLP_{name}"
    )


def otlp_traces_client_options() -> OtlpClientOptions:
    """``OTEL_EXPORTER_OTLP_(TRACES_)CERTIFICATE`` (the CA file),
    ``…CLIENT_CERTIFICATE`` and ``…CLIENT_KEY`` (mTLS; a certificate without a key holds
    both) and ``…TIMEOUT``, as the SDK's exporter applies them (#1160). A file that
    cannot be read stops the start, as a malformed ``SCADBUDDY_TRUSTED_PROXIES`` does;
    a timeout that is not a number is ignored with a warning, as the SDK does."""
    ca_file = _variable("CERTIFICATE")
    client_certificate = _variable("CLIENT_CERTIFICATE")
    verify: ssl.SSLContext | bool = True
    if ca_file or client_certificate:
        verify = ssl.create_default_context(cafile=ca_file)
        if client_certificate:
            verify.load_cert_chain(client_certificate, _variable("CLIENT_KEY"))
    raw = _variable("TIMEOUT")
    timeout = None
    if raw:
        try:
            timeout = float(raw)
        except ValueError:
            logger.warning("ignoring an OTLP timeout that is not a number of seconds")
    return OtlpClientOptions(verify=verify, timeout=timeout)


__all__ = ["OtlpClientOptions", "otlp_traces_client_options", "otlp_traces_target"]
