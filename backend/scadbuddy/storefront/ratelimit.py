"""The edge's per-client request limit (#5282, spec 2026-11-02 §5.8, plan point 15).

Each client (`client_key`: an IPv4 address, or the /64 of an IPv6 one, since one host
is given a whole /64 and can rotate through it) may make `READ_LIMIT` reads and
`WRITE_LIMIT` writes under the prefix in any `WINDOW` seconds: a sliding window of the
request times. One more is 429 ``…/storefront/rate_limited`` with ``Retry-After``, the
whole seconds until the oldest counted request leaves the window. A refused request is
not counted.

The counts are in memory, per replica. The render limits, which cost money, are counted
in Postgres by the CRUD side (spec §5.4). A client with no request in the last window is
forgotten at the next sweep, so the memory follows the clients of the last minute.

Two rules keep a flood of addresses from filling the table and locking others out:

- One IPv6 /48 (a usual allocation: 65,536 /64s) holds at most `MAX_PER_PREFIX` /64
  entries. A new /64 past those is counted under the /48 itself, so one allocation adds
  at most 65 entries, and its overflow shares one allowance.
- The table holds at most `MAX_CLIENTS`. When it is full, the idle clients are swept at
  once (at most once a second). If it is still full, a new client is counted in one
  shared bucket, `OVERFLOW`, with the same limits: limited, never locked out. A live
  client's history is never dropped to make room, so no flood can reset anyone's count.
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections import Counter, deque
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
#: Past `MAX_PER_PREFIX` /64s of one /48, the next ones share the /48's entry.
IPV6_ALLOCATION: Final = 48
MAX_PER_PREFIX: Final = 64
#: The key of the bucket new clients share while the table is full.
OVERFLOW: Final = "overflow"
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


def _allocation(key: str) -> str | None:
    """The /48 of an IPv6 /64 key (`client_key`); None for any other key."""
    if not key.endswith(f"/{IPV6_PREFIX}"):
        return None
    network = ipaddress.ip_network(key)
    return str(network.supernet(new_prefix=IPV6_ALLOCATION))


class _Client:
    """A client's request times; each deque is made on its first use. ``allocation``
    is the /48 of a /64 entry, which it counts against."""

    __slots__ = ("allocation", "reads", "writes")

    def __init__(self, allocation: str | None = None) -> None:
        self.allocation = allocation
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
        #: How many /64 entries each /48 holds.
        self._per_allocation: Counter[str] = Counter()
        self._next_sweep = clock() + WINDOW
        self._next_full_sweep = -math.inf

    def clients(self) -> set[str]:
        """The client keys counted now."""
        return set(self._clients)

    def history(self, address: str) -> tuple[int | None, int | None]:
        """How many reads and writes are kept of the entry ``address`` counts under now;
        None for one never made."""
        entry = self._clients.get(self._key(client_key(address)))
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
        key = self._key(client_key(address))
        entry = self._clients.get(key)
        if entry is None:
            if len(self._clients) >= self._max_clients and now >= self._next_full_sweep:
                self._next_full_sweep = now + FULL_SWEEP_EVERY
                self._sweep(now)
            if len(self._clients) >= self._max_clients:
                key = OVERFLOW
                entry = self._clients.get(key)
        if entry is None:
            allocation = _allocation(key)
            entry = self._clients[key] = _Client(allocation)
            if allocation is not None:
                self._per_allocation[allocation] += 1
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

    def _key(self, key: str) -> str:
        """``key``, or its /48 when it is a new /64 of a /48 that holds `MAX_PER_PREFIX`."""
        if key in self._clients:
            return key
        allocation = _allocation(key)
        if allocation is not None and self._per_allocation[allocation] >= MAX_PER_PREFIX:
            return allocation
        return key

    def _sweep(self, now: float) -> None:
        self._clients = {
            key: entry for key, entry in self._clients.items() if entry.newest() > now - WINDOW
        }
        self._per_allocation = Counter(
            entry.allocation for entry in self._clients.values() if entry.allocation is not None
        )
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
