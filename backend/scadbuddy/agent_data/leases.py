"""The agent's leases (#5299 D3; migration ``20261115T1020Z_agent_leases``): a lock the
agent holds across work that is not the database's, as a row of ``agent_leases`` with an
expiry, the way ``bambuddy/copy_leases.py`` holds the print worker's copy lock.

Two kinds of key:

- ``tool-listing:<source>``: a plugin's connection test (``agent/src/access/listings.ts``
  ``exclusive``, ``exclusiveServer``). It replaces the session advisory lock on
  ``hashtextextended('scadbuddy:ai_tool_listings:test:<name>', 0)``, where ``<name>`` is a
  remote plugin's name or a package server's source. An agent pod of an older build, or
  one that does not use the routes yet, still takes that lock over SQL, so an acquire
  also tries it as a transaction lock: while such a pod tests the plugin, the lease is
  ``busy``. The other way round is not covered: an SQL pod does not read this table, so
  it can start a test while a lease holds. Only during a rolling deploy, and the worst
  case is two tests of one plugin at once, each listing written under the plugin's row
  lock, the last one kept.
- ``plugin-oauth:<plugin>``: a sign-in's token refresh (``plugins/oauthStore.ts``), which
  over SQL holds the grant row's ``FOR UPDATE`` across the token endpoint's answer so two
  replicas never spend one refresh token. An acquire also takes that row lock in its
  transaction (``FOR UPDATE``), so it waits while an SQL pod refreshes, for at most the
  caller's ``wait_s`` (a ``lock_timeout``), then answers ``busy``. An SQL pod that
  starts a refresh while a lease holds is not refused: during a rolling deploy two
  replicas could spend one refresh token, and the loser marks the grant for a new
  sign-in.
- ``attention-rate``: the attention requests' rate limit (``agent/src/gate/questions.ts``
  ``attentionLimit``, #5299 step 10), held across the count and the workflow Update that opens
  the entry, as the SQL path holds the transaction lock on
  ``hashtextextended('scadbuddy:attention-rate', 0)``. An acquire also tries that lock as a
  transaction lock: while an SQL pod counts, the lease is ``busy``.

A holder names its lease by a token of its own. An acquire by the holder answers it held;
a renew or release sent again renews or deletes what the first did. A lease past its
expiry is taken over by the next acquire.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.errors import LockNotAvailable

#: How long a lease holds without a renew, and the longest one, in seconds.
LEASE_TTL = 120.0
MAX_TTL = 300.0
#: The keys the agent may take: a listing's source, a remote plugin's name.
KEY_PATTERN = (
    r"^(tool-listing:(remote:[a-z][a-z0-9-]{0,30}[a-z0-9]|package:[^\x00-\x1f]{1,400})"
    r"|plugin-oauth:[a-z][a-z0-9-]{0,30}[a-z0-9]|attention-rate)$"
)
_KEY = re.compile(KEY_PATTERN)
#: The prefix of the advisory lock key the SQL path takes (``access/listings.ts``).
ADVISORY_PREFIX = "scadbuddy:ai_tool_listings:test:"
#: The SQL path's attention rate lock (``agent/src/gate/questions.ts`` ``ATTENTION_RATE_LOCK``).
ATTENTION_RATE_LOCK = "scadbuddy:attention-rate"


def valid_key(key: str) -> bool:
    return _KEY.match(key) is not None


def advisory_name(key: str) -> str | None:
    """The name inside the SQL path's advisory lock key for ``key``: a remote plugin's
    name, a package server's source; None for a key with no such lock."""
    if not key.startswith("tool-listing:"):
        return None
    source = key[len("tool-listing:") :]
    return source[len("remote:") :] if source.startswith("remote:") else source


def acquire(
    conn: Connection[Any], key: str, token: str, ttl: float, wait: float = 0.0
) -> datetime | None:
    """The new expiry, or None when another holder keeps ``key``. Runs in the caller's
    transaction. ``wait`` (seconds) bounds the wait for an SQL pod's grant row lock; past
    it the lease is busy."""
    name = advisory_name(key)
    lock = ATTENTION_RATE_LOCK if key == "attention-rate" else None
    if name is not None:
        lock = ADVISORY_PREFIX + name
    if lock is not None:
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS held", (lock,)
        ).fetchone()
        if row is None or not _first(row):
            return None
    if key.startswith("plugin-oauth:"):
        # Waits while an SQL pod holds the grant's row across a refresh, but no longer than
        # the caller asked: a lock_timeout of 0 would wait for ever, so at least 1 ms.
        # The setting in force before is put back after, so the rest of the transaction
        # keeps whatever the role or server set: by the restore below once the row is
        # locked, and by the savepoint's rollback when the wait ends in LockNotAvailable.
        before = conn.execute("SELECT current_setting('lock_timeout') AS t").fetchone()
        try:
            with conn.transaction():
                conn.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    (f"{max(1, round(wait * 1000))}ms",),
                )
                conn.execute(
                    "SELECT 1 FROM ai_plugin_oauth WHERE plugin = %s FOR UPDATE",
                    (key[len("plugin-oauth:") :],),
                )
                conn.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    (_first(before) if before is not None else "0",),
                )
        except LockNotAvailable:
            return None
    row = conn.execute(
        "INSERT INTO agent_leases (key, token, expires_at)"
        " VALUES (%s, %s, now() + make_interval(secs => %s))"
        " ON CONFLICT (key) DO UPDATE"
        " SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at"
        " WHERE agent_leases.expires_at <= now() OR agent_leases.token = EXCLUDED.token"
        " RETURNING expires_at",
        (key, token, ttl),
    ).fetchone()
    return None if row is None else _first(row)


def renew(conn: Connection[Any], key: str, token: str, ttl: float) -> datetime | None:
    row = conn.execute(
        "UPDATE agent_leases SET expires_at = now() + make_interval(secs => %s)"
        " WHERE key = %s AND token = %s AND expires_at > now() RETURNING expires_at",
        (ttl, key, token),
    ).fetchone()
    return None if row is None else _first(row)


def release(conn: Connection[Any], key: str, token: str) -> None:
    conn.execute("DELETE FROM agent_leases WHERE key = %s AND token = %s", (key, token))


def held_in(conn: Connection[Any], key: str, token: str) -> bool:
    """Whether ``token`` holds ``key``, in the caller's transaction, locking the row so no
    acquire takes it over before that transaction ends."""
    row = conn.execute(
        "SELECT 1 FROM agent_leases WHERE key = %s AND token = %s AND expires_at > now() FOR SHARE",
        (key, token),
    ).fetchone()
    return row is not None


def _first(row: Any) -> Any:
    return next(iter(row.values())) if isinstance(row, dict) else row[0]
