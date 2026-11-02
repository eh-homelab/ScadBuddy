"""Delegated sessions (#4975 phase 6, spec 2026-11-01 §7.4): a browser the agent drives
signs in as the principal of the agent session it serves.

The agent's credential calls ``POST /api/v1/auth/delegated-sessions`` with that principal
and a lifetime of at most an hour; the answer is a one-time sign-in URL (a minute to use,
kept as its SHA-256) on ScadBuddy's public origin. The browser opens it, and
``GET /api/v1/auth/delegated?code=`` spends the code and sets a ``delegated`` session's
cookie. As built, the browser is a Playwright MCP plugin (the built-in headless browser of
#349 is gone), which loads the public URL over https, so the cookie is ``__Host-``
everywhere. A delegated session is refused on the ``person`` routes and on what only a
person does (`core/authorship.py` ``by_a_person``), so the browser can never approve what
its own session asked for (AI spec §8.6). A code is a bearer secret: never log one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from scadbuddy.auth.store import Pool
from scadbuddy.auth.tokens import new_token, token_hash

CODE_LIFETIME = timedelta(minutes=1)
MAX_TTL = timedelta(hours=1)
MIN_TTL = timedelta(minutes=1)
#: The session's issuer: no IdP vouched for it; the agent's credential did.
ISSUER = "delegated"
PATH = "/api/v1/auth/delegated"
#: How long a spent or expired code stays before the sessions sweep forgets it.
KEEP = timedelta(days=1)


@dataclass(frozen=True)
class Delegation:
    principal: str
    ttl: timedelta


def mint(pool: Pool, principal: str, ttl: timedelta, now: datetime | None = None) -> str:
    code = new_token()
    at = now or datetime.now(UTC)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO auth_delegation_codes (code_hash, principal, ttl_seconds, created_at,"
            " expires_at) VALUES (%s, %s, %s, %s, %s)",
            (token_hash(code), principal, int(ttl.total_seconds()), at, at + CODE_LIFETIME),
        )
    return code


def redeem(pool: Pool, code: str, now: datetime | None = None) -> Delegation | None:
    """What ``code`` delegates, when it was live; it is spent either way it matched."""
    code_hash = token_hash(code)
    if code_hash is None:
        return None
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE auth_delegation_codes SET used_at = %(now)s WHERE code_hash = %(hash)s"
            " AND used_at IS NULL AND expires_at > %(now)s RETURNING principal, ttl_seconds",
            {"hash": code_hash, "now": now or datetime.now(UTC)},
        ).fetchone()
    if row is None:
        return None
    return Delegation(row["principal"], timedelta(seconds=row["ttl_seconds"]))


def delete_spent(pool: Pool, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(UTC)) - KEEP
    with pool.connection() as conn:
        cur = conn.execute("DELETE FROM auth_delegation_codes WHERE expires_at < %s", (cutoff,))
    return cur.rowcount
