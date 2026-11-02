"""``auth_sessions`` (spec 2026-11-01 §5.2), over a small autocommit pool of dict rows as
``orders/store.py`` is. Rows are keyed by the token's SHA-256 (``tokens.py``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.auth.models import (
    ABSOLUTE_LIFETIME,
    IDLE_TIMEOUT,
    SLIDE_INTERVAL,
    AuthSession,
    SessionKind,
    principal_for,
)
from scadbuddy.auth.tokens import new_token, token_hash

Pool = ConnectionPool[Connection[DictRow]]

_COLUMNS = (
    "id_hash, principal, issuer, subject, name, email, kind, created_at, last_seen_at,"
    " idle_expires_at, absolute_expires_at, client_address, user_agent"
)
_LIVE = "revoked_at IS NULL AND idle_expires_at > %(now)s AND absolute_expires_at > %(now)s"


def _session(row: dict[str, Any]) -> AuthSession:
    return AuthSession(
        id_hash=bytes(row["id_hash"]),
        principal=row["principal"],
        issuer=row["issuer"],
        subject=row["subject"],
        name=row["name"],
        email=row["email"],
        kind=row["kind"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        idle_expires_at=row["idle_expires_at"],
        absolute_expires_at=row["absolute_expires_at"],
        client_address=row["client_address"],
        user_agent=row["user_agent"],
    )


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


class SessionStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def create(
        self,
        *,
        issuer: str,
        subject: str,
        kind: SessionKind,
        name: str | None = None,
        email: str | None = None,
        client_address: str | None = None,
        user_agent: str | None = None,
        lifetime: timedelta = ABSOLUTE_LIFETIME,
        now: datetime | None = None,
        principal: str | None = None,
    ) -> tuple[str, AuthSession]:
        """A new session, and its token: the only time the token exists outside the
        browser. A ``lifetime`` shorter than the idle timeout caps the idle deadline
        too, so a short session (§7.4, §9.3) never slides past it."""
        at = _now(now)
        token = new_token()
        absolute = at + lifetime
        with self._pool.connection() as conn:
            row = conn.execute(
                f"INSERT INTO auth_sessions (id_hash, principal, issuer, subject, name,"
                f" email, kind, created_at, last_seen_at, idle_expires_at,"
                f" absolute_expires_at, client_address, user_agent)"
                f" VALUES (%(id_hash)s, %(principal)s, %(issuer)s, %(subject)s, %(name)s,"
                f" %(email)s, %(kind)s, %(now)s, %(now)s, %(idle)s, %(absolute)s,"
                f" %(client_address)s, %(user_agent)s) RETURNING {_COLUMNS}",
                {
                    "id_hash": token_hash(token),
                    # A delegated session names the principal it acts for (§7.4).
                    "principal": principal or principal_for(issuer, subject),
                    "issuer": issuer,
                    "subject": subject,
                    "name": name,
                    "email": email,
                    "kind": kind,
                    "now": at,
                    "idle": min(at + IDLE_TIMEOUT, absolute),
                    "absolute": absolute,
                    "client_address": client_address,
                    "user_agent": user_agent,
                },
            ).fetchone()
        assert row is not None
        return token, _session(row)

    def resolve(self, token: str, *, now: datetime | None = None) -> AuthSession | None:
        """The live session ``token`` names, or None. Slides its idle deadline when it
        was last seen more than ``SLIDE_INTERVAL`` ago."""
        id_hash = token_hash(token)
        if id_hash is None:
            return None
        at = _now(now)
        params = {"id_hash": id_hash, "now": at}
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM auth_sessions WHERE id_hash = %(id_hash)s AND {_LIVE}",
                params,
            ).fetchone()
            if row is None or at - row["last_seen_at"] < SLIDE_INTERVAL:
                return None if row is None else _session(row)
            # The update checks liveness again: a revoke between the two statements wins.
            row = conn.execute(
                f"UPDATE auth_sessions SET last_seen_at = %(now)s,"
                f" idle_expires_at = LEAST(%(now)s + %(idle)s, absolute_expires_at)"
                f" WHERE id_hash = %(id_hash)s AND {_LIVE} RETURNING {_COLUMNS}",
                {**params, "idle": IDLE_TIMEOUT},
            ).fetchone()
        return None if row is None else _session(row)

    def revoke(
        self, id_hash: bytes, *, principal: str | None = None, now: datetime | None = None
    ) -> bool:
        """Whether a live session was revoked; with ``principal``, only one of theirs."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE auth_sessions SET revoked_at = %(now)s"
                " WHERE id_hash = %(id_hash)s AND revoked_at IS NULL"
                " AND (%(principal)s::text IS NULL OR principal = %(principal)s)",
                {"id_hash": id_hash, "principal": principal, "now": _now(now)},
            )
            return cur.rowcount == 1

    def list_live(self, principal: str, *, now: datetime | None = None) -> list[AuthSession]:
        """A person's live sessions, the most recently used first."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM auth_sessions WHERE principal = %(principal)s"
                f" AND {_LIVE} ORDER BY last_seen_at DESC",
                {"principal": principal, "now": _now(now)},
            ).fetchall()
        return [_session(row) for row in rows]

    def revoke_all(self, principal: str, *, now: datetime | None = None) -> int:
        """ "Sign out everywhere": how many sessions were revoked."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE auth_sessions SET revoked_at = %(now)s"
                " WHERE principal = %(principal)s AND revoked_at IS NULL",
                {"principal": principal, "now": _now(now)},
            )
            return cur.rowcount

    def delete_expired(self, *, now: datetime | None = None) -> int:
        """Drop every row no request can use again: revoked or past either deadline."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM auth_sessions WHERE revoked_at IS NOT NULL"
                " OR idle_expires_at <= %(now)s OR absolute_expires_at <= %(now)s",
                {"now": _now(now)},
            )
            return cur.rowcount
