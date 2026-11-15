"""The KEKs the agent's secrets are opened with, and their rotation at the API's start
(#5299 D5, steps 4 and 5).

The API holds the KEK (``SCADBUDDY_SECRET_KEY_FILE``) and, during a rotation, the one
before it (``SCADBUDDY_SECRET_KEY_PREVIOUS_FILE``). At start, after the migrations, the
data keys of the Claude credentials, of the payload subjects, of the Web Push keys and of
the notification channels' tokens sealed under the previous key are sealed again under the
current one: what the agent did itself before (``agent/src/main.ts`` ``afterMigrate``). The
agent still re-wraps its workspace file keys, plugin secrets and plugin sign-ins, until the
steps of #5299 that move those tables (5 and 7).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from psycopg import Connection

from scadbuddy.agent_data import credentials, notify, payload_keys
from scadbuddy.core.secrets import Kek, SecretKeyError
from scadbuddy.library.settings_store import settings_kek

logger = logging.getLogger(__name__)


#: Each key read, by path.
_KEYS: dict[Path, Kek] = {}
#: How long a failed read stands before the file is read again: a file that could not be
#: read for a moment (a Secret volume's swap) is read again soon, and one that stays
#: unreadable costs a read every few seconds, not one a request.
RETRY_AFTER_S = 5.0
#: How often a file that stays unreadable is logged.
WARN_EVERY_S = 300.0
#: When each path last failed to read, and when it was last logged (monotonic seconds).
_FAILED_AT: dict[Path, float] = {}
_WARNED_AT: dict[Path, float] = {}
_clock = time.monotonic


def kek_at(path: Path | None) -> Kek | None:
    """The key in ``path``, kept by path once read: the routes ask at every request, and no
    request reads a key file again once its key is kept; the API's start (``main``
    ``_rotate_agent_secrets``) makes the first read. None without one, or when it cannot be
    read (the settings store's own start refuses a bad current key first). A failed read is
    kept only ``RETRY_AFTER_S``, then the file is read again, and is logged at most once in
    ``WARN_EVERY_S``."""
    if path is None:
        return None
    if path in _KEYS:
        return _KEYS[path]
    now = _clock()
    failed = _FAILED_AT.get(path)
    if failed is not None and now - failed < RETRY_AFTER_S:
        return None
    try:
        key = settings_kek(path)
    except SecretKeyError as err:
        _FAILED_AT[path] = now
        warned = _WARNED_AT.get(path)
        if warned is None or now - warned >= WARN_EVERY_S:
            _WARNED_AT[path] = now
            logger.warning("a secret key file cannot be read (%s); trying again", err)
        return None
    _FAILED_AT.pop(path, None)
    _WARNED_AT.pop(path, None)
    if key is not None:
        _KEYS[path] = key
    return key


def _tables(conn: Connection[Any]) -> tuple[bool, bool, bool]:
    row = conn.execute(
        "SELECT to_regclass('ai_credentials') IS NOT NULL AS credentials,"
        " to_regclass('ai_payload_keys') IS NOT NULL AS payload_keys,"
        " to_regclass('ai_notify_channels') IS NOT NULL"
        " AND to_regclass('ai_push_subscriptions') IS NOT NULL AS notify"
    ).fetchone()
    if row is None:
        return False, False, False
    values = list(row.values()) if isinstance(row, dict) else list(row)
    return bool(values[0]), bool(values[1]), bool(values[2])


def rotate(conn: Connection[Any], previous: Kek, current: Kek) -> dict[str, tuple[int, int]]:
    """Re-wraps what is sealed under ``previous``; (re-wrapped, failed) per table, for the
    tables that exist (the agent's migrations make them)."""
    out: dict[str, tuple[int, int]] = {}
    if previous.id == current.id:
        return out
    has_credentials, has_keys, has_notify = _tables(conn)
    if has_credentials:
        with conn.transaction():
            out["credentials"] = credentials.rewrap(conn, previous, current)
    if has_keys:
        with conn.transaction():
            out["payload_keys"] = payload_keys.rewrap(conn, previous, current)
    if has_notify:
        with conn.transaction():
            out["notify"] = notify.rewrap(conn, previous, current)
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
