# backend/scadbuddy/telemetry/admission.py
"""Who may post browser spans to the relay, and how often (spec 2026-10-01 §5.2).

**Same origin only.** A page on another origin must not drive the relay through a LAN
user's browser, to spend its budget or inject spans. In order, before the body is read:

1. no ``Origin`` is refused: a browser always sends one on a ``fetch`` POST, so its
   absence means the caller is not this page (`origin_allowed` answers True for None
   on purpose, for the realtime socket, so this is a check of its own);
2. `origin_allowed` (`api/realtime.py`: the public URL, ``SCADBUDDY_ALLOWED_ORIGINS``
   and loopback) must accept the ``Origin`` that is present;
3. a ``Sec-Fetch-Site`` that is present must be ``same-origin``.

The ``Origin`` check is the one that matters: a cross-origin page can skip the
preflight (``mode: 'no-cors'`` with a ``text/plain`` body), and that request still
carries its foreign ``Origin``. The route sends no CORS headers and answers no
preflight, but that is not relied on to stop a request. Every other write gets the
``Origin`` check alone (`api/cross_site.py`, #962), which lets a request with none
through; the relay is stricter.

**Rate limits**, in memory: a per-process bucket caps what one pod sends the collector
whatever the client, and a per-client bucket sits under it. The client is the peer,
unless the peer is in ``SCADBUDDY_TRUSTED_PROXIES`` (`core/proxies.py`). An IPv6 client
is its /64 (`bucket_key`): one host may rotate its address within it, by privacy
extensions or on purpose, and would otherwise get a fresh bucket each time (#1152). The
agent keys its own limits by the exact address; who the client *is*
(`core/proxies.py`) is the same in both.
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Final

from starlette.datastructures import Headers

from scadbuddy.api.realtime import RateLimit, origin_allowed
from scadbuddy.core.problems import ApiError
from scadbuddy.core.proxies import Network, client_address
from scadbuddy.core.settings import Settings

#: Per process, so per pod rather than cluster-wide: with N API replicas the ceiling is
#: N times this. The API runs one replica (2026-10-01, ``replicas: 1`` in
#: eh-homelab/clusters ``applications/scadbuddy/scadbuddy.yaml``); a change there has to
#: adjust these. A bucket shared in Postgres would cost a write per batch on a path whose
#: only job is to be cheap.
PROCESS_BURST: Final = 100
PROCESS_PER_SECOND: Final = 20.0
#: A page's exporter sends about one batch every 5 s (frontend `RelayExporter`).
CLIENT_BURST: Final = 20
CLIENT_PER_SECOND: Final = 2.0
#: Clients with a bucket of their own; the least recently seen is forgotten past it.
MAX_TRACKED_CLIENTS: Final = 4096
#: The bucket of a request whose peer is unknown.
UNKNOWN_CLIENT: Final = "unknown"


def _forbidden(detail: str) -> ApiError:
    return ApiError(403, detail, title="Forbidden")


def check_origin(headers: Headers, settings: Settings) -> None:
    """Refuse (403) a request that is not from one of ScadBuddy's own pages."""
    origin = headers.get("origin")
    if origin is None:
        raise _forbidden("the relay accepts requests from ScadBuddy's own pages, which send Origin")
    if not origin_allowed(origin, settings.public_url, settings.allowed_origin_list):
        raise _forbidden("Origin not allowed")
    site = headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() != "same-origin":
        raise _forbidden("Sec-Fetch-Site must be same-origin")


def check_content_type(headers: Headers) -> None:
    """Refuse (415) anything but OTLP/JSON, the browser exporter's format."""
    kind = headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != "application/json":
        raise ApiError(415, "the relay accepts application/json only")


def relay_client(headers: Headers, peer: str | None, trusted_proxies: tuple[Network, ...]) -> str:
    """The client a rate limit counts against. Every ``X-Forwarded-For`` line, joined as
    Node joins them for the agent, so a proxy that sends two gives the same answer."""
    forwarded_for = ", ".join(headers.getlist("x-forwarded-for")) or None
    return client_address(peer, forwarded_for, trusted_proxies) or UNKNOWN_CLIENT


def bucket_key(client: str) -> str:
    """The rate-limit bucket of ``client``: an IPv6 address's /64, an IPv4-mapped one's
    IPv4 address; any other value as is."""
    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return client
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return client


def _too_many(detail: str, limit: RateLimit) -> ApiError:
    seconds = max(1, math.ceil(limit.retry_after()))
    return ApiError(429, detail, title="Too Many Requests", headers={"Retry-After": str(seconds)})


class RelayLimits:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        process_burst: int = PROCESS_BURST,
        process_per_second: float = PROCESS_PER_SECOND,
        client_burst: int = CLIENT_BURST,
        client_per_second: float = CLIENT_PER_SECOND,
        max_clients: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        self._clock = clock
        self._process = RateLimit(process_burst, process_per_second, clock)
        self._client_burst = client_burst
        self._client_per_second = client_per_second
        self._max_clients = max_clients
        self._clients: OrderedDict[str, RateLimit] = OrderedDict()

    @property
    def tracked_clients(self) -> int:
        return len(self._clients)

    def _client(self, client: str) -> RateLimit:
        limit = self._clients.get(client)
        if limit is not None:
            self._clients.move_to_end(client)
            return limit
        limit = RateLimit(self._client_burst, self._client_per_second, self._clock)
        self._clients[client] = limit
        if len(self._clients) > self._max_clients:
            self._clients.popitem(last=False)
        return limit

    def take(self, client: str) -> None:
        """One batch from ``client``, or a 429 whose ``Retry-After`` is the seconds until
        the bucket that refused it holds one again."""
        limit = self._client(bucket_key(client))
        # Check both, then take from both: a refusal by the process bucket must not
        # spend the client's own token. ``retry_after() > 0`` is "no token now".
        if limit.retry_after() > 0:
            raise _too_many("this client is over the relay's rate limit", limit)
        if self._process.retry_after() > 0:
            raise _too_many("the relay is over its overall rate limit", self._process)
        limit.take()
        self._process.take()


__all__ = [
    "CLIENT_BURST",
    "CLIENT_PER_SECOND",
    "MAX_TRACKED_CLIENTS",
    "PROCESS_BURST",
    "PROCESS_PER_SECOND",
    "RelayLimits",
    "bucket_key",
    "check_content_type",
    "check_origin",
    "relay_client",
]
