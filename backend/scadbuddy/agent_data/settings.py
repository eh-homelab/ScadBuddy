"""The agent's ``ai_settings`` (#5299 step 4): non-secret values, one JSON value per key
(``agent/src/credentials.ts`` `SettingsStore`).

`read` is the read the workspace's limits make (``workspace.py``). The settings routes, a
write's compare-and-set and its NOTIFYs among them, are Go's (``tools/go/core/internal/secrets``,
#7667).
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection


def read(conn: Connection[Any], keys: list[str]) -> dict[str, Any]:
    """The stored values of ``keys``; a key with no row is left out."""
    rows = conn.execute(
        "SELECT key, value FROM ai_settings WHERE key = ANY(%s)", (keys,)
    ).fetchall()
    return {_field(row, 0, "key"): _field(row, 1, "value") for row in rows}


def _field(row: Any, index: int, name: str) -> Any:
    return row[name] if isinstance(row, dict) else row[index]
