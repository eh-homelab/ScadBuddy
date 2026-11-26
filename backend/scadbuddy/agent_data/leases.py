"""The agent's leases (#5299 D3; migration ``20261115T1020Z_agent_leases``): a lock the
agent holds across work that is not the database's, as a row of ``agent_leases`` with an
expiry, the way ``bambuddy/copy_leases.py`` holds the print worker's copy lock.

The routes that take, renew and release a lease (``…/agent/leases/{key}``) are Go's
(``tools/go/core/internal/access``, #7667). Here is the one check a Python route still
makes: a sign-in's token refresh (``api/crud_agent_plugins.py``) is stored only while its
caller holds the lease ``plugin-oauth:<plugin>``.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection


def held_in(conn: Connection[Any], key: str, token: str) -> bool:
    """Whether ``token`` holds ``key``, in the caller's transaction, locking the row so no
    acquire takes it over before that transaction ends."""
    row = conn.execute(
        "SELECT 1 FROM agent_leases WHERE key = %s AND token = %s AND expires_at > now() FOR SHARE",
        (key, token),
    ).fetchone()
    return row is not None
