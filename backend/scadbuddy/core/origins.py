"""A browser's ``Origin``, compared to the origins a server takes writes or sockets from
(#962). It imports no database code: the storefront edge (#5282) uses it through
`api/cross_site.py` `CrossSiteGate`, and the API's realtime socket uses it too."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

#: As ``urlsplit(...).hostname`` gives them: an IPv6 literal without its brackets.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def normalised_origin(value: str) -> str | None:
    """``scheme://host[:port]``, lower-cased, default port dropped; None if not http(s)."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def is_loopback(origin: str | None) -> bool:
    normalised = None if origin is None else normalised_origin(origin)
    return normalised is not None and urlsplit(normalised).hostname in LOOPBACK_HOSTS


def origin_allowed(
    origin: str | None,
    public_url: str | None,
    allowed_origins: Iterable[str] = (),
    *,
    loopback: bool = True,
) -> bool:
    """Whether a request from ``origin`` may pass: none (not a browser page), the public
    URL's, one of ``allowed_origins``, or, with ``loopback``, a loopback origin."""
    if origin is None:
        return True
    normalised = normalised_origin(origin)
    if normalised is None:
        return False
    if loopback and is_loopback(normalised):
        return True
    candidates = [public_url, *allowed_origins]
    return any(
        candidate is not None and normalised == normalised_origin(candidate)
        for candidate in candidates
    )
