"""The KEKs the agent's secrets are opened with, and their rotation at the API's start
(#5299 D5, step 4).

The API holds the KEK (``SCADBUDDY_SECRET_KEY_FILE``) and, during a rotation, the one
before it (``SCADBUDDY_SECRET_KEY_PREVIOUS_FILE``). At start, after the migrations, the
data keys of the Claude credentials and of the payload subjects sealed under the previous
key are sealed again under the current one: what the agent did itself before
(``agent/src/main.ts`` ``afterMigrate``). The agent still re-wraps its workspace file keys,
plugin secrets, Web Push keys, notification channel tokens and plugin sign-ins, until the
steps of #5299 that move those tables (5 and 7).
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any

from psycopg import Connection

from scadbuddy.agent_data import credentials, payload_keys
from scadbuddy.core.secrets import Kek, SecretKeyError
from scadbuddy.library.settings_store import settings_kek

logger = logging.getLogger(__name__)


@functools.cache
def kek_at(path: Path | None) -> Kek | None:
    """The key in ``path``, read once per process and kept (by path): the routes ask at
    every request, so no request reads a file, and the API's start (``main``
    ``_rotate_agent_secrets``) makes the first read. None without one, or when it cannot be
    read (the settings store's own start refuses a bad current key first)."""
    try:
        return settings_kek(path)
    except SecretKeyError:
        return None


def _tables(conn: Connection[Any]) -> tuple[bool, bool]:
    row = conn.execute(
        "SELECT to_regclass('ai_credentials') IS NOT NULL AS credentials,"
        " to_regclass('ai_payload_keys') IS NOT NULL AS payload_keys"
    ).fetchone()
    if row is None:
        return False, False
    values = list(row.values()) if isinstance(row, dict) else list(row)
    return bool(values[0]), bool(values[1])


def rotate(conn: Connection[Any], previous: Kek, current: Kek) -> dict[str, tuple[int, int]]:
    """Re-wraps what is sealed under ``previous``; (re-wrapped, failed) per table, for the
    tables that exist (the agent's migrations make them)."""
    out: dict[str, tuple[int, int]] = {}
    if previous.id == current.id:
        return out
    has_credentials, has_keys = _tables(conn)
    if has_credentials:
        with conn.transaction():
            out["credentials"] = credentials.rewrap(conn, previous, current)
    if has_keys:
        with conn.transaction():
            out["payload_keys"] = payload_keys.rewrap(conn, previous, current)
    for table, (rewrapped, failed) in out.items():
        if rewrapped or failed:
            logger.info(
                "secret key rotation: re-wrapped the agent's %s",
                table,
                extra={
                    "rewrapped": rewrapped,
                    "failed": failed,
                    "from": previous.id,
                    "to": current.id,
                },
            )
    return out
