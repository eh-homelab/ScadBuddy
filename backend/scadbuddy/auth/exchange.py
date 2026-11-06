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

import hashlib
import hmac
import logging
import os
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from scadbuddy.auth.service_credentials import CALLERS, ServiceCaller

logger = logging.getLogger(__name__)

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
