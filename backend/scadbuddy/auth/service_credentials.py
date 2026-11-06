"""Service credentials (#4975 phase 4, spec 2026-11-01 §7.1, §7.2; D4).

An opaque bearer token per caller, ``sbsvc_<id>.<secret>``, issued and rotated by the
API: at start and on the prune Schedule each caller's newest live credential is checked,
and when it has fewer than :data:`ROTATE_BEFORE` left the next one is minted for
:data:`LIFETIME`, so the two overlap and a rotation never cuts a caller off. A caller
reads its current token from ``service_credentials_<caller>`` (granted to its own
Postgres role only, when the deployment made one) and unseals it with the KEK
(:class:`TokenReader`). Verification is the SHA-256; the sealed copy exists only for
that hand-off.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import threading
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args

import httpx
import psycopg
from psycopg import Connection, sql
from psycopg.rows import dict_row, tuple_row

from scadbuddy.auth.models import Service
from scadbuddy.auth.store import Pool
from scadbuddy.core.secrets import Envelope, Kek, open_secret, seal_secret

logger = logging.getLogger(__name__)

ServiceCaller = Literal["agent", "print", "projects"]
CALLERS: tuple[ServiceCaller, ...] = get_args(ServiceCaller)

#: What each caller may reach, as route classes (§7.1, `auth/routes.py`). The projects
#: worker's flow host routes are `api` routes; phase 5 narrows what it may assert.
SCOPES: Mapping[ServiceCaller, frozenset[str]] = {
    "agent": frozenset({"api", "internal.workspace"}),
    "print": frozenset({"internal.outputs", "internal.library"}),
    "projects": frozenset({"api"}),
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
        "SELECT 1 FROM service_credentials WHERE caller = %s AND revoked_at IS NULL"
        " AND expires_at > %s LIMIT 1",
        (caller, at),
    ).fetchone()
    return row is not None


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

    def mint(self, kek: Kek, caller: ServiceCaller, *, now: datetime | None = None) -> str:
        """A new credential for ``caller`` whatever it holds ("rotate now")."""
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, caller)
            return _insert(conn, kek, caller, _now(now))

    def rotate_due(self, kek: Kek, *, now: datetime | None = None) -> list[str]:
        """Mints the next credential of every caller whose newest live one has fewer
        than :data:`ROTATE_BEFORE` left, is sealed under another KEK (a rotated key the
        caller can no longer open), or is missing; the ids minted. Each caller is
        checked and minted under its lock, so a replica's start, the prune Schedule
        and a revoke's replacement never mint two at once (#5393)."""
        at = _now(now)
        minted = []
        for caller in CALLERS:
            with self._pool.connection() as conn, conn.transaction():
                _lock(conn, caller)
                row = conn.execute(
                    "SELECT expires_at, kek_id FROM service_credentials"
                    " WHERE caller = %s AND revoked_at IS NULL AND expires_at > %s"
                    " ORDER BY expires_at DESC LIMIT 1",
                    (caller, at),
                ).fetchone()
                if row is None or row["expires_at"] - at < ROTATE_BEFORE or row["kek_id"] != kek.id:
                    minted.append(_insert(conn, kek, caller, at))
        return minted

    def replace_if_none(
        self, kek: Kek, caller: ServiceCaller, *, now: datetime | None = None
    ) -> str | None:
        """Mints one for ``caller`` when it has none live, under its lock (#5393); the
        id minted, or None when one was live."""
        at = _now(now)
        with self._pool.connection() as conn, conn.transaction():
            _lock(conn, caller)
            if _has_live(conn, caller, at):
                return None
            return _insert(conn, kek, caller, at)

    def resolve(self, token: str, *, now: datetime | None = None) -> Service | None:
        credential_hash = token_hash(token)
        if credential_hash is None:
            return None
        at = _now(now)
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id, caller, scopes, last_used_at FROM service_credentials"
                " WHERE secret_hash = %s AND revoked_at IS NULL AND expires_at > %s",
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
                "SELECT id, caller, scopes, created_at, expires_at, revoked_at, last_used_at"
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

    def delete_expired(
        self, *, older_than: timedelta = LIFETIME, now: datetime | None = None
    ) -> int:
        """Forgets credentials expired or revoked more than ``older_than`` ago."""
        cutoff = _now(now) - older_than
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM service_credentials WHERE expires_at < %(cutoff)s"
                " OR revoked_at < %(cutoff)s",
                {"cutoff": cutoff},
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


class ServiceAuth(httpx.Auth):
    """``Authorization: Bearer <token>`` on every request, when the reader has one."""

    def __init__(self, reader: TokenReader) -> None:
        self._reader = reader

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await asyncio.to_thread(self._reader.token)
        if token is not None:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request
