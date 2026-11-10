"""A caller's service credential, without the database (#5298, spec 2026-11-05 §7.2).

The part of `service_credentials.py` that a process with no database needs: the token
source (:class:`ExchangeReader`, an exchange of a proof file) and the httpx auth that
sends it. It imports no Postgres code, so the storefront edge (#5282) can use it.
`service_credentials.py` re-exports every name here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from collections.abc import AsyncGenerator, Callable, Generator, Mapping
from pathlib import Path
from typing import Literal, Protocol

import httpx

logger = logging.getLogger(__name__)

ServiceCaller = Literal["agent", "print", "projects", "render", "storefront"]

_TOKEN_RE = re.compile(r"sbsvc_([0-9a-f]{16})\.([A-Za-z0-9_-]{43})")


def token_hash(token: str) -> bytes | None:
    if _TOKEN_RE.fullmatch(token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).digest()


class ExchangeSettings(Protocol):
    """The settings `exchange_source` reads: `Settings` and the storefront's own have them."""

    @property
    def service_token_file(self) -> Path | None: ...

    @property
    def service_bootstrap_file(self) -> Path | None: ...

    @property
    def api_internal_url(self) -> str | None: ...


class TokenSource(Protocol):
    """Where a caller's current credential comes from: ``token`` is called on a worker
    thread before each request, ``invalidate`` with a token the API answered 401, or 403
    ``out_of_scope``, to."""

    def token(self) -> str | None: ...

    def invalidate(self, token: str) -> None: ...


#: The exchange route, under the API's base URL.
EXCHANGE_PATH = "/api/v1/auth/service-token"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
#: The ``subject_token_type`` of each proof a caller can hold (spec 2026-11-05 §6.2).
PROOF_TOKEN_TYPES: Mapping[str, str] = {
    "k8s": "urn:ietf:params:oauth:token-type:jwt",
    "bootstrap": "urn:scadbuddy:params:token-type:bootstrap",
}
#: §6.3: a failed exchange is tried again after 5 s, doubling to 60 s.
EXCHANGE_RETRY_FIRST = 5.0
EXCHANGE_RETRY_MAX = 60.0
#: An old API pod answers 404 (§8 step 1): the view serves, and the exchange is tried
#: again this much later.
FALLBACK_RETRY = 60.0
#: A 401 exchanges again at once, but not sooner than this after the last exchange, so
#: a route that refuses the credential cannot turn every request into an exchange.
REEXCHANGE_MIN = 10.0
EXCHANGE_TIMEOUT = 10.0


class ExchangeReader:
    """A caller's credential from the exchange (spec 2026-11-05 §6.3, §8 step 1): the
    proof file is read again before each exchange (the kubelet rotates a projected
    token), the credential is renewed at half-life and at once after a 401, and a failed
    exchange keeps the current one until it expires, retrying after 5 s doubling to 60 s.
    An API without the route (404, an old pod during the roll) hands over to
    ``fallback``, the caller's view, while it has one."""

    def __init__(
        self,
        caller: ServiceCaller,
        api_url: str,
        proof_file: Path,
        proof: Literal["k8s", "bootstrap"],
        *,
        fallback: TokenSource | None = None,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.caller = caller
        self._url = api_url.rstrip("/") + EXCHANGE_PATH
        self._proof_file = proof_file
        self._token_type = PROOF_TOKEN_TYPES[proof]
        self._fallback = fallback
        self._clock = clock
        self._transport = transport
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._renew_at = 0.0
        self._next_try = 0.0
        self._delay = EXCHANGE_RETRY_FIRST
        self._exchanged_at: float | None = None
        self._on_fallback = False
        # One exchange at a time, run outside the lock: while it is in flight a caller
        # with a still-valid token gets it at once; only one without waits.
        self._exchanging = False
        self._exchanged = threading.Condition(self._lock)

    def _valid(self, now: float) -> bool:
        return self._token is not None and now < self._expires_at

    def token(self) -> str | None:
        with self._lock:
            now = self._clock()
            while self._exchanging and not self._valid(now):
                self._exchanged.wait()
                now = self._clock()
            if self._token is not None and now < self._renew_at:
                return self._token
            exchange = not self._exchanging and now >= self._next_try
            self._exchanging = self._exchanging or exchange
        if exchange:
            outcome: httpx.Response | str = "no answer"
            try:
                outcome = self._request()
            finally:
                with self._lock:
                    self._exchanging = False
                    self._apply(now, outcome)
                    self._exchanged.notify_all()
        with self._lock:
            if self._valid(now):
                return self._token
            on_fallback, fallback = self._on_fallback, self._fallback
        if on_fallback and fallback is not None:
            return fallback.token()
        return None

    def invalidate(self, token: str) -> None:
        with self._lock:
            if token != self._token:
                if self._on_fallback and self._fallback is not None:
                    self._fallback.invalidate(token)
                return
            self._token = None
            earliest = (self._exchanged_at or 0.0) + REEXCHANGE_MIN
            self._next_try = max(self._clock(), earliest)

    def _failed(self, now: float, reason: str) -> None:
        logger.warning("could not exchange the %s service credential (%s)", self.caller, reason)
        self._next_try = now + self._delay
        self._delay = min(self._delay * 2, EXCHANGE_RETRY_MAX)

    def _request(self) -> httpx.Response | str:
        """The exchange's answer, or why there is none. Outside the lock."""
        try:
            proof = self._proof_file.read_text().strip()
        except OSError as error:
            return f"no proof: {type(error).__name__}"
        if not proof:
            return "empty proof"
        form = {
            "grant_type": GRANT_TYPE,
            "subject_token": proof,
            "subject_token_type": self._token_type,
            # Never believed: when sent, the API refuses a proof of another caller.
            "client_id": self.caller,
        }
        try:
            with httpx.Client(transport=self._transport, timeout=EXCHANGE_TIMEOUT) as client:
                return client.post(self._url, data=form)
        except httpx.HTTPError as error:
            return type(error).__name__

    def _apply(self, now: float, response: httpx.Response | str) -> None:
        """Takes the exchange's answer in. Under the lock."""
        if isinstance(response, str):
            self._failed(now, response)
            return
        if response.status_code == 200:
            try:
                body = response.json()
                token = body["access_token"]
                lifetime = float(body["expires_in"])
            except (ValueError, KeyError, TypeError):
                self._failed(now, "an answer without a token")
                return
            if not isinstance(token, str) or token_hash(token) is None or lifetime <= 0:
                self._failed(now, "an answer without a token")
                return
            self._token = token
            self._exchanged_at = now
            self._expires_at = now + lifetime
            self._renew_at = now + lifetime / 2
            self._next_try = self._renew_at
            self._delay = EXCHANGE_RETRY_FIRST
            self._on_fallback = False
            return
        if response.status_code == 404 and self._fallback is not None:
            if not self._on_fallback:
                logger.info(
                    "the API has no service-token exchange yet; the %s credential is read"
                    " from its view",
                    self.caller,
                )
            self._on_fallback = True
            self._next_try = now + FALLBACK_RETRY
            return
        if response.status_code == 429:
            try:
                wait = max(1.0, float(response.headers.get("retry-after", "")))
            except ValueError:
                wait = self._delay
            logger.warning("the %s service-token exchange was rate limited", self.caller)
            self._next_try = now + wait
            return
        self._failed(now, f"HTTP {response.status_code}")


class ServiceCredentialConfigError(ValueError):
    """A caller's credential settings cannot be used: the process refuses to start."""


def exchange_source(caller: ServiceCaller, settings: ExchangeSettings) -> ExchangeReader:
    """The credential of a caller whose credential comes only from the exchange of
    ``SCADBUDDY_SERVICE_TOKEN_FILE`` or ``SCADBUDDY_SERVICE_BOOTSTRAP_FILE``, with no view
    to fall back to, whatever ``SCADBUDDY_DATABASE_URL`` says (#5298): the projects and
    render workers, which hold no database (and the render worker no KEK either), and the
    print worker whenever it has a proof."""
    proof: Literal["k8s", "bootstrap"]
    if settings.service_token_file is not None:
        proof_file, proof = settings.service_token_file, "k8s"
    elif settings.service_bootstrap_file is not None:
        proof_file, proof = settings.service_bootstrap_file, "bootstrap"
    else:
        raise ServiceCredentialConfigError(
            "SCADBUDDY_SERVICE_TOKEN_FILE (or SCADBUDDY_SERVICE_BOOTSTRAP_FILE outside"
            f" Kubernetes) is required for the {caller} service: it exchanges that proof for"
            " its service credential, with no view to fall back to (#5298)"
        )
    if settings.api_internal_url is None:
        raise ServiceCredentialConfigError(
            "SCADBUDDY_API_INTERNAL_URL is required to exchange the service credential"
        )
    return ExchangeReader(caller, settings.api_internal_url, proof_file, proof)


class ServiceAuth(httpx.Auth):
    """``Authorization: Bearer <token>`` on every request, when the source has one. A 401
    answer drops that token, so the next request carries a fresh one; the refused
    request is not sent again, since a write might then run twice."""

    def __init__(self, reader: TokenSource) -> None:
        self._reader = reader

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await asyncio.to_thread(self._reader.token)
        if token is not None:
            request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if token is not None and response.status_code == 401:
            await asyncio.to_thread(self._reader.invalidate, token)

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """The same, for a sync client (`crud/client.py` `CrudSyncClient`)."""
        token = self._reader.token()
        if token is not None:
            request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if token is not None and response.status_code == 401:
            self._reader.invalidate(token)

    def refresh(self, request: httpx.Request) -> None:
        """Drops the token ``request`` carried, so the next request exchanges for a new
        one: for a 403 that says the credential's scopes are stale (`crud/client.py`)."""
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            self._reader.invalidate(header.removeprefix("Bearer "))
