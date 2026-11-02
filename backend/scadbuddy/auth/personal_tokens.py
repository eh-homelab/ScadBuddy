"""Personal access tokens (#4975 phase 6, spec 2026-11-01 §7.5; D6): what `bambuddy-fs`
and the Go client present instead of a browser's session.

A signed-in person mints one in Settings ("Access tokens"); it is shown once and kept as
its SHA-256. It is ``sbpat_<id>.<secret>`` (32 random bytes), sent as ``Authorization:
Bearer``. Its scope is ``read`` (safe methods only) or ``write``; it acts as the person who
minted it on the ``api`` routes, never on a ``person`` route (`auth/routes.py`). A token is a
bearer secret: never log one.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from scadbuddy.auth.models import PersonalToken
from scadbuddy.auth.store import Pool

TokenScope = Literal["read", "write"]
PREFIX = "sbpat_"
#: The longest a token may live, and the default.
MAX_LIFETIME = timedelta(days=365)
DEFAULT_LIFETIME = timedelta(days=90)
#: ``last_used_at`` is written at most this often per token.
TOUCH_INTERVAL = timedelta(minutes=1)
#: How long an expired or revoked token stays listed before it is forgotten.
KEEP = timedelta(days=30)
_TOKEN_RE = re.compile(r"sbpat_([0-9a-f]{16})\.([A-Za-z0-9_-]{43})")


def token_hash(token: str) -> bytes | None:
    if _TOKEN_RE.fullmatch(token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).digest()


@dataclass(frozen=True)
class TokenInfo:
    id: str
    name: str
    scope: TokenScope
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


_COLUMNS = "id, name, scope, created_at, expires_at, last_used_at, revoked_at"


class PersonalTokenStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def mint(
        self,
        principal: str,
        name: str,
        scope: TokenScope,
        lifetime: timedelta,
        *,
        now: datetime | None = None,
    ) -> tuple[str, TokenInfo]:
        """A new token and its row: the only time the token exists outside its holder."""
        at = _now(now)
        token_id = secrets.token_hex(8)
        token = f"{PREFIX}{token_id}.{secrets.token_urlsafe(32)}"
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO personal_access_tokens (id, principal, name, scope, token_hash,"
                " created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING {_COLUMNS}",
                (token_id, principal, name, scope, token_hash(token), at, at + lifetime),
            ).fetchone()
        assert row is not None
        return token, TokenInfo(**row)

    def resolve(self, token: str, *, now: datetime | None = None) -> PersonalToken | None:
        digest = token_hash(token)
        if digest is None:
            return None
        at = _now(now)
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id, principal, scope, last_used_at FROM personal_access_tokens"
                " WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > %s",
                (digest, at),
            ).fetchone()
            if row is None:
                return None
            last = row["last_used_at"]
            if last is None or at - last >= TOUCH_INTERVAL:
                conn.execute(
                    "UPDATE personal_access_tokens SET last_used_at = %s WHERE id = %s",
                    (at, row["id"]),
                )
        return PersonalToken(token_id=row["id"], principal=row["principal"], scope=row["scope"])

    def list(self, principal: str) -> list[TokenInfo]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM personal_access_tokens WHERE principal = %s"
                " ORDER BY created_at DESC",
                (principal,),
            ).fetchall()
        return [TokenInfo(**row) for row in rows]

    def revoke(self, principal: str, token_id: str, *, now: datetime | None = None) -> bool:
        """Revokes one of ``principal``'s own live tokens; whether there was one."""
        at = _now(now)
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE personal_access_tokens SET revoked_at = %s WHERE id = %s"
                " AND principal = %s AND revoked_at IS NULL AND expires_at > %s RETURNING id",
                (at, token_id, principal, at),
            ).fetchone()
        return row is not None

    def delete_expired(self, *, now: datetime | None = None) -> int:
        cutoff = _now(now) - KEEP
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM personal_access_tokens WHERE expires_at < %(cutoff)s"
                " OR revoked_at < %(cutoff)s",
                {"cutoff": cutoff},
            )
        return cur.rowcount
