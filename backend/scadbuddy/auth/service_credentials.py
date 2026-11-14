"""Service credentials (#4975 phase 4, spec 2026-11-01 §7.1, §7.2; D4).

An opaque bearer token per caller, ``sbsvc_<id>.<secret>``, issued and rotated by the
API: at start and on the prune Schedule each caller's newest live credential is checked,
and when it has fewer than :data:`ROTATE_BEFORE` left, or other scopes than
:data:`SCOPES` names, the next one is minted for :data:`LIFETIME`, so the two overlap and
a rotation never cuts a caller off. A caller
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

import builtins
import logging
import math
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, get_args

import psycopg
from psycopg import Connection, sql
from psycopg.rows import dict_row, tuple_row

from scadbuddy.auth.models import Service
from scadbuddy.auth.service_auth import (
    EXCHANGE_PATH as EXCHANGE_PATH,
)
from scadbuddy.auth.service_auth import (
    EXCHANGE_RETRY_FIRST,
)
from scadbuddy.auth.service_auth import (
    GRANT_TYPE as GRANT_TYPE,
)
from scadbuddy.auth.service_auth import (
    ExchangeReader as ExchangeReader,
)
from scadbuddy.auth.service_auth import (
    ServiceAuth as ServiceAuth,
)
from scadbuddy.auth.service_auth import (
    ServiceCaller as ServiceCaller,
)
from scadbuddy.auth.service_auth import (
    ServiceCredentialConfigError as ServiceCredentialConfigError,
)
from scadbuddy.auth.service_auth import (
    TokenSource as TokenSource,
)
from scadbuddy.auth.service_auth import (
    exchange_source as exchange_source,
)
from scadbuddy.auth.service_auth import (
    token_hash as token_hash,
)
from scadbuddy.auth.store import Pool
from scadbuddy.core.secrets import Envelope, Kek, open_secret, seal_secret

if TYPE_CHECKING:
    from scadbuddy.core.settings import Settings

logger = logging.getLogger(__name__)

CALLERS: tuple[ServiceCaller, ...] = get_args(ServiceCaller)
#: The callers the API mints sealed 30-day credentials for, read through their views.
#: The render worker is a caller of the exchange only (spec 2026-11-05 §11 D8), as are
#: the storefront edge (#5282) and the projects and print workers (#5298): they hold no
#: database, so a sealed one would be a live secret nothing reads, and the API revokes any
#: left (`revoke_retired_legacy`).
LegacyCaller = Literal["agent"]
LEGACY_CALLERS: tuple[LegacyCaller, ...] = get_args(LegacyCaller)

#: How a credential's caller proved itself: ``legacy`` is a sealed row read through a
#: view; the others are minted by the exchange (§6.2) or in process (§7.1).
Proof = Literal["legacy", "k8s", "bootstrap", "inprocess"]
ExchangedProof = Literal["k8s", "bootstrap", "inprocess"]

#: What each caller may reach, as route classes (§7.1, `auth/routes.py`). The projects
#: worker reaches only the public routes its flow host functions call (`flow.host`, #5298),
#: not `api`. The `crud.<caller>` classes are the CRUD routes under
#: `/internal/crud/<caller>/` (#5298); the render worker has no other class. The
#: storefront edge (#5282) reaches only its own, `/internal/crud/storefront/`. The agent
#: reports the assistant's spend on an order through its own (`crud.agent`, #5330).
SCOPES: Mapping[ServiceCaller, frozenset[str]] = {
    "agent": frozenset({"api", "internal.workspace", "crud.agent"}),
    "print": frozenset({"internal.outputs", "internal.library", "crud.print"}),
    "projects": frozenset({"flow.host", "crud.projects"}),
    "render": frozenset({"crud.render"}),
    "storefront": frozenset({"crud.storefront"}),
}
#: The Postgres role of each caller, granted its view when the deployment made it.
ROLES: Mapping[ServiceCaller, str] = {
    "agent": "scadbuddy_agent",
}

#: The views a caller reads besides its own credentials': the agent checks a person's
#: session on its own routes (#4975 phase 5, spec §5.6).
VIEWS: Mapping[ServiceCaller, tuple[str, ...]] = {
    "agent": ("service_credentials_agent", "auth_sessions_agent"),
}

PREFIX = "sbsvc_"
LIFETIME = timedelta(days=30)
ROTATE_BEFORE = timedelta(days=7)
#: A caller's newest live sealed credential, the one it is handed: latest to expire, then
#: latest created (`_insert`), then by id, so a tie always picks the same row.
NEWEST = " ORDER BY expires_at DESC, created_at DESC, id DESC LIMIT 1"
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


def aad(credential_id: str, caller: str, scopes: frozenset[str] | list[str]) -> str:
    """Binds a sealed token to its row, so it cannot be moved to a wider one."""
    return f"service_credentials|{credential_id}|{caller}|{','.join(sorted(scopes))}"


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
    # `created_at` strictly after the caller's last one (callers hold its lock), so the
    # newest of two minted at one instant is the later (`NEWEST`).
    conn.execute(
        "INSERT INTO service_credentials (id, caller, scopes, secret_hash,"
        " secret_sealed, dek_sealed, kek_id, created_at, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, GREATEST(%s, (SELECT max(created_at)"
        " + interval '1 microsecond' FROM service_credentials WHERE caller = %s)), %s)",
        (
            credential_id,
            caller,
            scopes,
            token_hash(token),
            envelope.secret_sealed,
            envelope.dek_sealed,
            envelope.kek_id,
            at,
            caller,
            at + LIFETIME,
        ),
    )
    return credential_id


class ServiceCredentialStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        self._listeners: builtins.list[Callable[[ServiceCaller], None]] = []

    def on_suspension_change(self, listener: Callable[[ServiceCaller], None]) -> None:
        """Calls ``listener`` with the caller after each :meth:`suspend` and :meth:`resume`
        in this process (the in-process provider drops its token)."""
        self._listeners.append(listener)

    def _changed(self, caller: ServiceCaller) -> None:
        for listener in self._listeners:
            listener(caller)

    def mint(self, kek: Kek, caller: LegacyCaller, *, now: datetime | None = None) -> str:
        """A new credential for ``caller`` whatever it holds ("rotate now")."""
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, caller)
            return _insert(conn, kek, caller, _now(now))

    def rotate_due(self, kek: Kek, *, now: datetime | None = None) -> list[str]:
        """Mints the next credential of every legacy caller whose newest live sealed one
        has fewer than :data:`ROTATE_BEFORE` left, is sealed under another KEK (a rotated
        key the caller can no longer open), carries other scopes than :data:`SCOPES` (a
        release changed them; the old one stays live until it expires, for the old pods
        of a roll), or is missing; the ids minted. Each caller is
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
                    "SELECT expires_at, kek_id, scopes FROM service_credentials"
                    " WHERE caller = %s AND proof = 'legacy' AND revoked_at IS NULL"
                    " AND expires_at > %s" + NEWEST,
                    (caller, at),
                ).fetchone()
                if (
                    row is None
                    or row["expires_at"] - at < ROTATE_BEFORE
                    or row["kek_id"] != kek.id
                    or frozenset(row["scopes"]) != SCOPES[caller]
                ):
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
        return Service(row["caller"], frozenset(row["scopes"]), credential_id=str(row["id"]))

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

    def revoke_retired_legacy(self, *, now: datetime | None = None) -> int:
        """Revokes every live sealed credential of a caller no longer in
        :data:`LEGACY_CALLERS` (the projects and print workers', #5298); how many. Idempotent."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE service_credentials SET revoked_at = %s WHERE proof = 'legacy'"
                " AND revoked_at IS NULL AND NOT (caller = ANY(%s))",
                (_now(now), list(LEGACY_CALLERS)),
            )
        return cur.rowcount

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
        self._changed(caller)

    def resume(self, caller: ServiceCaller) -> bool:
        """Lifts a suspension; whether there was one."""
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, caller)
            cur = conn.execute(
                "DELETE FROM service_caller_suspensions WHERE caller = %s", (caller,)
            )
        self._changed(caller)
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


#: How often a caller reads its view again (§7.2: at start and every hour).
REREAD_SECONDS = 3600.0
#: How soon it reads again while it has no token: one started before the API minted
#: its credential, or whose read failed (review of #5211).
RETRY_SECONDS = 60.0
#: How soon it reads again after a refusal (`invalidate`), but not sooner than this after
#: the last read: a route that keeps refusing cannot turn every request into a database
#: read, and the new credential (one the API minted at start for new scopes) is still
#: reached inside the CRUD client's 30 s retry budget.
REREAD_MIN_SECONDS = 5.0


class TokenReader:
    """A caller's current token, read from its view and unsealed with the KEK; ``None``
    without a KEK or a live credential. The newest live one is presented, so a rotation
    reaches the caller within :data:`REREAD_SECONDS`, inside the seven days' overlap, or
    within :data:`REREAD_MIN_SECONDS` of a refusal that invalidates it."""

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
        self._stale = False

    def token(self) -> str | None:
        if self._kek is None:
            return None
        with self._lock:
            now = self._clock()
            if self._stale:
                wait = REREAD_MIN_SECONDS
            else:
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
            self._stale = False
            return self._token

    def invalidate(self, token: str) -> None:
        """Reads again on the next :meth:`token`, :data:`REREAD_MIN_SECONDS` after the last
        read at the soonest: after a 401 or a 403 ``out_of_scope`` the newest one may be
        another (the API mints one at start when a release changed the caller's scopes).
        The refused token is kept until then."""
        with self._lock:
            if token == self._token:
                self._stale = True

    def _read(self, kek: Kek) -> str | None:
        with psycopg.connect(self._database_url, autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute(
                sql.SQL(
                    "SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id FROM {}" + NEWEST
                ).format(sql.Identifier(f"service_credentials_{self.caller}"))
            ).fetchone()
        if row is None:
            return None
        envelope = Envelope(bytes(row["secret_sealed"]), bytes(row["dek_sealed"]), row["kek_id"])
        return open_secret(kek, envelope, aad(row["id"], row["caller"], row["scopes"]))


class InProcessProvider:
    """§7.1 (E2): a credential the API mints for a caller it serves in its own process
    (the in-process `projects` worker), with ``proof = inprocess``: no route, no file,
    no KEK. Renewed at half-life; a mint that fails keeps the current one until it
    expires. A suspend or resume of its caller in this process drops the token, so the
    next call mints rather than presenting the one the suspension revoked."""

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
        self._listening = False

    def _suspension_changed(self, caller: ServiceCaller) -> None:
        if caller != self.caller:
            return
        with self._lock:
            self._token = None
            self._next_try = 0.0

    def token(self) -> str | None:
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._renew_at:
                return self._token
            if now >= self._next_try:
                try:
                    store = self._store()
                    if not self._listening:
                        store.on_suspension_change(self._suspension_changed)
                        self._listening = True
                    issued = store.issue(self.caller, "inprocess", rate_limited=False)
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
                self._next_try = 0.0


def token_source(caller: ServiceCaller, settings: Settings, kek: Kek | None) -> TokenSource:
    """The caller's credential as its settings say (spec 2026-11-05 §7.2, §8 step 1): an
    exchange of ``SCADBUDDY_SERVICE_TOKEN_FILE`` (a projected token) or
    ``SCADBUDDY_SERVICE_BOOTSTRAP_FILE``, falling back to the view while the caller has a
    database URL; otherwise the view, as before. Both files set is refused by
    `Settings` itself. The projects, render and print workers never take this: they take
    `exchange_source`, with no fallback (#5298)."""
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
