"""The proofs a caller presents at ``POST /api/v1/auth/service-token`` (#5494, spec
2026-11-05 §6.1, §6.2, §7.2, §11 D1, D2).

In Kubernetes a caller presents its projected ServiceAccount token (audience
:data:`AUDIENCE`), which :class:`TokenReviewVerifier` asks the apiserver about with the
API pod's own token: a deleted pod's token is refused at once, and the credential is bound
to the pod's name and uid. Outside it, :class:`BootstrapVerifier` compares a per-caller
secret with the hashes read at start from ``SCADBUDDY_SERVICE_BOOTSTRAP_DIR``. Each
verifier answers the caller it proved, or ``None``; a verifier that cannot answer at all
raises :class:`VerifierUnavailableError`. Nothing here logs a presented token.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
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

from scadbuddy.auth.service_credentials import CALLERS, ServiceCaller

logger = logging.getLogger(__name__)

#: Review of #5498: refused proofs are limited before any TokenReview. Per API process,
#: at most this many proofs a minute reach a verifier ...
VERIFIER_BUDGET = 60
#: ... and a claimed caller with this many refusals in a minute is limited for the rest
#: of it (all its proofs, its real one too, answer 429 without a TokenReview).
REFUSALS_PER_CALLER = 10
LIMIT_WINDOW = 60.0
#: The bucket of every claim that names no caller, so a forger cannot grow the table.
UNKNOWN = "unknown"


class ProofLimiter:
    """The two limits on proofs, in this process's memory (one event loop, no lock):
    :meth:`spend` before each verifier call, :meth:`check` and :meth:`refused` per
    claimed caller. Each answers the seconds to wait, or ``None``."""

    def __init__(
        self,
        *,
        budget: int = VERIFIER_BUDGET,
        refusals: int = REFUSALS_PER_CALLER,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budget = budget
        self._refusals = refusals
        self._clock = clock
        self._spent: deque[float] = deque()
        self._refused: dict[str, deque[float]] = {}

    def _trim(self, times: deque[float], now: float) -> None:
        while times and now - times[0] >= LIMIT_WINDOW:
            times.popleft()

    def _wait(self, times: deque[float], now: float) -> int:
        return max(1, math.ceil(times[0] + LIMIT_WINDOW - now))

    def spend(self) -> int | None:
        """One proof reaches a verifier, or the seconds until one may."""
        now = self._clock()
        self._trim(self._spent, now)
        if len(self._spent) >= self._budget:
            return self._wait(self._spent, now)
        self._spent.append(now)
        return None

    def check(self, claimed: str) -> int | None:
        now = self._clock()
        times = self._refused.get(claimed)
        if times is None:
            return None
        self._trim(times, now)
        if len(times) >= self._refusals:
            return self._wait(times, now)
        return None

    def refused(self, claimed: str) -> None:
        now = self._clock()
        times = self._refused.setdefault(claimed, deque())
        self._trim(times, now)
        times.append(now)


def _claims(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        data = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(data)
    except (binascii.Error, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


def claimed_caller(token: str, namespace: str) -> str | None:
    """The caller a JWT's payload claims (``kubernetes.io.serviceaccount.name``, else
    ``sub``), read WITHOUT checking its signature: only a refusal bucket's key, never who
    the caller is. :data:`UNKNOWN` for a claim of no ScadBuddy ServiceAccount here, and
    ``None`` for a token that is not a JWT."""
    claims = _claims(token)
    if claims is None:
        return None
    k8s = claims.get("kubernetes.io")
    account = k8s.get("serviceaccount") if isinstance(k8s, dict) else None
    name = account.get("name") if isinstance(account, dict) else None
    claimed_namespace = k8s.get("namespace") if isinstance(k8s, dict) else None
    if isinstance(name, str) and claimed_namespace == namespace:
        username = f"system:serviceaccount:{namespace}:{name}"
    elif isinstance(claims.get("sub"), str):
        username = claims["sub"]
    else:
        return UNKNOWN
    return caller_for_service_account(username, namespace) or UNKNOWN


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
    def claim(self, token: str, client_id: str) -> str | None:
        """The caller the proof claims to be, unverified: a refusal bucket's key, or
        ``None`` for a token that cannot be a proof of this kind."""
        ...

    async def verify(self, token: str) -> VerifiedProof | None: ...


def caller_for_service_account(username: str, namespace: str) -> ServiceCaller | None:
    """The caller a TokenReview's ``status.user.username`` names, or ``None``: an unknown
    ServiceAccount, ``default``, another namespace, or no namespace configured."""
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


class TokenReviewVerifier:
    """§6.2 steps 1 to 3: a TokenReview with ``spec.audiences: ["scadbuddy"]``, sent with
    the API pod's own token (re-read each time: the kubelet rotates it)."""

    def __init__(
        self,
        namespace: str,
        *,
        url: str,
        token_file: Path,
        ca_file: Path | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.namespace = namespace
        self._url = url.rstrip("/") + "/apis/authentication.k8s.io/v1/tokenreviews"
        self._token_file = token_file
        self._verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=str(ca_file)) if ca_file is not None else True
        )
        self._transport = transport

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

    def claim(self, token: str, client_id: str) -> str | None:
        return claimed_caller(token, self.namespace)

    async def verify(self, token: str) -> VerifiedProof | None:
        try:
            own = self._token_file.read_text().strip()
        except OSError as error:
            raise VerifierUnavailableError("the API pod has no apiserver token") from error
        review = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "spec": {"token": token, "audiences": [AUDIENCE]},
        }
        try:
            async with httpx.AsyncClient(
                verify=self._verify, transport=self._transport, timeout=REVIEW_TIMEOUT
            ) as client:
                response = await client.post(
                    self._url, json=review, headers={"Authorization": f"Bearer {own}"}
                )
        except httpx.HTTPError as error:
            raise VerifierUnavailableError(type(error).__name__) from error
        if response.status_code not in (200, 201):
            raise VerifierUnavailableError(f"TokenReview answered {response.status_code}")
        try:
            status = response.json().get("status") or {}
        except ValueError as error:
            raise VerifierUnavailableError("TokenReview answered no JSON") from error
        return self._proof(status)

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

    def claim(self, token: str, client_id: str) -> str | None:
        """The caller named in the request's ``client_id``; a bootstrap secret says nothing."""
        return client_id if client_id in CALLERS else UNKNOWN

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
