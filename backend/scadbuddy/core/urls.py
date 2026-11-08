"""URL checks shared by the API's settings and the storefront edge's (#4960)."""

from __future__ import annotations

from urllib.parse import urlsplit


def https_origin(value: str, variable: str) -> str:
    """``value`` when it is an ``https://`` origin with no path, query or fragment; a
    ``ValueError`` naming ``variable`` otherwise."""
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or parts.path or parts.query or parts.fragment:
        raise ValueError(f"{variable} must be an https:// origin with no path, not {value!r}")
    return value
