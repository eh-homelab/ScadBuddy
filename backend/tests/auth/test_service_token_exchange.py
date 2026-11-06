"""The service-token exchange (#5494, spec 2026-11-05 §6, §7, §8 step 1): the store's
exchanged credentials (15 minutes, no sealed copy, bound to a pod), the rate limits,
suspension, the proof verifiers (TokenReview against a fake apiserver, the bootstrap
directory), the ServiceAccount map, and the callers' readers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from joserfc import jwt
from joserfc.jwk import ECKey, KeySet, OctKey, RSAKey
from pydantic import ValidationError

from scadbuddy.auth.exchange import (
    AUDIENCE,
    BootstrapConfigError,
    BootstrapVerifier,
    ReviewBudget,
    ReviewLimitedError,
    TokenReviewVerifier,
    VerifiedProof,
    VerifierUnavailableError,
    caller_for_service_account,
)
from scadbuddy.auth.models import Service
from scadbuddy.auth.service_credentials import (
    EXCHANGE_LIFETIME,
    LIFETIME,
    SCOPES,
    CallerSuspendedError,
    ExchangeReader,
    InProcessProvider,
    RateLimitedError,
    ServiceAuth,
    ServiceCredentialConfigError,
    ServiceCredentialStore,
    TokenReader,
    token_source,
)
from scadbuddy.core.secrets import Kek
from scadbuddy.core.settings import Settings
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS, PgPool

KEK = Kek("k1", os.urandom(32))
NAMESPACE = "bambuddy"
ISSUER = "https://kubernetes.default.svc.cluster.local"

# The cluster's signing keys: one RSA (as the cluster's, RS256) and one EC (ES256).
SIGNING_KEY = RSAKey.generate_key(2048, parameters={"kid": "rsa-1"})
EC_KEY = ECKey.generate_key("P-256", parameters={"kid": "ec-1"})
# A key the cluster never published: what a forger signs with.
FORGER_KEY = RSAKey.generate_key(2048, parameters={"kid": "rsa-1"})


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def claims_for(
    service_account: str, *, namespace: str = NAMESPACE, nonce: str = "", **override: Any
) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": [AUDIENCE],
        "sub": f"system:serviceaccount:{namespace}:{service_account}",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "kubernetes.io": {"namespace": namespace, "serviceaccount": {"name": service_account}},
        "nonce": nonce,
    }
    claims.update(override)
    return claims


def signed(
    service_account: str,
    *,
    key: Any = SIGNING_KEY,
    alg: str = "RS256",
    kid: str | None = None,
    **claims: Any,
) -> str:
    """A projected token as the cluster (or, with another key, a forger) signs it."""
    header = {"alg": alg, "kid": kid or key.kid}
    return jwt.encode(header, claims_for(service_account, **claims), key)


def unsigned(service_account: str) -> str:
    """``alg: none``: no signature at all."""
    header = _b64({"alg": "none", "kid": "rsa-1"})
    return f"{header}.{_b64(claims_for(service_account))}."


def hs256(service_account: str) -> str:
    """HS256 under a key a forger chose (the classic confusion of a public key for a
    shared secret)."""
    return jwt.encode(
        {"alg": "HS256", "kid": "rsa-1"},
        claims_for(service_account),
        OctKey.import_key(b"x" * 32),
        algorithms=["HS256"],
    )


# The ServiceAccount map (§6.1).


@pytest.mark.parametrize(
    ("username", "caller"),
    [
        ("system:serviceaccount:bambuddy:scadbuddy-render", "render"),
        ("system:serviceaccount:bambuddy:scadbuddy-print", "print"),
        ("system:serviceaccount:bambuddy:scadbuddy-projects", "projects"),
        ("system:serviceaccount:bambuddy:scadbuddy-agent-worker", "agent"),
        ("system:serviceaccount:bambuddy:scadbuddy", "agent"),
        # Every pod that names no ServiceAccount runs as `default`: never a caller.
        ("system:serviceaccount:bambuddy:default", None),
        ("system:serviceaccount:bambuddy:pgadmin", None),
        ("system:serviceaccount:other:scadbuddy-print", None),
        ("system:serviceaccount:bambuddy:scadbuddy-print:x", None),
        ("system:node:bambuddy:scadbuddy-print", None),
        ("scadbuddy-print", None),
    ],
)
def test_the_service_account_map(username: str, caller: str | None) -> None:
    assert caller_for_service_account(username, NAMESPACE) == caller


def test_no_namespace_maps_nothing() -> None:
    assert caller_for_service_account("system:serviceaccount::scadbuddy-print", "") is None
    assert caller_for_service_account("system:serviceaccount:bambuddy:scadbuddy-print", "") is None


def test_the_tokenreview_budget_is_60_a_minute() -> None:
    clock = Clock()
    budget = ReviewBudget(clock=clock)
    for _ in range(60):
        assert budget.spend() is None
    assert budget.spend() == 60
    clock.now += 30
    assert budget.spend() == 30
    clock.now += 30
    assert budget.spend() is None


# The offline check and TokenReview (§6.2, review of #5498), against a fake apiserver.


POD_EXTRA = {
    "authentication.kubernetes.io/pod-name": ["scadbuddy-print-6c9f-abcde"],
    "authentication.kubernetes.io/pod-uid": ["0f6f2b7e-1111-2222-3333-444455556666"],
    "authentication.kubernetes.io/credential-id": ["JTI=abc"],
}


class FakeApiserver:
    """The cluster's issuer discovery, its JWKS and TokenReview. A review authenticates
    every token signed by a published key, as the token's `sub`, bound to a pod, unless
    the token was revoked or given a status of its own."""

    def __init__(self, own_token: str = "api-pod-token") -> None:
        self.own_token = own_token
        self.keys: list[Any] = [SIGNING_KEY, EC_KEY]
        self.statuses: dict[str, dict[str, Any]] = {}
        self.revoked: set[str] = set()
        #: TokenReviews only.
        self.requests: list[httpx.Request] = []
        self.jwks_fetches = 0
        self.status = 201
        self.jwks_status = 200

    def revoke(self, token: str) -> None:
        self.revoked.add(token)

    def review_as(
        self,
        token: str,
        *,
        username: str | None = None,
        extra: dict[str, list[str]] | None = None,
        audiences: list[str] | None = None,
    ) -> None:
        sub = jwt.decode(token, KeySet(self.keys), algorithms=["RS256", "ES256"]).claims["sub"]
        self.statuses[token] = {
            "authenticated": True,
            "user": {"username": username or sub, "extra": POD_EXTRA if extra is None else extra},
            "audiences": [AUDIENCE] if audiences is None else audiences,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {self.own_token}":
            return httpx.Response(401)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={"issuer": ISSUER, "jwks_uri": "https://192.168.11.51:6443/openid/v1/jwks"},
            )
        if request.url.path == "/openid/v1/jwks":
            self.jwks_fetches += 1
            if self.jwks_status != 200:
                return httpx.Response(self.jwks_status)
            return httpx.Response(200, json={"keys": [k.as_dict(private=False) for k in self.keys]})
        assert request.url.path == "/apis/authentication.k8s.io/v1/tokenreviews"
        self.requests.append(request)
        if self.status != 201:
            return httpx.Response(self.status)
        body = json.loads(request.content)
        assert body["kind"] == "TokenReview"
        token = body["spec"]["token"]
        status: dict[str, Any] = {"authenticated": False, "error": "invalid bearer token"}
        if AUDIENCE in body["spec"].get("audiences", []) and token not in self.revoked:
            if token not in self.statuses:
                self.review_as(token)
            status = self.statuses[token]
        return httpx.Response(201, json={**body, "status": status})


def _verifier(
    apiserver: FakeApiserver, tmp_path: Path, clock: Callable[[], float] = time.monotonic
) -> TokenReviewVerifier:
    token_file = tmp_path / "apiserver-token"
    token_file.write_text(apiserver.own_token + "\n")
    return TokenReviewVerifier(
        NAMESPACE,
        url="https://kubernetes.default.svc:443",
        token_file=token_file,
        ca_file=None,
        transport=httpx.MockTransport(apiserver.handler),
        clock=clock,
    )


async def test_a_signed_pod_bound_token_is_reviewed_and_mapped(tmp_path: Path) -> None:
    apiserver = FakeApiserver()
    verifier = _verifier(apiserver, tmp_path)
    proof = await verifier.verify(signed("scadbuddy-print"))
    assert proof == VerifiedProof(
        caller="print",
        proof="k8s",
        pod_name="scadbuddy-print-6c9f-abcde",
        pod_uid="0f6f2b7e-1111-2222-3333-444455556666",
    )
    (request,) = apiserver.requests
    assert json.loads(request.content)["spec"]["audiences"] == [AUDIENCE]
    # ES256 is the other algorithm a cluster may sign with.
    assert await verifier.verify(signed("scadbuddy-render", key=EC_KEY, alg="ES256")) is not None


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(signed("scadbuddy-print", key=FORGER_KEY), id="another-key"),
        pytest.param(unsigned("scadbuddy-print"), id="alg-none"),
        pytest.param(hs256("scadbuddy-print"), id="hs256"),
        pytest.param(signed("scadbuddy-print", iss="https://elsewhere"), id="issuer"),
        pytest.param(signed("scadbuddy-print", aud=["https://kubernetes"]), id="audience"),
        pytest.param(signed("scadbuddy-print", exp=int(time.time()) - 120), id="expired"),
        pytest.param(signed("scadbuddy-print", nbf=int(time.time()) + 120), id="not-yet"),
        pytest.param(signed("default"), id="default-sa"),
        pytest.param(signed("pgadmin"), id="unknown-sa"),
        pytest.param(signed("scadbuddy-print", namespace="other"), id="other-namespace"),
        pytest.param("not-a-jwt", id="junk"),
        pytest.param("a.b.c", id="junk-parts"),
    ],
)
async def test_a_token_that_fails_offline_never_reaches_tokenreview(
    tmp_path: Path, token: str
) -> None:
    apiserver = FakeApiserver()
    verifier = _verifier(apiserver, tmp_path)
    assert await verifier.verify(token) is None
    assert apiserver.requests == []
    # Nor does it spend the TokenReview budget.
    assert verifier.budget.spent == 0


async def test_a_little_clock_skew_is_allowed(tmp_path: Path) -> None:
    verifier = _verifier(FakeApiserver(), tmp_path)
    now = int(time.time())
    assert await verifier.verify(signed("scadbuddy-print", exp=now - 10)) is not None
    assert await verifier.verify(signed("scadbuddy-print", nbf=now + 10)) is not None


async def test_tokenreview_still_decides(tmp_path: Path) -> None:
    """Revocation (a deleted pod) and the pod binding are TokenReview's."""
    apiserver = FakeApiserver()
    verifier = _verifier(apiserver, tmp_path)
    revoked = signed("scadbuddy-print", nonce="revoked")
    apiserver.revoke(revoked)
    unbound = signed("scadbuddy-print", nonce="unbound")
    apiserver.review_as(unbound, extra={})
    other_audience = signed("scadbuddy-print", nonce="aud")
    apiserver.review_as(other_audience, audiences=["https://kubernetes"])
    # The apiserver must agree on who it is.
    disagrees = signed("scadbuddy-print", nonce="who")
    apiserver.review_as(disagrees, username="system:serviceaccount:bambuddy:scadbuddy-render")
    for token in (revoked, unbound, other_audience, disagrees):
        assert await verifier.verify(token) is None
    assert len(apiserver.requests) == 4


async def test_an_unknown_kid_refetches_the_jwks_at_most_every_30_s(tmp_path: Path) -> None:
    apiserver = FakeApiserver()
    clock = Clock()
    verifier = _verifier(apiserver, tmp_path, clock)
    assert await verifier.verify(signed("scadbuddy-print")) is not None
    assert apiserver.jwks_fetches == 1
    rotated = RSAKey.generate_key(2048, parameters={"kid": "rsa-2"})
    for i in range(5):
        assert await verifier.verify(signed("scadbuddy-print", key=rotated, nonce=str(i))) is None
    assert apiserver.jwks_fetches == 2
    # The cluster rotates its key; within 30 s it is still not asked again.
    apiserver.keys.append(rotated)
    clock.now += 29
    assert await verifier.verify(signed("scadbuddy-print", key=rotated)) is None
    assert apiserver.jwks_fetches == 2
    clock.now += 1
    assert await verifier.verify(signed("scadbuddy-print", key=rotated)) is not None
    assert apiserver.jwks_fetches == 3


async def test_the_budget_holds_back_only_signed_tokens(tmp_path: Path) -> None:
    apiserver = FakeApiserver()
    clock = Clock()
    verifier = _verifier(apiserver, tmp_path, clock)
    for i in range(60):
        token = signed("scadbuddy-print", nonce=f"r{i}")
        apiserver.revoke(token)
        assert await verifier.verify(token) is None
    with pytest.raises(ReviewLimitedError) as limited:
        await verifier.verify(signed("scadbuddy-print"))
    assert limited.value.retry_after == 60
    assert limited.value.caller == "print"
    assert len(apiserver.requests) == 60


@pytest.mark.parametrize("where", ["review", "jwks"])
async def test_an_apiserver_down_is_unavailable_not_refused(tmp_path: Path, where: str) -> None:
    apiserver = FakeApiserver()
    if where == "review":
        apiserver.status = 503
    else:
        apiserver.jwks_status = 503
    with pytest.raises(VerifierUnavailableError):
        await _verifier(apiserver, tmp_path).verify(signed("scadbuddy-print"))


# The bootstrap directory (§7.2).


def _bootstrap_dir(tmp_path: Path, **secrets_by_caller: str) -> Path:
    directory = tmp_path / "bootstrap"
    directory.mkdir()
    for caller, value in secrets_by_caller.items():
        (directory / caller).write_text(value + "\n")
    return directory


async def test_bootstrap_maps_each_callers_secret(tmp_path: Path) -> None:
    render, agent = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier = BootstrapVerifier.from_dir(_bootstrap_dir(tmp_path, render=render, agent=agent))
    assert await verifier.verify(render) == VerifiedProof("render", "bootstrap", None, None)
    assert await verifier.verify(agent) == VerifiedProof("agent", "bootstrap", None, None)
    assert await verifier.verify(secrets.token_urlsafe(32)) is None
    assert await verifier.verify("") is None
    # Only the hashes are kept.
    assert render not in repr(vars(verifier))
    assert hashlib.sha256(render.encode()).digest() in verifier.hashes.values()


def test_a_short_bootstrap_secret_is_a_start_error(tmp_path: Path) -> None:
    with pytest.raises(BootstrapConfigError):
        BootstrapVerifier.from_dir(_bootstrap_dir(tmp_path, print="short"))


def test_one_secret_for_two_callers_is_a_start_error(tmp_path: Path) -> None:
    shared = secrets.token_urlsafe(32)
    with pytest.raises(BootstrapConfigError):
        BootstrapVerifier.from_dir(_bootstrap_dir(tmp_path, print=shared, projects=shared))


def test_a_missing_bootstrap_dir_is_a_start_error(tmp_path: Path) -> None:
    with pytest.raises(BootstrapConfigError):
        BootstrapVerifier.from_dir(tmp_path / "nope")


# The store's exchanged credentials (§6.2, §6.3, §6.5, §11 D4, D5).


@pytest.fixture
def store(pg_pool: PgPool) -> ServiceCredentialStore:
    return ServiceCredentialStore(pg_pool)


pg = pytest.mark.requires_postgres


@pg
def test_an_exchanged_credential_lives_15_minutes_bound_to_its_pod(
    store: ServiceCredentialStore, pg_pool: PgPool
) -> None:
    now = datetime.now(UTC)
    issued = store.issue("render", "k8s", pod_name="p", pod_uid="u", now=now)
    assert issued.token.startswith("sbsvc_")
    assert issued.expires_at == now + EXCHANGE_LIFETIME
    assert timedelta(minutes=15) == EXCHANGE_LIFETIME
    assert issued.scopes == SCOPES["render"] == frozenset({"crud.render"})
    assert store.resolve(issued.token, now=now) == Service("render", SCOPES["render"])
    assert store.resolve(issued.token, now=now + EXCHANGE_LIFETIME) is None
    with pg_pool.connection() as conn:
        row = conn.execute("SELECT * FROM service_credentials").fetchone()
    assert row is not None
    assert (row["proof"], row["pod_name"], row["pod_uid"]) == ("k8s", "p", "u")
    # No sealed copy: nobody reads it back.
    assert (row["secret_sealed"], row["dek_sealed"], row["kek_id"]) == (None, None, None)
    (listed,) = store.list()
    assert (listed.proof, listed.pod_name, listed.pod_uid) == ("k8s", "p", "u")


@pg
@pytest.mark.parametrize(
    ("proof", "sealed", "pod"),
    [
        ("legacy", False, False),  # a legacy row must carry its seal
        ("k8s", True, True),  # an exchanged row must carry none
        ("bootstrap", False, True),  # only a k8s row names a pod
        ("k8s", False, False),  # and a k8s row always does
    ],
)
def test_the_table_refuses_a_row_of_the_wrong_shape(
    pg_pool: PgPool, proof: str, sealed: bool, pod: bool
) -> None:
    seal = b"x" if sealed else None
    with pg_pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO service_credentials (id, caller, scopes, secret_hash, secret_sealed,"
            " dek_sealed, kek_id, expires_at, proof, pod_name, pod_uid)"
            " VALUES ('00000000000000aa', 'print', '{}', %s, %s, %s, %s, now(), %s, %s, %s)",
            (
                b"\0" * 32,
                seal,
                seal,
                "k1" if sealed else None,
                proof,
                "p" if pod else None,
                "u" if pod else None,
            ),
        )


@pg
def test_an_exchanged_row_is_never_handed_out_through_a_view(
    store: ServiceCredentialStore, pg_conninfo: str
) -> None:
    """A caller still reading its view (§8 step 1) never gets an unsealed row."""
    store.issue("print", "k8s", pod_name="p", pod_uid="u")
    assert TokenReader("print", pg_conninfo, KEK).token() is None
    store.rotate_due(KEK)
    token = TokenReader("print", pg_conninfo, KEK).token()
    assert token is not None
    assert store.resolve(token) == Service("print", SCOPES["print"])


@pg
def test_the_rotation_never_mints_for_render_or_counts_an_exchanged_row(
    store: ServiceCredentialStore,
) -> None:
    store.issue("print", "k8s", pod_name="p", pod_uid="u")
    minted = store.rotate_due(KEK)
    assert len(minted) == 3
    assert sorted(c.caller for c in store.list() if c.proof == "legacy") == [
        "agent",
        "print",
        "projects",
    ]


@pg
def test_six_a_minute_per_pod(store: ServiceCredentialStore) -> None:
    now = datetime.now(UTC)
    for i in range(6):
        store.issue("print", "k8s", pod_name="p", pod_uid="u1", now=now + timedelta(seconds=i))
    with pytest.raises(RateLimitedError) as limited:
        store.issue("print", "k8s", pod_name="p", pod_uid="u1", now=now + timedelta(seconds=10))
    # The oldest counted leaves the window 50 s later.
    assert limited.value.retry_after == 50
    # Another pod of the same caller is not held back.
    store.issue("print", "k8s", pod_name="q", pod_uid="u2", now=now + timedelta(seconds=10))
    store.issue("print", "k8s", pod_name="p", pod_uid="u1", now=now + timedelta(seconds=61))


@pg
def test_thirty_a_minute_per_caller(store: ServiceCredentialStore) -> None:
    now = datetime.now(UTC)
    for i in range(30):
        store.issue("agent", "k8s", pod_name=f"p{i}", pod_uid=f"u{i}", now=now)
    with pytest.raises(RateLimitedError):
        store.issue("agent", "k8s", pod_name="p99", pod_uid="u99", now=now)
    # Other callers are not held back.
    store.issue("print", "k8s", pod_name="p", pod_uid="u", now=now)


@pg
def test_a_bootstrap_caller_counts_as_one_pod(store: ServiceCredentialStore) -> None:
    now = datetime.now(UTC)
    for _ in range(6):
        store.issue("projects", "bootstrap", now=now)
    with pytest.raises(RateLimitedError):
        store.issue("projects", "bootstrap", now=now)


@pg
def test_the_in_process_provider_is_not_rate_limited(store: ServiceCredentialStore) -> None:
    now = datetime.now(UTC)
    for _ in range(8):
        store.issue("projects", "inprocess", now=now, rate_limited=False)


@pg
def test_a_suspended_caller_gets_nothing_and_loses_what_it_holds(
    store: ServiceCredentialStore,
) -> None:
    store.rotate_due(KEK)
    issued = store.issue("print", "k8s", pod_name="p", pod_uid="u")
    other = store.issue("agent", "k8s", pod_name="a", pod_uid="ua")
    store.suspend("print", by="user:i#s")
    assert store.suspensions()[0].caller == "print"
    assert store.resolve(issued.token) is None
    with pytest.raises(CallerSuspendedError):
        store.issue("print", "k8s", pod_name="p", pod_uid="u")
    # The rotation does not mint for it either, and every one of its rows is revoked.
    store.rotate_due(KEK, now=datetime.now(UTC) + LIFETIME)
    assert not [c for c in store.list() if c.caller == "print" and c.revoked_at is None]
    assert store.resolve(other.token) == Service("agent", SCOPES["agent"])
    store.resume("print")
    assert store.suspensions() == []
    store.issue("print", "k8s", pod_name="p", pod_uid="u")


@pg
def test_revoke_every_credential_of_a_pod(store: ServiceCredentialStore) -> None:
    first = store.issue("print", "k8s", pod_name="p", pod_uid="u")
    second = store.issue("print", "k8s", pod_name="p", pod_uid="u")
    kept = store.issue("print", "k8s", pod_name="q", pod_uid="v")
    assert store.revoke_pod("u") == 2
    assert store.resolve(first.token) is None
    assert store.resolve(second.token) is None
    assert store.resolve(kept.token) is not None


@pg
def test_exchanged_rows_are_forgotten_after_a_day(store: ServiceCredentialStore) -> None:
    now = datetime.now(UTC)
    store.rotate_due(KEK, now=now)
    store.issue("print", "k8s", pod_name="p", pod_uid="u", now=now)
    later = now + EXCHANGE_LIFETIME + timedelta(days=1, seconds=1)
    assert store.delete_expired(now=later) == 1
    assert {c.proof for c in store.list()} == {"legacy"}


# The callers' readers (§6.3, §8 step 1).


class FakeBackend:
    """The exchange route as the API answers it."""

    def __init__(self) -> None:
        self.status = 200
        self.calls: list[dict[str, list[str]]] = []
        self.issued = 0
        self.retry_after = "7"

    def handler(self, request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs

        assert request.url.path == "/api/v1/auth/service-token"
        self.calls.append(parse_qs(request.content.decode()))
        if self.status == 200:
            self.issued += 1
            token = f"sbsvc_{self.issued:016x}." + "a" * 43
            return httpx.Response(
                200,
                json={
                    "access_token": token,
                    "token_type": "Bearer",
                    "expires_in": 900,
                    "scope": "internal.library internal.outputs",
                    "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                },
            )
        headers = {"Retry-After": self.retry_after} if self.status == 429 else {}
        return httpx.Response(self.status, headers=headers, json={"detail": "no"})


class FixedFallback:
    def __init__(self, token: str | None) -> None:
        self.value = token
        self.reads = 0

    def token(self) -> str | None:
        self.reads += 1
        return self.value

    def invalidate(self, token: str) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _reader(
    tmp_path: Path,
    backend: FakeBackend,
    clock: Clock,
    *,
    fallback: FixedFallback | None = None,
    proof: str = "k8s",
) -> ExchangeReader:
    proof_file = tmp_path / "proof"
    proof_file.write_text("jwt-1\n")
    return ExchangeReader(
        "print",
        "http://api:8080/",
        proof_file,
        proof,  # type: ignore[arg-type]
        fallback=fallback,
        clock=clock,
        transport=httpx.MockTransport(backend.handler),
    )


def test_the_reader_exchanges_and_renews_at_half_life(tmp_path: Path) -> None:
    backend, clock = FakeBackend(), Clock()
    reader = _reader(tmp_path, backend, clock)
    first = reader.token()
    assert first is not None and first.startswith("sbsvc_")
    (call,) = backend.calls
    assert call == {
        "grant_type": ["urn:ietf:params:oauth:grant-type:token-exchange"],
        "subject_token": ["jwt-1"],
        "subject_token_type": ["urn:ietf:params:oauth:token-type:jwt"],
        # Must match the caller the proof proves; never believed.
        "client_id": ["print"],
    }
    clock.now += 449
    assert reader.token() == first
    assert len(backend.calls) == 1
    # The projected token was rotated by the kubelet: the file is read again.
    (tmp_path / "proof").write_text("jwt-2")
    clock.now += 1
    second = reader.token()
    assert second != first
    assert backend.calls[-1]["subject_token"] == ["jwt-2"]


def test_the_bootstrap_proof_type(tmp_path: Path) -> None:
    backend = FakeBackend()
    _reader(tmp_path, backend, Clock(), proof="bootstrap").token()
    assert backend.calls[0]["subject_token_type"] == ["urn:scadbuddy:params:token-type:bootstrap"]


def test_a_failed_exchange_keeps_the_token_and_backs_off(tmp_path: Path) -> None:
    backend, clock = FakeBackend(), Clock()
    reader = _reader(tmp_path, backend, clock)
    token = reader.token()
    backend.status = 503
    clock.now += 450
    assert reader.token() == token
    calls = len(backend.calls)
    # 5 s, then 10, 20, 40, 60, 60: never faster.
    for delay in (5, 10, 20, 40, 60, 60):
        clock.now += delay - 0.5
        reader.token()
        assert len(backend.calls) == calls
        clock.now += 0.5
        reader.token()
        calls += 1
        assert len(backend.calls) == calls
    # Kept until it expires, then none.
    clock.now = 1000.0 + 900
    assert reader.token() is None


def test_a_429_waits_its_retry_after(tmp_path: Path) -> None:
    backend, clock = FakeBackend(), Clock()
    reader = _reader(tmp_path, backend, clock)
    backend.status = 429
    backend.retry_after = "30"
    assert reader.token() is None
    clock.now += 29
    reader.token()
    assert len(backend.calls) == 1
    backend.status = 200
    clock.now += 1
    assert reader.token() is not None


def test_a_401_exchanges_again_at_once_but_not_in_a_loop(tmp_path: Path) -> None:
    backend, clock = FakeBackend(), Clock()
    reader = _reader(tmp_path, backend, clock)
    first = reader.token()
    assert first is not None
    clock.now += 20
    reader.invalidate(first)
    second = reader.token()
    assert second is not None and second != first
    # A second 401 right after does not exchange again within 10 s.
    reader.invalidate(second)
    assert reader.token() is None
    assert len(backend.calls) == 2
    clock.now += 10
    assert reader.token() is not None
    # A stale token's 401 changes nothing.
    calls = len(backend.calls)
    reader.invalidate(first)
    assert len(backend.calls) == calls


def test_an_old_api_answering_404_falls_back_to_the_view(tmp_path: Path) -> None:
    backend, clock = FakeBackend(), Clock()
    view = FixedFallback("sbsvc_0000000000000001." + "b" * 43)
    reader = _reader(tmp_path, backend, clock, fallback=view)
    backend.status = 404
    assert reader.token() == view.value
    # The exchange is tried again a minute later; the view serves meanwhile.
    clock.now += 30
    assert reader.token() == view.value
    assert len(backend.calls) == 1
    backend.status = 200
    clock.now += 30
    token = reader.token()
    assert token is not None and token != view.value


def test_a_404_without_a_database_url_is_no_token(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.status = 404
    assert _reader(tmp_path, backend, Clock()).token() is None


def test_a_missing_proof_file_is_no_token(tmp_path: Path) -> None:
    backend = FakeBackend()
    reader = _reader(tmp_path, backend, Clock())
    (tmp_path / "proof").unlink()
    assert reader.token() is None
    assert backend.calls == []


async def test_service_auth_drops_a_token_the_api_refused(tmp_path: Path) -> None:
    """On a 401 the reader exchanges at the next request; the refused request is not
    sent again, since a write might then run twice."""
    backend, clock = FakeBackend(), Clock()
    seen: list[str | None] = []

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/service-token":
            return backend.handler(request)
        seen.append(request.headers.get("authorization"))
        return httpx.Response(401 if len(seen) == 1 else 200)

    proof_file = tmp_path / "proof"
    proof_file.write_text("jwt-1")
    reader = ExchangeReader(
        "print",
        "http://api:8080",
        proof_file,
        "k8s",
        clock=clock,
        transport=httpx.MockTransport(api),
    )
    async with httpx.AsyncClient(
        base_url="http://api:8080", auth=ServiceAuth(reader), transport=httpx.MockTransport(api)
    ) as client:
        assert (await client.post("/api/v1/internal/x")).status_code == 401
        clock.now += 10
        assert (await client.post("/api/v1/internal/x")).status_code == 200
    assert len(seen) == 2
    assert seen[0] is not None and seen[1] is not None
    assert seen[0] != seen[1]


@pg
def test_the_in_process_provider_mints_and_renews(store: ServiceCredentialStore) -> None:
    clock = Clock()
    provider = InProcessProvider("projects", lambda: store, clock=clock)
    first = provider.token()
    assert first is not None
    assert store.resolve(first) == Service("projects", SCOPES["projects"])
    assert provider.token() == first
    clock.now += EXCHANGE_LIFETIME.total_seconds() / 2
    second = provider.token()
    assert second != first
    assert [c.proof for c in store.list()] == ["inprocess", "inprocess"]
    store.suspend("projects", by="user:i#s")
    clock.now += EXCHANGE_LIFETIME.total_seconds()
    assert provider.token() is None


# Which source a caller reads (§7.2, §8 step 1).


def _settings(**update: Any) -> Settings:
    return Settings(
        database_url=UNUSED_DATABASE_URL, temporal_address=UNUSED_TEMPORAL_ADDRESS, **update
    )


def test_a_caller_without_a_proof_reads_its_view() -> None:
    assert isinstance(token_source("print", _settings(), KEK), TokenReader)


def test_a_caller_with_a_proof_exchanges_it(tmp_path: Path) -> None:
    proof = tmp_path / "token"
    for update in ({"service_token_file": proof}, {"service_bootstrap_file": proof}):
        settings = _settings(api_internal_url="http://scadbuddy:8080", **update)
        assert isinstance(token_source("projects", settings, None), ExchangeReader)
    with pytest.raises(ServiceCredentialConfigError):
        token_source("print", _settings(service_token_file=proof), KEK)


def test_both_proofs_is_a_start_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not both"):
        _settings(service_token_file=tmp_path / "a", service_bootstrap_file=tmp_path / "b")


def test_the_namespace_is_a_namespace() -> None:
    assert _settings(service_account_namespace=" bambuddy ").service_account_namespace == (
        "bambuddy"
    )
    with pytest.raises(ValidationError):
        _settings(service_account_namespace="Bad_Name")
