"""The edge's per-client request limit (#5282, spec 2026-11-02 §5.8, plan point 15).

Each client (`client_key`: an IPv4 address, or the /64 of an IPv6 one, since one host
is given a whole /64 and can rotate through it) may make `READ_LIMIT` reads and
`WRITE_LIMIT` writes under the prefix in any `WINDOW` seconds: a sliding window of the
request times. One more is 429 ``…/storefront/rate_limited`` with ``Retry-After``, the
whole seconds until the oldest counted request leaves the window. A refused request is
not counted.

The counts are in memory, per replica. The render limits, which cost money, are counted
in Postgres by the CRUD side (spec §5.4). A client with no request in the last window is
forgotten at the next sweep, so the memory follows the clients of the last minute. The
table holds at most `MAX_CLIENTS`: when it is full, the idle clients are swept at once,
and if it is still full a new client gets the same 429. A live client's history is
never dropped to make room, so no flood of new addresses can reset anyone's count.
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Final

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE
from scadbuddy.core.proxies import Network, client_address, plain_address
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS

WINDOW: Final = 60.0
READ_LIMIT: Final = 120
WRITE_LIMIT: Final = 30
#: The most clients the table holds.
MAX_CLIENTS: Final = 50_000
#: An IPv6 client is its /64.
IPV6_PREFIX: Final = 64
#: The shortest wait between two sweeps of a full table, so a flood of new clients does
#: not walk the whole table on each request.
FULL_SWEEP_EVERY: Final = 1.0
RATE_LIMITED: Final = f"{STOREFRONT_PROBLEMS}rate_limited"
_READS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
#: The key of a request with no client address at all.
_UNKNOWN: Final = "unknown"


def client_key(address: str) -> str:
    """The key a client is counted under: an IPv4 address whole (an IPv4-mapped IPv6
    one too), an IPv6 address as its /64, and anything else as it is."""
    plain = plain_address(address.strip().removeprefix("[").removesuffix("]"))
    try:
        ip = ipaddress.ip_address(plain)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, IPV6_PREFIX), strict=False))
    return str(ip)


class _Client:
    """A client's request times; each deque is made on its first use."""

    __slots__ = ("reads", "writes")

    def __init__(self) -> None:
        self.reads: deque[float] | None = None
        self.writes: deque[float] | None = None

    def newest(self) -> float:
        return max(
            self.reads[-1] if self.reads else -math.inf,
            self.writes[-1] if self.writes else -math.inf,
        )


def _limited(seconds: int) -> JSONResponse:
    return JSONResponse(
        {
            "type": RATE_LIMITED,
            "title": "Too Many Requests",
            "status": 429,
            "detail": f"Too many requests. Try again in {seconds} seconds.",
        },
        status_code=429,
        headers={"Retry-After": str(seconds)},
        media_type=PROBLEM_MEDIA_TYPE,
    )


class RequestLimit:
    """ASGI middleware for the paths under ``prefix``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        prefix: str,
        trusted: Sequence[Network],
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = MAX_CLIENTS,
    ) -> None:
        self.app = app
        self._prefix = prefix
        self._trusted = trusted
        self._clock = clock
        self._max_clients = max_clients
        self._clients: dict[str, _Client] = {}
        self._next_sweep = clock() + WINDOW
        self._next_full_sweep = -math.inf

    def clients(self) -> set[str]:
        """The client keys counted now."""
        return set(self._clients)

    def history(self, address: str) -> tuple[int | None, int | None]:
        """How many reads and writes of ``address``'s client are kept; None for one never
        made."""
        entry = self._clients.get(client_key(address))
        if entry is None:
            return (None, None)
        return (
            None if entry.reads is None else len(entry.reads),
            None if entry.writes is None else len(entry.writes),
        )

    def note(self, address: str, *, write: bool) -> int | None:
        """Counts a request from ``address``; or, over the limit or with the table full,
        counts nothing and answers the seconds until it may try again."""
        now = self._clock()
        if now >= self._next_sweep:
            self._sweep(now)
        key = client_key(address)
        entry = self._clients.get(key)
        if entry is None:
            if len(self._clients) >= self._max_clients and now >= self._next_full_sweep:
                self._next_full_sweep = now + FULL_SWEEP_EVERY
                self._sweep(now)
            if len(self._clients) >= self._max_clients:
                return math.ceil(WINDOW)
            entry = self._clients[key] = _Client()
        times = entry.writes if write else entry.reads
        if times is None:
            times = deque()
            if write:
                entry.writes = times
            else:
                entry.reads = times
        limit = WRITE_LIMIT if write else READ_LIMIT
        while times and times[0] <= now - WINDOW:
            times.popleft()
        if len(times) >= limit:
            return max(1, math.ceil(times[0] + WINDOW - now))
        times.append(now)
        return None

    def _sweep(self, now: float) -> None:
        self._clients = {
            key: entry for key, entry in self._clients.items() if entry.newest() > now - WINDOW
        }
        self._next_sweep = now + WINDOW

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == self._prefix or path.startswith(self._prefix + "/")
        ):
            await self.app(scope, receive, send)
            return
        peer = scope["client"][0] if scope.get("client") else None
        forwarded = Headers(scope=scope).get("x-forwarded-for")
        address = client_address(peer, forwarded, self._trusted) or _UNKNOWN
        retry_after = self.note(address, write=scope["method"] not in _READS)
        if retry_after is None:
            await self.app(scope, receive, send)
            return
        await _limited(retry_after)(scope, receive, send)
