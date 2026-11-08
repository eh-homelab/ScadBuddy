"""The edge's per-client request limit (#5282, spec 2026-11-02 §5.8, plan point 15).

Each client address (`core/proxies.py` `client_address`, as the routes resolve it) may
make `READ_LIMIT` reads and `WRITE_LIMIT` writes under the prefix in any `WINDOW`
seconds: a sliding window of the request times. One more is 429
``…/storefront/rate_limited`` with ``Retry-After``, the whole seconds until the oldest
counted request leaves the window. A refused request is not counted.

The counts are in memory, per replica. The render limits, which cost money, are counted
in Postgres by the CRUD side (spec §5.4). A client with no request in the last window is
forgotten at the next sweep, so the memory follows the clients of the last minute.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Final

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE
from scadbuddy.core.proxies import Network, client_address
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS

WINDOW: Final = 60.0
READ_LIMIT: Final = 120
WRITE_LIMIT: Final = 30
RATE_LIMITED: Final = f"{STOREFRONT_PROBLEMS}rate_limited"
_READS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
#: The key of a request with no client address at all.
_UNKNOWN: Final = "unknown"


class _Client:
    __slots__ = ("reads", "writes")

    def __init__(self) -> None:
        self.reads: deque[float] = deque()
        self.writes: deque[float] = deque()

    def newest(self) -> float:
        return max(
            self.reads[-1] if self.reads else -math.inf,
            self.writes[-1] if self.writes else -math.inf,
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
    ) -> None:
        self.app = app
        self._prefix = prefix
        self._trusted = trusted
        self._clock = clock
        self._clients: dict[str, _Client] = {}
        self._next_sweep = clock() + WINDOW

    def clients(self) -> set[str]:
        """The client addresses counted now."""
        return set(self._clients)

    def note(self, client: str, *, write: bool) -> int | None:
        """Counts a request of ``client``; or, over the limit, counts nothing and answers
        the seconds until it may try again."""
        now = self._clock()
        if now >= self._next_sweep:
            self._sweep(now)
        entry = self._clients.setdefault(client, _Client())
        times = entry.writes if write else entry.reads
        limit = WRITE_LIMIT if write else READ_LIMIT
        while times and times[0] <= now - WINDOW:
            times.popleft()
        if len(times) >= limit:
            return max(1, math.ceil(times[0] + WINDOW - now))
        times.append(now)
        return None

    def _sweep(self, now: float) -> None:
        self._clients = {
            client: entry
            for client, entry in self._clients.items()
            if entry.newest() > now - WINDOW
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
        client = client_address(peer, forwarded, self._trusted) or _UNKNOWN
        retry_after = self.note(client, write=scope["method"] not in _READS)
        if retry_after is None:
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {
                "type": RATE_LIMITED,
                "title": "Too Many Requests",
                "status": 429,
                "detail": f"Too many requests. Try again in {retry_after} seconds.",
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            media_type=PROBLEM_MEDIA_TYPE,
        )
        await response(scope, receive, send)
