"""Service credentials (#4975 phase 4, spec 2026-11-01 §7.1, §7.2; D4).

An opaque bearer token per caller, ``sbsvc_<id>.<secret>``, issued and rotated by the
API: at start and on the prune Schedule each caller's newest live credential is checked,
and when it has fewer than :data:`ROTATE_BEFORE` left the next one is minted for
:data:`LIFETIME`, so the two overlap and a rotation never cuts a caller off. A caller
reads its current token from ``service_credentials_<caller>`` (granted to its own
Postgres role only, when the deployment made one) and unseals it with the KEK
(:class:`TokenReader`). Verification is the SHA-256; the sealed copy exists only for
that hand-off.

#5494 (spec 2026-11-05 §6, §7, §8 step 1): a caller configured with a proof
(``SCADBUDDY_SERVICE_TOKEN_FILE`` or ``SCADBUDDY_SERVICE_BOOTSTRAP_FILE``) exchanges it at
``POST /api/v1/auth/service-token`` instead (:class:`ExchangeReader`), and the API's own
in-process callers mint one in memory (:class:`InProcessProvider`). Those credentials
(:meth:`ServiceCredentialStore.issue`) live :data:`EXCHANGE_LIFETIME`, have no sealed copy
and resolve exactly as the sealed ones do. A suspended caller (§6.5) gets and resolves
none.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import logging
import math
import re
import secrets
import threading
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args

import httpx
import psycopg
from psycopg import Connection, sql
from psycopg.rows import dict_row, tuple_row

from scadbuddy.auth.models import Service
from scadbuddy.auth.store import Pool
from scadbuddy.core.secrets import Envelope, Kek, open_secret, seal_secret

if TYPE_CHECKING:
    from scadbuddy.core.settings import Settings

logger = logging.getLogger(__name__)

ServiceCaller = Literal["agent", "print", "projects", "render"]
CALLERS: tuple[ServiceCaller, ...] = get_args(ServiceCaller)
#: The callers the API mints sealed 30-day credentials for, read through their views.
#: The render worker is a caller of the exchange only (spec 2026-11-05 §11 D8).
LegacyCaller = Literal["agent", "print", "projects"]
LEGACY_CALLERS: tuple[LegacyCaller, ...] = get_args(LegacyCaller)

#: How a credential's caller proved itself: ``legacy`` is a sealed row read through a
#: view; the others are minted by the exchange (§6.2) or in process (§7.1).
Proof = Literal["legacy", "k8s", "bootstrap", "inprocess"]
ExchangedProof = Literal["k8s", "bootstrap", "inprocess"]

#: What each caller may reach, as route classes (§7.1, `auth/routes.py`). The projects
#: worker's flow host routes are `api` routes; phase 5 narrows what it may assert. The
#: render worker's `crud.render` names no route yet (#5298 adds them), so it reaches none.
SCOPES: Mapping[ServiceCaller, frozenset[str]] = {
    "agent": frozenset({"api", "internal.workspace"}),
    "print": frozenset({"internal.outputs", "internal.library"}),
    "projects": frozenset({"api"}),
    "render": frozenset({"crud.render"}),
}
#: The Postgres role of each caller, granted its view when the deployment made it.
ROLES: Mapping[ServiceCaller, str] = {
    "agent": "scadbuddy_agent",
    "print": "scadbuddy_print",
    "projects": "scadbuddy_projects",
}

#: The views a caller reads besides its own credentials': the agent checks a person's
#: session on its own routes (#4975 phase 5, spec §5.6).
VIEWS: Mapping[ServiceCaller, tuple[str, ...]] = {
    "agent": ("service_credentials_agent", "auth_sessions_agent"),
    "print": ("service_credentials_print",),
    "projects": ("service_credentials_projects",),
}

PREFIX = "sbsvc_"
LIFETIME = timedelta(days=30)
ROTATE_BEFORE = timedelta(days=7)
#: An exchanged credential's lifetime (spec 2026-11-05 §11 D4); the caller renews it at
#: half-life.
EXCHANGE_LIFETIME = timedelta(minutes=15)
#: An exchanged row is forgotten this long after it expired or was revoked (§6.3).
EXCHANGE_RETENTION = timedelta(days=1)
#: §11 D5: exchanges a minute per pod and per caller, then 429.
RATE_WINDOW = timedelta(minutes=1)
RATE_PER_POD = 6
RATE_PER_CALLER = 30
#: ``last_used_at`` is written at most this often per credential.
TOUCH_INTERVAL = timedelta(minutes=1)
_TOKEN_RE = re.compile(r"sbsvc_([0-9a-f]{16})\.([A-Za-z0-9_-]{43})")


def aad(credential_id: str, caller: str, scopes: frozenset[str] | list[str]) -> str:
    """Binds a sealed token to its row, so it cannot be moved to a wider one."""
    return f"service_credentials|{credential_id}|{caller}|{','.join(sorted(scopes))}"


def token_hash(token: str) -> bytes | None:
    if _TOKEN_RE.fullmatch(token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).digest()


@dataclass(frozen=True)
class CredentialInfo:
    id: str
    caller: ServiceCaller
    scopes: list[str]
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    proof: Proof
    pod_name: str | None
    pod_uid: str | None


@dataclass(frozen=True)
class Issued:
    """An exchanged credential: the token is in memory only, never stored."""

    token: str
    credential_id: str
    scopes: frozenset[str]
    expires_at: datetime


@dataclass(frozen=True)
class Suspension:
    caller: ServiceCaller
    suspended_at: datetime
    suspended_by: str


class CallerSuspendedError(Exception):
    """The caller is suspended (§6.5): no credential until a person resumes it."""


class RateLimitedError(Exception):
    """§11 D5: too many exchanges this minute; ``retry_after`` seconds until one more."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"retry after {retry_after} s")
        self.retry_after = retry_after


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _lock(conn: Connection[Any], caller: ServiceCaller) -> None:
    """Serialises every mint of ``caller``'s credentials until the transaction ends."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:service_credentials:' || %s, 0))",
        (caller,),
    )


def _has_live(conn: Connection[Any], caller: ServiceCaller, at: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM service_credentials WHERE caller = %s AND proof = 'legacy'"
        " AND revoked_at IS NULL AND expires_at > %s LIMIT 1",
        (caller, at),
    ).fetchone()
    return row is not None


def _suspended(conn: Connection[Any], caller: ServiceCaller) -> bool:
    row = conn.execute(
        "SELECT 1 FROM service_caller_suspensions WHERE caller = %s", (caller,)
    ).fetchone()
    return row is not None


def _retry_after(
    conn: Connection[Any], where: sql.Composable, args: tuple[Any, ...], limit: int, at: datetime
) -> int | None:
    """Seconds until fewer than ``limit`` exchanged rows matching ``where`` were made in
    the last :data:`RATE_WINDOW`, or ``None`` when fewer already were."""
    rows = conn.execute(
        sql.SQL(
            "SELECT created_at FROM service_credentials WHERE {} AND proof IN ('k8s', 'bootstrap')"
            " AND created_at > %s AND created_at <= %s ORDER BY created_at DESC LIMIT %s"
        ).format(where),
        (*args, at - RATE_WINDOW, at, limit),
    ).fetchall()
    if len(rows) < limit:
        return None
    free_at: datetime = rows[-1]["created_at"] + RATE_WINDOW
    return max(1, math.ceil((free_at - at).total_seconds()))


def _insert(conn: Connection[Any], kek: Kek, caller: ServiceCaller, at: datetime) -> str:
    credential_id = secrets.token_hex(8)
    token = f"{PREFIX}{credential_id}.{secrets.token_urlsafe(32)}"
    scopes = sorted(SCOPES[caller])
    envelope = seal_secret(kek, token, aad(credential_id, caller, scopes))
    conn.execute(
        "INSERT INTO service_credentials (id, caller, scopes, secret_hash,"
        " secret_sealed, dek_sealed, kek_id, created_at, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            credential_id,
            caller,
            scopes,
            token_hash(token),
            envelope.secret_sealed,
            envelope.dek_sealed,
            envelope.kek_id,
            at,
            at + LIFETIME,
        ),
    )
    return credential_id


class ServiceCredentialStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def mint(self, kek: Kek, caller: LegacyCaller, *, now: datetime | None = None) -> str:
        """A new credential for ``caller`` whatever it holds ("rotate now")."""
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, caller)
            return _insert(conn, kek, caller, _now(now))

    def rotate_due(self, kek: Kek, *, now: datetime | None = None) -> list[str]:
        """Mints the next credential of every legacy caller whose newest live sealed one
        has fewer than :data:`ROTATE_BEFORE` left, is sealed under another KEK (a rotated
        key the caller can no longer open), or is missing; the ids minted. Each caller is
        checked and minted under its lock, so a replica's start, the prune Schedule
        and a revoke's replacement never mint two at once (#5393). A suspended caller
        gets none."""
        at = _now(now)
        minted = []
        for caller in LEGACY_CALLERS:
            with self._pool.connection() as conn, conn.transaction():
                _lock(conn, caller)
                if _suspended(conn, caller):
                    continue
                row = conn.execute(
                    "SELECT expires_at, kek_id FROM service_credentials"
                    " WHERE caller = %s AND proof = 'legacy' AND revoked_at IS NULL"
                    " AND expires_at > %s ORDER BY expires_at DESC LIMIT 1",
                    (caller, at),
                ).fetchone()
                if row is None or row["expires_at"] - at < ROTATE_BEFORE or row["kek_id"] != kek.id:
                    minted.append(_insert(conn, kek, caller, at))
        return minted

    def replace_if_none(
        self, kek: Kek, caller: ServiceCaller, *, now: datetime | None = None
    ) -> str | None:
        """Mints one for ``caller`` when it has no sealed one live, under its lock
        (#5393); the id minted, or None when one was live, the caller is not a legacy
        one, or it is suspended."""
        at = _now(now)
        if caller not in LEGACY_CALLERS:
            return None
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, caller)
            if _has_live(conn, caller, at) or _suspended(conn, caller):
                return None
            return _insert(conn, kek, caller, at)

    def issue(
        self,
        caller: ServiceCaller,
        proof: ExchangedProof,
        *,
        pod_name: str | None = None,
        pod_uid: str | None = None,
        rate_limited: bool = True,
        now: datetime | None = None,
    ) -> Issued:
        """An exchanged credential for ``caller`` (spec 2026-11-05 §6.2): no sealed copy,
        :data:`EXCHANGE_LIFETIME`, bound to the pod a Kubernetes proof named. Under the
        caller's lock, so every replica counts the same rows. Raises
        :class:`CallerSuspendedError`, or :class:`RateLimitedError` past
        :data:`RATE_PER_POD` a minute per pod (a bootstrap caller is one pod) or
        :data:`RATE_PER_CALLER` per caller."""
        at = _now(now)
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, caller)
            if _suspended(conn, caller):
                raise CallerSuspendedError(caller)
            if rate_limited:
                pod: tuple[sql.Composable, tuple[Any, ...]] = (
                    (sql.SQL("pod_uid = %s"), (pod_uid,))
                    if pod_uid is not None
                    else (
                        sql.SQL("caller = %s AND proof = %s AND pod_uid IS NULL"),
                        (caller, proof),
                    )
                )
                waits = [
                    _retry_after(conn, pod[0], pod[1], RATE_PER_POD, at),
                    _retry_after(conn, sql.SQL("caller = %s"), (caller,), RATE_PER_CALLER, at),
                ]
                wait = max((w for w in waits if w is not None), default=None)
                if wait is not None:
                    raise RateLimitedError(wait)
            credential_id = secrets.token_hex(8)
            token = f"{PREFIX}{credential_id}.{secrets.token_urlsafe(32)}"
            scopes = SCOPES[caller]
            expires_at = at + EXCHANGE_LIFETIME
            conn.execute(
                "INSERT INTO service_credentials (id, caller, scopes, secret_hash, created_at,"
                " expires_at, proof, pod_name, pod_uid)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    credential_id,
                    caller,
                    sorted(scopes),
                    token_hash(token),
                    at,
                    expires_at,
                    proof,
                    pod_name,
                    pod_uid,
                ),
            )
        return Issued(token, credential_id, scopes, expires_at)

    def resolve(self, token: str, *, now: datetime | None = None) -> Service | None:
        credential_hash = token_hash(token)
        if credential_hash is None:
            return None
        at = _now(now)
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id, caller, scopes, last_used_at FROM service_credentials c"
                " WHERE secret_hash = %s AND revoked_at IS NULL AND expires_at > %s"
                " AND NOT EXISTS (SELECT 1 FROM service_caller_suspensions s"
                " WHERE s.caller = c.caller)",
                (credential_hash, at),
            ).fetchone()
            if row is None:
                return None
            last = row["last_used_at"]
            if last is None or at - last >= TOUCH_INTERVAL:
                conn.execute(
                    "UPDATE service_credentials SET last_used_at = %s WHERE id = %s",
                    (at, row["id"]),
                )
        return Service(row["caller"], frozenset(row["scopes"]))

    def list(self) -> list[CredentialInfo]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, caller, scopes, created_at, expires_at, revoked_at, last_used_at,"
                " proof, pod_name, pod_uid"
                " FROM service_credentials ORDER BY caller, created_at DESC"
            ).fetchall()
        return [CredentialInfo(**row) for row in rows]

    def revoke(self, credential_id: str, *, now: datetime | None = None) -> ServiceCaller | None:
        """Revokes it; its caller, or None when no live one had that id."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE service_credentials SET revoked_at = %s"
                " WHERE id = %s AND revoked_at IS NULL RETURNING caller",
                (_now(now), credential_id),
            ).fetchone()
        return None if row is None else row["caller"]

    def revoke_pod(self, pod_uid: str, *, now: datetime | None = None) -> int:
        """§6.5: revokes every live credential bound to that pod; how many."""
        at = _now(now)
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE service_credentials SET revoked_at = %s"
                " WHERE pod_uid = %s AND revoked_at IS NULL AND expires_at > %s",
                (at, pod_uid, at),
            )
        return cur.rowcount

    def suspend(self, caller: ServiceCaller, *, by: str, now: datetime | None = None) -> None:
        """§6.5: refuses the caller's exchanges and lookups, and revokes every credential
        it holds, until :meth:`resume`. Under its lock, so no mint races it."""
        at = _now(now)
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, caller)
            conn.execute(
                "INSERT INTO service_caller_suspensions (caller, suspended_at, suspended_by)"
                " VALUES (%s, %s, %s) ON CONFLICT (caller) DO NOTHING",
                (caller, at, by),
            )
            conn.execute(
                "UPDATE service_credentials SET revoked_at = %s"
                " WHERE caller = %s AND revoked_at IS NULL",
                (at, caller),
            )

    def resume(self, caller: ServiceCaller) -> bool:
        """Lifts a suspension; whether there was one."""
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, caller)
            cur = conn.execute(
                "DELETE FROM service_caller_suspensions WHERE caller = %s", (caller,)
            )
        return cur.rowcount > 0

    def suspensions(self) -> builtins.list[Suspension]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT caller, suspended_at, suspended_by FROM service_caller_suspensions"
                " ORDER BY caller"
            ).fetchall()
        return [Suspension(**row) for row in rows]

    def delete_expired(
        self,
        *,
        older_than: timedelta = LIFETIME,
        exchanged_older_than: timedelta = EXCHANGE_RETENTION,
        now: datetime | None = None,
    ) -> int:
        """Forgets sealed credentials expired or revoked more than ``older_than`` ago,
        and exchanged ones more than ``exchanged_older_than`` ago (§6.3)."""
        at = _now(now)
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM service_credentials WHERE"
                " (proof = 'legacy' AND (expires_at < %(legacy)s OR revoked_at < %(legacy)s))"
                " OR (proof <> 'legacy'"
                " AND (expires_at < %(exchanged)s OR revoked_at < %(exchanged)s))",
                {"legacy": at - older_than, "exchanged": at - exchanged_older_than},
            )
        return cur.rowcount


def grant_caller_views(conn: Connection[Any]) -> list[str]:
    """Grants each caller's role, where the deployment made it, its own views only
    (:data:`VIEWS`); the roles granted. Idempotent and never revokes, as
    `render/worker_role.py`."""
    granted = []
    for caller, role in ROLES.items():
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
            continue
        schema = conn.cursor(row_factory=tuple_row).execute("SELECT current_schema()").fetchone()
        assert schema is not None
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(str(schema[0])), sql.Identifier(role)
            )
        )
        for view in VIEWS[caller]:
            conn.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    sql.Identifier(view), sql.Identifier(role)
                )
            )
        granted.append(role)
    return granted


class TokenSource(Protocol):
    """Where a caller's current credential comes from: ``token`` is called on a worker
    thread before each request, ``invalidate`` with a token the API answered 401 to."""

    def token(self) -> str | None: ...

    def invalidate(self, token: str) -> None: ...


#: How often a caller reads its view again (§7.2: at start and every hour).
REREAD_SECONDS = 3600.0
#: How soon it reads again while it has no token: one started before the API minted
#: its credential, or whose read failed (review of #5211).
RETRY_SECONDS = 60.0


class TokenReader:
    """A caller's current token, read from its view and unsealed with the KEK; ``None``
    without a KEK or a live credential. The newest live one is presented, so a rotation
    reaches the caller within :data:`REREAD_SECONDS`, inside the seven days' overlap."""

    def __init__(
        self,
        caller: ServiceCaller,
        database_url: str,
        kek: Kek | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.caller = caller
        self._database_url = database_url
        self._kek = kek
        self._clock = clock
        self._lock = threading.Lock()
        self._read_at: float | None = None
        self._token: str | None = None

    def token(self) -> str | None:
        if self._kek is None:
            return None
        with self._lock:
            now = self._clock()
            wait = REREAD_SECONDS if self._token is not None else RETRY_SECONDS
            if self._read_at is not None and now - self._read_at < wait:
                return self._token
            try:
                self._token = self._read(self._kek)
            except psycopg.Error as error:
                # Keep the last token: it is good for at least the overlap.
                logger.warning(
                    "could not read the %s service credential (%s)",
                    self.caller,
                    type(error).__name__,
                )
            except Exception:
                logger.exception("could not open the %s service credential", self.caller)
            self._read_at = now
            return self._token

    def invalidate(self, token: str) -> None:
        """Nothing: a sealed credential is replaced by the rotation, read within the hour."""

    def _read(self, kek: Kek) -> str | None:
        with psycopg.connect(self._database_url, autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute(
                sql.SQL(
                    "SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id FROM {}"
                    " ORDER BY expires_at DESC LIMIT 1"
                ).format(sql.Identifier(f"service_credentials_{self.caller}"))
            ).fetchone()
        if row is None:
            return None
        envelope = Envelope(bytes(row["secret_sealed"]), bytes(row["dek_sealed"]), row["kek_id"])
        return open_secret(kek, envelope, aad(row["id"], row["caller"], row["scopes"]))


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

    def token(self) -> str | None:
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._renew_at:
                return self._token
            if now >= self._next_try:
                self._exchange(now)
            if self._token is not None and now < self._expires_at:
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

    def _exchange(self, now: float) -> None:
        try:
            proof = self._proof_file.read_text().strip()
        except OSError as error:
            self._failed(now, f"no proof: {type(error).__name__}")
            return
        if not proof:
            self._failed(now, "empty proof")
            return
        form = {
            "grant_type": GRANT_TYPE,
            "subject_token": proof,
            "subject_token_type": self._token_type,
            # The refusal bucket a bootstrap proof is limited in; the API never believes it.
            "client_id": self.caller,
        }
        try:
            with httpx.Client(transport=self._transport, timeout=EXCHANGE_TIMEOUT) as client:
                response = client.post(self._url, data=form)
        except httpx.HTTPError as error:
            self._failed(now, type(error).__name__)
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


class InProcessProvider:
    """§7.1 (E2): a credential the API mints for a caller it serves in its own process
    (the in-process `projects` worker), with ``proof = inprocess``: no route, no file,
    no KEK. Renewed at half-life; a mint that fails keeps the current one until it
    expires."""

    def __init__(
        self,
        caller: ServiceCaller,
        store: Callable[[], ServiceCredentialStore],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.caller = caller
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._renew_at = 0.0
        self._next_try = 0.0

    def token(self) -> str | None:
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._renew_at:
                return self._token
            if now >= self._next_try:
                try:
                    issued = self._store().issue(self.caller, "inprocess", rate_limited=False)
                except CallerSuspendedError:
                    logger.warning("the %s caller is suspended", self.caller)
                    self._next_try = now + RETRY_SECONDS
                except Exception as error:
                    logger.warning(
                        "could not mint the %s service credential (%s)",
                        self.caller,
                        type(error).__name__,
                    )
                    self._next_try = now + EXCHANGE_RETRY_FIRST
                else:
                    lifetime = EXCHANGE_LIFETIME.total_seconds()
                    self._token = issued.token
                    self._expires_at = now + lifetime
                    self._renew_at = self._next_try = now + lifetime / 2
            if self._token is not None and now < self._expires_at:
                return self._token
            return None

    def invalidate(self, token: str) -> None:
        with self._lock:
            if token == self._token:
                self._token = None


class ServiceCredentialConfigError(ValueError):
    """A caller's credential settings cannot be used: the process refuses to start."""


def token_source(caller: ServiceCaller, settings: Settings, kek: Kek | None) -> TokenSource:
    """The caller's credential as its settings say (spec 2026-11-05 §7.2, §8 step 1): an
    exchange of ``SCADBUDDY_SERVICE_TOKEN_FILE`` (a projected token) or
    ``SCADBUDDY_SERVICE_BOOTSTRAP_FILE``, falling back to the view while the caller has a
    database URL; otherwise the view, as before. Both files set is refused by
    `Settings` itself."""
    view = TokenReader(caller, settings.database_url, kek)
    proof: Literal["k8s", "bootstrap"]
    if settings.service_token_file is not None:
        proof_file, proof = settings.service_token_file, "k8s"
    elif settings.service_bootstrap_file is not None:
        proof_file, proof = settings.service_bootstrap_file, "bootstrap"
    else:
        return view
    if settings.api_internal_url is None:
        raise ServiceCredentialConfigError(
            "SCADBUDDY_API_INTERNAL_URL is required to exchange the service credential"
        )
    return ExchangeReader(
        caller,
        settings.api_internal_url,
        proof_file,
        proof,
        fallback=view if settings.database_url else None,
    )


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
