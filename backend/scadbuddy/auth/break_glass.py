"""Break-glass sign-in (#4975 phase 3, spec 2026-11-01 §9.3).

``kubectl exec deploy/scadbuddy -- python -m scadbuddy.auth.break_glass`` prints a
sign-in URL with a one-time code (ten minutes, single use, kept as its SHA-256). Opening
it gives a ``break_glass`` session for an hour. Exec access to the pod is the proof:
whoever has it already has the database. It works with the IdP down or the login
misconfigured.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import psycopg

from scadbuddy.auth.store import Pool
from scadbuddy.auth.tokens import new_token, token_hash

CODE_LIFETIME = timedelta(minutes=10)
SESSION_LIFETIME = timedelta(hours=1)
#: The session's issuer and subject: no IdP vouched for it.
ISSUER = "break-glass"
SUBJECT = "break-glass"
PATH = "/api/v1/auth/break-glass"


def mint(conn: psycopg.Connection[Any], now: datetime | None = None) -> str:
    code = new_token()
    at = now or datetime.now(UTC)
    conn.execute(
        "INSERT INTO auth_break_glass_codes (code_hash, created_at, expires_at)"
        " VALUES (%s, %s, %s)",
        (token_hash(code), at, at + CODE_LIFETIME),
    )
    return code


def redeem(pool: Pool, code: str, now: datetime | None = None) -> bool:
    """Whether ``code`` was live; it is spent either way it matched."""
    code_hash = token_hash(code)
    if code_hash is None:
        return False
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE auth_break_glass_codes SET used_at = %(now)s WHERE code_hash = %(hash)s"
            " AND used_at IS NULL AND expires_at > %(now)s RETURNING code_hash",
            {"hash": code_hash, "now": now or datetime.now(UTC)},
        ).fetchone()
    return row is not None


def url_for(public_url: str | None, code: str) -> str:
    if public_url:
        parts = urlsplit(public_url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}{PATH}?code={code}"
    return f"{PATH}?code={code}"


def main() -> int:
    from scadbuddy.core.settings import Settings
    from scadbuddy.library.settings_store import SettingsStore

    settings = Settings()
    public_url = SettingsStore(settings).load().public_url
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        code = mint(conn)
    url = url_for(public_url, code)
    print(f"Open within {int(CODE_LIFETIME.total_seconds() // 60)} minutes, once:\n{url}")
    if not public_url:
        print("No public URL is set: open the path above on ScadBuddy's own origin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
