"""The agent's ``ai_settings`` (#5299 step 4): non-secret values, one JSON value per key
(``agent/src/credentials.ts`` `SettingsStore`).

A write is one transaction. With ``expect`` it is a compare-and-set, as the agent's
``setMany`` with a check was: the table is locked against other writers (readers are not
blocked), each expected key is compared with what is stored (jsonb equality; an absent
key matches only ``present: false``), and nothing is written unless all match. The agent
runs its check over values it read first, and sends what it read as ``expect``.

The NOTIFYs the agent's caches follow go out in the same transaction, so on commit: the
warm process cache's eviction (``scadbuddy_warm_evict``, payload ``*``,
``agent/src/sessions/warmEvict.ts``) and the runtime settings' re-read
(``scadbuddy_settings``, payload ``session-runtime``, ``sessions/runtimeSettings.ts``).
The agent says which a write needs: which keys a warm process was spawned by is its own
knowledge. The agent hears both over its change stream (`agent_changes/hub.py`, #5299 step 11).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

WARM_EVICT_CHANNEL = "scadbuddy_warm_evict"
SETTINGS_CHANNEL = "scadbuddy_settings"
RUNTIME_PAYLOAD = "session-runtime"


@dataclass(frozen=True)
class Expected:
    """A key as the agent read it: ``present`` false for no row."""

    key: str
    present: bool
    value: Any = None


def read(conn: Connection[Any], keys: list[str]) -> dict[str, Any]:
    """The stored values of ``keys``; a key with no row is left out."""
    rows = conn.execute(
        "SELECT key, value FROM ai_settings WHERE key = ANY(%s)", (keys,)
    ).fetchall()
    return {_field(row, 0, "key"): _field(row, 1, "value") for row in rows}


def _field(row: Any, index: int, name: str) -> Any:
    return row[name] if isinstance(row, dict) else row[index]


def _matches(conn: Connection[Any], expected: Expected) -> bool:
    row = conn.execute(
        "SELECT value = %s AS same FROM ai_settings WHERE key = %s",
        (Jsonb(expected.value), expected.key),
    ).fetchone()
    if row is None:
        return not expected.present
    return expected.present and bool(_field(row, 0, "same"))


def write(
    conn: Connection[Any],
    values: dict[str, Any],
    *,
    expect: list[Expected] | None,
    warm_evict: bool,
    runtime: bool,
) -> bool:
    """Writes ``values`` in one transaction; whether it wrote (``expect`` held)."""
    with conn.transaction():
        if expect is not None:
            conn.execute("LOCK TABLE ai_settings IN SHARE ROW EXCLUSIVE MODE")
            if not all(_matches(conn, e) for e in expect):
                return False
        for key, value in values.items():
            conn.execute(
                "INSERT INTO ai_settings (key, value) VALUES (%s, %s)"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (key, Jsonb(value)),
            )
        if warm_evict:
            conn.execute("SELECT pg_notify(%s, '*')", (WARM_EVICT_CHANNEL,))
        if runtime:
            conn.execute("SELECT pg_notify(%s, %s)", (SETTINGS_CHANNEL, RUNTIME_PAYLOAD))
    return True
