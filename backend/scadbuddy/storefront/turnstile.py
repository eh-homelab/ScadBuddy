"""The edge's bot check (#5283, spec 2026-11-02 §5.8): Cloudflare Turnstile. Today only
design requests take it (action ``request``); ``submit``, ``sign_up`` and ``sign_in`` are
kept for the submit, code request and sign-in routes still to come.

The edge holds no Turnstile secret: the CRUD service keeps it sealed under its KEK and
calls Cloudflare's Siteverify itself (`orders/storefront/turnstile.py`). A route that
takes the check awaits `require_turnstile` before it reads its body or calls the CRUD
route that acts: it reads the widget's token from `TURNSTILE_HEADER` and sends it, with
the form's ``action``, to ``POST …/storefront/turnstile/verify``, which answers a verdict.

- ``off`` (no site key set) and ``pass`` let the request on.
- ``fail`` (no token, a refused, used or expired token, or one made for another form or
  host) is 403 ``…/storefront/bot_check_failed``: the page solves the widget again.
- ``unavailable`` (a site key with no secret the CRUD side can read, or Cloudflare not
  answering) is 503 ``…/storefront/bot_check_unavailable``. The check fails closed.

A token longer than Cloudflare's bound fails here, before any call, and so does a client
with `FAILED_CHECKS` failed checks in `FAILED_WINDOW`, or one of a prefix with
`PREFIX_FAILED_CHECKS` (`FailedChecks`). The verdict is asked once, never retried, with
`TURNSTILE_VERIFY_SECONDS` to answer; a transport failure or a timeout is 503 and is not
counted as a failed check. `SiteKey` is the CRUD side's site key, read at most once per
`SITE_KEY_TTL` (30 s) per replica: the page's widget (`GET /api/store/v1/turnstile`) and
the page's CSP (`static.py`) both read it, so the CSP names Cloudflare only while the
check is on. While a read within that time found no key, `require_turnstile` passes with
no call, so a key the shop sets takes effect within 30 s. Siteverify itself stays on the
CRUD side.
"""

from __future__ import annotations

import asyncio
import heapq
import ipaddress
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Final

from starlette.requests import Request

from scadbuddy.core.problems import ApiError
from scadbuddy.core.proxies import client_address, client_key
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.storefront import StorefrontCrud
from scadbuddy.storefront.wire import (
    MAX_TURNSTILE_TOKEN_CHARS,
    STOREFRONT_PROBLEMS,
    TURNSTILE_HEADER,
    TurnstileAction,
)

logger = logging.getLogger(__name__)

BOT_CHECK_FAILED: Final = f"{STOREFRONT_PROBLEMS}bot_check_failed"
BOT_CHECK_UNAVAILABLE: Final = f"{STOREFRONT_PROBLEMS}bot_check_unavailable"
#: How long one replica trusts its read of the site key. While a read found none, a check
#: passes with no call, so a key the shop sets takes effect within this time.
SITE_KEY_TTL: Final = 30.0
#: How long a failed read is kept: page loads meanwhile do not each wait for the CRUD side.
SITE_KEY_RETRY: Final = 30.0


def bot_check_failed() -> ApiError:
    return ApiError(
        403,
        "The check that you are not a robot did not pass. Try it again.",
        type_=BOT_CHECK_FAILED,
    )


def bot_check_unavailable() -> ApiError:
    return ApiError(
        503,
        "The check that you are not a robot is not working now. Try again later.",
        type_=BOT_CHECK_UNAVAILABLE,
    )


def _client(request: Request) -> str:
    """The browser's address, as `routes.py` finds it for every visitor route."""
    trusted = request.app.state.trusted_proxies
    peer = request.client.host if request.client else None
    address = client_address(peer, request.headers.get("x-forwarded-for"), trusted)
    if address is None:
        raise ApiError(400, "The request has no client address.")
    return address


#: Failed checks one client (`client_key`: an IPv4 address, an IPv6 /64) may have in
#: `FAILED_WINDOW` before the edge stops asking Siteverify for it.
FAILED_CHECKS: Final = 10
#: Failed checks one prefix (an IPv4 /24, an IPv6 /48) may have in `FAILED_WINDOW`.
PREFIX_FAILED_CHECKS: Final = 100
FAILED_WINDOW: Final = 600.0
#: The most clients, and the most prefixes, `FailedChecks` tracks.
MAX_FAILING_CLIENTS: Final = 50_000
#: How often a full table of blocked entries is logged.
FULL_WARNING_EVERY: Final = 60.0


def _prefix(client: str) -> str:
    """``client``'s IPv4 /24 or IPv6 /48; anything else as it is."""
    try:
        ip = ipaddress.ip_address(client_key(client).split("/")[0])
    except ValueError:
        return client
    bits = 24 if isinstance(ip, ipaddress.IPv4Address) else 48
    return str(ipaddress.ip_network((ip, bits), strict=False))


class _Tier:
    """One key's failures: the unblocked keys in an LRU (least recently failed first),
    the blocked ones apart with the time their block ends, so eviction is O(1) and never
    drops a block. A heap of block ends sweeps every expired block, in order of expiry."""

    def __init__(self, most: int, size: int) -> None:
        self.most = most
        self.size = size
        self.open: OrderedDict[str, deque[float]] = OrderedDict()
        self.blocked: dict[str, float] = {}
        self._ends: list[tuple[float, str]] = []

    def _sweep(self, now: float) -> None:
        while self._ends and self._ends[0][0] <= now:
            until, key = heapq.heappop(self._ends)
            # A key blocked again since has a later end of its own in the heap.
            if self.blocked.get(key) == until:
                del self.blocked[key]

    def is_blocked(self, key: str, now: float) -> bool:
        until = self.blocked.get(key)
        if until is None:
            return False
        if until > now:
            return True
        del self.blocked[key]
        return False

    def fail(self, key: str, now: float) -> bool:
        """Count a failure of ``key``; False when the table is full of blocked keys and
        ``key`` is not tracked."""
        self._sweep(now)
        if self.is_blocked(key, now):
            return True
        failures = self.open.get(key)
        if failures is None:
            if len(self.open) + len(self.blocked) >= self.size:
                if not self.open:
                    return False
                self.open.popitem(last=False)
            failures = self.open[key] = deque()
        else:
            self.open.move_to_end(key)
        while failures and failures[0] <= now - FAILED_WINDOW:
            failures.popleft()
        failures.append(now)
        if len(failures) >= self.most:
            # A blocked key is never asked about, so it gets no newer failures: its block
            # ends when the oldest failure that makes it ages out.
            until = failures[-self.most] + FAILED_WINDOW
            self.blocked[key] = until
            heapq.heappush(self._ends, (until, key))
            del self.open[key]
        return True


class FailedChecks:
    """Failed bot checks, so the edge stops calling Siteverify for whoever keeps failing.
    One per edge replica.

    Two tiers. A client (`core/proxies.py` `client_key`: an IPv4 address, an IPv6 /64,
    since a host owns at least a /64 and may rotate inside it) with `FAILED_CHECKS`
    failures in `FAILED_WINDOW` is refused. So is every client of a prefix (an IPv4 /24,
    an IPv6 /48) with `PREFIX_FAILED_CHECKS` failures in the window, so rotating across
    /64s does not escape the bound either. The price of that bound: honest neighbours in
    the prefix are refused too, but only after 100 failures in 10 minutes from it. A
    refusal is 403 ``bot_check_failed`` with no Siteverify call.

    Each tier tracks at most `MAX_FAILING_CLIENTS` keys. A new key evicts the least
    recently failed unblocked key; a blocked key is never evicted. When every tracked key
    is blocked, a new one is checked as usual but not tracked, and a warning is logged at
    most once per `FULL_WARNING_EVERY`: a Siteverify call costs little, and refusing
    unknown clients would let an attacker shut every customer out."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        size: int = MAX_FAILING_CLIENTS,
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._clients = _Tier(FAILED_CHECKS, size)
        self._prefixes = _Tier(PREFIX_FAILED_CHECKS, size)
        self._warned_at: float | None = None

    def blocked(self, client: str) -> bool:
        now = self._clock()
        with self._lock:
            return self._clients.is_blocked(client_key(client), now) or (
                self._prefixes.is_blocked(_prefix(client), now)
            )

    def failed(self, client: str) -> None:
        now = self._clock()
        with self._lock:
            tracked = self._clients.fail(client_key(client), now)
            tracked = self._prefixes.fail(_prefix(client), now) and tracked
            if not tracked and (
                self._warned_at is None or now - self._warned_at >= FULL_WARNING_EVERY
            ):
                self._warned_at = now
                logger.warning(
                    "the failed bot check table is full of blocked clients;"
                    " new failing clients are checked but not tracked"
                )


async def require_turnstile(request: Request, action: TurnstileAction) -> None:
    """Passes when the check is off or the browser's token passes; raises 403
    `BOT_CHECK_FAILED` or 503 `BOT_CHECK_UNAVAILABLE` otherwise (module docstring). A
    client past `FAILED_CHECKS` (`FailedChecks`) is refused 403 with no call, and while
    `SiteKey` found no key within `SITE_KEY_TTL` it passes with no call at all."""
    site_key: SiteKey = request.app.state.turnstile_site_key
    if site_key.known_off():
        # Off: nothing is refused, whatever the browser sent.
        return
    token = request.headers.get(TURNSTILE_HEADER) or None
    if token is not None and len(token) > MAX_TURNSTILE_TOKEN_CHARS:
        raise bot_check_failed()
    client = _client(request)
    failures: FailedChecks = request.app.state.turnstile_failures
    if failures.blocked(client):
        raise bot_check_failed()
    crud: StorefrontCrud = request.app.state.crud
    try:
        verdict = await crud.turnstile_verify(
            token, action, visitor_id=request.state.visitor_id, client=client
        )
    except CrudUnavailableError:
        # No answer is not a refusal: never counted as a failed check.
        raise bot_check_unavailable() from None
    if verdict in ("off", "pass"):
        return
    if verdict == "fail":
        # A send with no token cost no Siteverify call, so it is not counted.
        if token is not None:
            failures.failed(client)
        raise bot_check_failed()
    if verdict != "unavailable":
        logger.error("the CRUD service gave an unknown bot check verdict")
    raise bot_check_unavailable()


class SiteKey:
    """The CRUD side's Turnstile site key, or None while the check is off, read at most
    once per `SITE_KEY_TTL`. A read that fails keeps the last value (None before the
    first), so a page still loads while the CRUD service is away; with the check on and
    the key not yet read, the page cannot make a token, and the check then refuses. A
    failed read is not tried again for `SITE_KEY_RETRY`, so while the CRUD service is away
    each page load does not wait out the read's budget."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._read: tuple[float, str | None] | None = None
        self._failed_at: float | None = None
        # Page loads at once after the TTL lapses share one read.
        self._reading = asyncio.Lock()

    def _last(self) -> str | None:
        return None if self._read is None else self._read[1]

    def _cached(self, now: float) -> tuple[bool, str | None]:
        if self._read is not None and now - self._read[0] < SITE_KEY_TTL:
            return True, self._read[1]
        if self._failed_at is not None and now - self._failed_at < SITE_KEY_RETRY:
            return True, self._last()
        return False, None

    async def get(self, crud: StorefrontCrud) -> str | None:
        hit, key = self._cached(self._clock())
        if hit:
            return key
        async with self._reading:
            # Another load may have read it while this one waited.
            now = self._clock()
            hit, key = self._cached(now)
            if hit:
                return key
            return await self._fetch(crud, now)

    async def _fetch(self, crud: StorefrontCrud, now: float) -> str | None:
        try:
            key = await crud.turnstile_site_key()
        except (CrudUnavailableError, CrudRefusedError) as error:
            logger.warning("could not read the bot check's site key (%s)", type(error).__name__)
            self._failed_at = now
            return self._last()
        self._read = (now, key)
        self._failed_at = None
        return key

    @property
    def ever_read(self) -> bool:
        """Whether any read has succeeded since the edge started."""
        return self._read is not None

    def known_off(self) -> bool:
        """Whether a read within `SITE_KEY_TTL` found no site key: the check is off."""
        read = self._read
        return read is not None and read[1] is None and self._clock() - read[0] < SITE_KEY_TTL
