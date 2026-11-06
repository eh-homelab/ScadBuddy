"""The proofs a caller presents at ``POST /api/v1/auth/service-token`` (#5494, spec
2026-11-05 §6.1, §6.2, §7.2, §11 D1, D2).

In Kubernetes a caller presents its projected ServiceAccount token (audience
:data:`AUDIENCE`). :class:`TokenReviewVerifier` first checks it offline against the
cluster issuer's JWKS (signature RS256 or ES256, ``iss``, ``aud``, ``exp``/``nbf``, and a
``sub`` that names a ScadBuddy caller); only such a token is then sent to TokenReview,
with the API pod's own token, so a deleted pod's token is refused at once and the
credential is bound to the pod's name and uid. A forged or junk token is refused without
a TokenReview and spends nothing (review of #5498). Outside Kubernetes,
:class:`BootstrapVerifier` compares a per-caller secret with the hashes read at start
from ``SCADBUDDY_SERVICE_BOOTSTRAP_DIR``. Each verifier answers the caller it proved, or
``None``; one that cannot answer at all raises :class:`VerifierUnavailableError`.
Nothing here logs a presented token.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import os
import ssl
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from joserfc import errors as jose_errors
from joserfc import jwt
from joserfc.jwk import KeySet

from scadbuddy.auth.service_credentials import CALLERS, ServiceCaller

logger = logging.getLogger(__name__)

#: Per API process, at most this many TokenReviews a minute. Only a token the cluster
#: signed for a known caller spends it, so junk and forgeries cannot exhaust it.
REVIEW_BUDGET = 60
BUDGET_WINDOW = 60.0
#: The algorithms a cluster signs ServiceAccount tokens with; never ``none`` or HS*.
JWT_ALGORITHMS = ("RS256", "ES256")
#: Clock skew allowed on ``exp`` and ``nbf``.
LEEWAY_SECONDS = 30
#: The JWKS and the issuer are kept this long ...
JWKS_CACHE_SECONDS = 3600.0
#: ... and fetched again for an unknown ``kid`` at most this often.
JWKS_REFETCH_SECONDS = 30.0


class ReviewLimitedError(Exception):
    """The TokenReview budget is spent: ``retry_after`` seconds until one more."""

    def __init__(self, caller: ServiceCaller, retry_after: int) -> None:
        super().__init__(f"retry after {retry_after} s")
        self.caller = caller
        self.retry_after = retry_after


class ReviewBudget:
    """:data:`REVIEW_BUDGET` TokenReviews a minute, in this process's memory (one event
    loop, no lock)."""

    def __init__(
        self, *, budget: int = REVIEW_BUDGET, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._budget = budget
        self._clock = clock
        self._spent: deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._spent and now - self._spent[0] >= BUDGET_WINDOW:
            self._spent.popleft()

    @property
    def spent(self) -> int:
        self._trim(self._clock())
        return len(self._spent)

    def spend(self) -> int | None:
        """One TokenReview may go, or the seconds until one may."""
        now = self._clock()
        self._trim(now)
        if len(self._spent) >= self._budget:
            return max(1, math.ceil(self._spent[0] + BUDGET_WINDOW - now))
        self._spent.append(now)
        return None


#: The audience every caller's projected token is issued for, and the only one accepted.
AUDIENCE = "scadbuddy"

#: How the caller proved itself, as the exchange route takes it.
ExchangeProof = Literal["k8s", "bootstrap"]
#: RFC 8693 token types, by proof.
TOKEN_TYPES: Mapping[str, ExchangeProof] = {
    "urn:ietf:params:oauth:token-type:jwt": "k8s",
    "urn:scadbuddy:params:token-type:bootstrap": "bootstrap",
}

#: §6.1: each ScadBuddy ServiceAccount's caller. ``default`` is never here: every pod
#: that names no ServiceAccount runs as it, Bambuddy's own among them (§2.4).
SERVICE_ACCOUNTS: Mapping[str, ServiceCaller] = {
    "scadbuddy-render": "render",
    "scadbuddy-print": "print",
    "scadbuddy-projects": "projects",
    "scadbuddy-agent-worker": "agent",
    # The API pod's: mounted as a projected token in the agent sidecar's container only.
    "scadbuddy": "agent",
}

_POD_NAME = "authentication.kubernetes.io/pod-name"
_POD_UID = "authentication.kubernetes.io/pod-uid"

#: Where a pod finds the apiserver and its own token (Kubernetes' in-cluster layout).
IN_CLUSTER_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
IN_CLUSTER_CA = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
#: TokenReview answers within this, or the exchange answers 503.
REVIEW_TIMEOUT = 5.0
#: A bootstrap secret shorter than this is a start error.
MIN_BOOTSTRAP_CHARS = 32


@dataclass(frozen=True)
class VerifiedProof:
    caller: ServiceCaller
    proof: ExchangeProof
    pod_name: str | None
    pod_uid: str | None


class VerifierUnavailableError(Exception):
    """The verifier could not decide (the apiserver unreachable or refusing the API)."""


class ProofVerifier(Protocol):
    async def verify(self, token: str) -> VerifiedProof | None: ...


def caller_for_service_account(username: str, namespace: str) -> ServiceCaller | None:
    """The caller a token's ``sub`` or a TokenReview's ``status.user.username`` names, or
    ``None``: an unknown ServiceAccount, ``default``, another namespace, or no namespace
    configured."""
    if not namespace:
        return None
    prefix = f"system:serviceaccount:{namespace}:"
    if not username.startswith(prefix):
        return None
    return SERVICE_ACCOUNTS.get(username.removeprefix(prefix))


def _one(extra: Mapping[str, Any], key: str) -> str | None:
    values = extra.get(key)
    if isinstance(values, list) and len(values) == 1 and isinstance(values[0], str) and values[0]:
        return values[0]
    return None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class TokenReviewVerifier:
    """§6.2 steps 1 to 3, behind an offline check (review of #5498): the token's signature
    against the issuer's JWKS and its claims first, then a TokenReview with
    ``spec.audiences: ["scadbuddy"]`` within :class:`ReviewBudget`. Every call to the
    apiserver is made with the API pod's own token, re-read each time (the kubelet
    rotates it)."""

    def __init__(
        self,
        namespace: str,
        *,
        url: str,
        token_file: Path,
        ca_file: Path | None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.namespace = namespace
        self._base = url.rstrip("/")
        self._token_file = token_file
        self._verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=str(ca_file)) if ca_file is not None else True
        )
        self._transport = transport
        self._clock = clock
        self._now = now
        self.budget = ReviewBudget(clock=clock)
        self._keys_lock = asyncio.Lock()
        self._keys: tuple[float, str, KeySet] | None = None
        self._refetched_at: float | None = None

    @classmethod
    def in_cluster(cls, namespace: str) -> TokenReviewVerifier | None:
        """The verifier for this pod's cluster, or ``None`` outside one (no apiserver in
        the environment, or no token of the pod's own to ask with)."""
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host or not namespace or not IN_CLUSTER_TOKEN.is_file():
            return None
        if ":" in host:
            host = f"[{host}]"
        return cls(
            namespace,
            url=f"https://{host}:{port}",
            token_file=IN_CLUSTER_TOKEN,
            ca_file=IN_CLUSTER_CA,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._verify, transport=self._transport, timeout=REVIEW_TIMEOUT
        )

    def _own_token(self) -> str:
        try:
            return self._token_file.read_text().strip()
        except OSError as error:
            raise VerifierUnavailableError("the API pod has no apiserver token") from error

    async def _get(self, client: httpx.AsyncClient, path: str) -> dict[str, Any]:
        try:
            response = await client.get(
                self._base + path, headers={"Authorization": f"Bearer {self._own_token()}"}
            )
        except httpx.HTTPError as error:
            raise VerifierUnavailableError(type(error).__name__) from error
        if response.status_code != 200:
            raise VerifierUnavailableError(f"{path} answered {response.status_code}")
        try:
            data = response.json()
        except ValueError as error:
            raise VerifierUnavailableError(f"{path} answered no JSON") from error
        if not isinstance(data, dict):
            raise VerifierUnavailableError(f"{path} answered no object")
        return data

    async def _issuer_keys(self, *, refetch: bool = False) -> tuple[str, KeySet]:
        """The issuer (from its discovery document) and its JWKS, both read from the
        apiserver itself (`kubernetes.default.svc`), never from the discovery's
        ``jwks_uri``. Kept :data:`JWKS_CACHE_SECONDS`; ``refetch`` (an unknown ``kid``)
        reads them again at most every :data:`JWKS_REFETCH_SECONDS`."""
        async with self._keys_lock:
            now = self._clock()
            cached = self._keys
            if cached is not None:
                fresh = now - cached[0] < JWKS_CACHE_SECONDS
                if not refetch and fresh:
                    return cached[1], cached[2]
                if (
                    refetch
                    and self._refetched_at is not None
                    and now - self._refetched_at < JWKS_REFETCH_SECONDS
                ):
                    return cached[1], cached[2]
            async with self._client() as client:
                discovery = await self._get(client, "/.well-known/openid-configuration")
                jwks = await self._get(client, "/openid/v1/jwks")
            issuer = discovery.get("issuer")
            if not isinstance(issuer, str) or not issuer:
                raise VerifierUnavailableError("the cluster's discovery names no issuer")
            try:
                keys = KeySet.import_key_set(jwks)  # type: ignore[arg-type]
            except (jose_errors.JoseError, ValueError, TypeError, KeyError):
                raise VerifierUnavailableError("the cluster's JWKS holds no usable key") from None
            if refetch:
                self._refetched_at = now
            self._keys = (now, issuer, keys)
            return issuer, keys

    async def _signed_caller(self, token: str) -> ServiceCaller | None:
        """The caller a token the cluster signed names, checked offline; ``None`` for
        anything else (no TokenReview, no budget)."""
        issuer, keys = await self._issuer_keys()
        try:
            decoded = jwt.decode(token, keys, algorithms=list(JWT_ALGORITHMS))
        except jose_errors.InvalidKeyIdError:
            issuer, keys = await self._issuer_keys(refetch=True)
            try:
                decoded = jwt.decode(token, keys, algorithms=list(JWT_ALGORITHMS))
            except (jose_errors.JoseError, ValueError, TypeError):
                return None
        except (jose_errors.JoseError, ValueError, TypeError):
            return None
        if not isinstance(decoded.header.get("kid"), str):
            return None
        claims = decoded.claims
        aud = claims.get("aud")
        audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        at = self._now()
        exp, nbf = _number(claims.get("exp")), _number(claims.get("nbf"))
        if (
            claims.get("iss") != issuer
            or AUDIENCE not in audiences
            or exp is None
            or exp + LEEWAY_SECONDS <= at
            or (claims.get("nbf") is not None and (nbf is None or nbf - LEEWAY_SECONDS > at))
        ):
            return None
        sub = claims.get("sub")
        return caller_for_service_account(sub, self.namespace) if isinstance(sub, str) else None

    async def verify(self, token: str) -> VerifiedProof | None:
        caller = await self._signed_caller(token)
        if caller is None:
            return None
        wait = self.budget.spend()
        if wait is not None:
            raise ReviewLimitedError(caller, wait)
        review = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "spec": {"token": token, "audiences": [AUDIENCE]},
        }
        try:
            async with self._client() as client:
                response = await client.post(
                    self._base + "/apis/authentication.k8s.io/v1/tokenreviews",
                    json=review,
                    headers={"Authorization": f"Bearer {self._own_token()}"},
                )
        except httpx.HTTPError as error:
            raise VerifierUnavailableError(type(error).__name__) from error
        if response.status_code not in (200, 201):
            raise VerifierUnavailableError(f"TokenReview answered {response.status_code}")
        try:
            status = response.json().get("status") or {}
        except ValueError as error:
            raise VerifierUnavailableError("TokenReview answered no JSON") from error
        proof = self._proof(status)
        if proof is not None and proof.caller != caller:
            logger.warning("refused a service-token exchange: TokenReview named another caller")
            return None
        return proof

    def _proof(self, status: Mapping[str, Any]) -> VerifiedProof | None:
        if status.get("authenticated") is not True:
            return None
        audiences = status.get("audiences")
        if not isinstance(audiences, list) or AUDIENCE not in audiences:
            return None
        user = status.get("user") or {}
        username = user.get("username")
        caller = (
            caller_for_service_account(username, self.namespace)
            if isinstance(username, str)
            else None
        )
        if caller is None:
            logger.info("refused a service-token exchange: not a ScadBuddy ServiceAccount")
            return None
        extra = user.get("extra") or {}
        pod_name, pod_uid = _one(extra, _POD_NAME), _one(extra, _POD_UID)
        if pod_name is None or pod_uid is None:
            # A token bound to no pod (`kubectl create token` without --bound-object-*):
            # deleting a pod could not revoke it.
            logger.info("refused a service-token exchange: the token is bound to no pod")
            return None
        return VerifiedProof(caller, "k8s", pod_name, pod_uid)


class BootstrapConfigError(ValueError):
    """``SCADBUDDY_SERVICE_BOOTSTRAP_DIR`` cannot be used: the API refuses to start."""


class BootstrapVerifier:
    """§7.2: each caller's bootstrap secret, as the SHA-256 of the file named after it
    (``render``, ``print``, ``projects``, ``agent``) read once at start."""

    def __init__(self, hashes: Mapping[ServiceCaller, bytes]) -> None:
        self.hashes = dict(hashes)

    @classmethod
    def from_dir(cls, directory: Path) -> BootstrapVerifier:
        if not directory.is_dir():
            raise BootstrapConfigError(
                f"SCADBUDDY_SERVICE_BOOTSTRAP_DIR {directory} is not a directory"
            )
        hashes: dict[ServiceCaller, bytes] = {}
        for caller in CALLERS:
            path = directory / caller
            if not path.is_file():
                continue
            secret = path.read_text().strip()
            if len(secret) < MIN_BOOTSTRAP_CHARS:
                raise BootstrapConfigError(
                    f"SCADBUDDY_SERVICE_BOOTSTRAP_DIR: the {caller} secret is shorter than"
                    f" {MIN_BOOTSTRAP_CHARS} characters"
                )
            digest = hashlib.sha256(secret.encode()).digest()
            if digest in hashes.values():
                raise BootstrapConfigError(
                    f"SCADBUDDY_SERVICE_BOOTSTRAP_DIR: the {caller} secret is another caller's"
                )
            hashes[caller] = digest
        if not hashes:
            logger.warning("SCADBUDDY_SERVICE_BOOTSTRAP_DIR holds no caller's secret")
        return cls(hashes)

    async def verify(self, token: str) -> VerifiedProof | None:
        presented = hashlib.sha256(token.strip().encode()).digest()
        found: ServiceCaller | None = None
        # Every hash is compared, in constant time, whichever matches.
        for caller, digest in self.hashes.items():
            if hmac.compare_digest(presented, digest):
                found = caller
        return (
            None
            if found is None or not token.strip()
            else VerifiedProof(found, "bootstrap", None, None)
        )
