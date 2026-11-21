"""The agent's tool access (``ai_access``) and tool listings (``ai_tool_listings``) as the
CRUD service writes them (#5299 step 5): the statements of ``agent/src/access/store.ts``
(`write_tools`, `write_enabled`, `forget_source`) and the reads of ``access/resolve.ts``
(`plugin_overrides`, `source_enabled`) that the plugin, package and access routes run in
their transactions, and the warm process cache's eviction (``sessions/warmEvict.ts``),
sent in the writer's transaction so it goes out only on commit."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from psycopg import Connection

#: ``sessions/warmEvict.ts`` ``WARM_EVICT_CHANNEL``.
WARM_EVICT_CHANNEL = "scadbuddy_warm_evict"
#: ``access/model.ts`` ``SOURCE_ITEM``: the row that switches a whole source.
SOURCE_ITEM = "*"
TOOL_ACCESS = ("off", "read", "write", "outward")
#: ``access/resolve.ts`` ``PACKAGE_SERVER_RE``.
_PACKAGE_SERVER = re.compile(r"^package:.+/mcp:.+$", re.DOTALL)


def remote_source(name: str) -> str:
    return f"remote:{name}"


def package_source(pkg: str) -> str:
    return f"package:{pkg}"


def package_server_source(pkg: str, server: str) -> str:
    return f"package:{pkg}/mcp:{server}"


def notify_warm_evict(conn: Connection[Any], target: str = "*") -> None:
    """Tells every replica to evict ``target``'s warm process (``*``: every one)."""
    conn.execute("SELECT pg_notify(%s, %s)", (WARM_EVICT_CHANNEL, target))


def write_tools(conn: Connection[Any], source: str, tools: Mapping[str, str], by: str) -> None:
    """Replaces every tool row of ``source`` (not ``*``) with ``tools``."""
    conn.execute("DELETE FROM ai_access WHERE source = %s AND item <> %s", (source, SOURCE_ITEM))
    for item, access in tools.items():
        conn.execute(
            "INSERT INTO ai_access (source, item, access, updated_by) VALUES (%s, %s, %s, %s)",
            (source, item, access, by),
        )


def write_enabled(conn: Connection[Any], source: str, enabled: bool, by: str) -> None:
    conn.execute(
        "INSERT INTO ai_access (source, item, access, updated_by) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (source, item) DO UPDATE SET access = EXCLUDED.access,"
        " updated_by = EXCLUDED.updated_by, updated_at = now()",
        (source, SOURCE_ITEM, "on" if enabled else "off", by),
    )


def forget_source(conn: Connection[Any], source: str) -> None:
    """Deletes everything stored for ``source``, its tool listing included."""
    conn.execute("DELETE FROM ai_access WHERE source = %s", (source,))
    conn.execute("DELETE FROM ai_tool_listings WHERE source = %s", (source,))


def _value(row: Any, key: str, index: int) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


def access_rows(conn: Connection[Any], sources: Iterable[str]) -> list[dict[str, str]]:
    """The rows of ``sources``, ordered by source and item."""
    wanted = list(sources)
    if not wanted:
        return []
    rows = conn.execute(
        "SELECT source, item, access, updated_by FROM ai_access WHERE source = ANY(%s)"
        " ORDER BY source, item",
        (wanted,),
    ).fetchall()
    return [
        {
            "source": _value(r, "source", 0),
            "item": _value(r, "item", 1),
            "access": _value(r, "access", 2),
            "updated_by": _value(r, "updated_by", 3),
        }
        for r in rows
    ]


def plugin_overrides(
    source: str, rows: Iterable[Mapping[str, str]]
) -> tuple[dict[str, str], list[str]]:
    """``access/resolve.ts`` ``pluginOverrides``: (tool tiers, disabled tools, sorted)."""
    tiers: dict[str, str] = {}
    disabled: list[str] = []
    for row in rows:
        if row["source"] != source or row["item"] == SOURCE_ITEM:
            continue
        if row["access"] not in TOOL_ACCESS:
            continue
        if row["access"] == "off":
            disabled.append(row["item"])
        else:
            tiers[row["item"]] = row["access"]
    return tiers, sorted(disabled, key=_js_order)


def _js_order(text: str) -> list[int]:
    """The key JavaScript's default sort orders strings by: UTF-16 code units."""
    data = text.encode("utf-16-be", "surrogatepass")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def enabled_by_default(source: str) -> bool:
    return source.startswith("builtin:") or _PACKAGE_SERVER.match(source) is not None


def source_enabled(source: str, rows: Iterable[Mapping[str, str]]) -> bool:
    """``access/resolve.ts`` ``sourceEnabled``: the source's ``*`` row, else its default."""
    for row in rows:
        if row["source"] == source and row["item"] == SOURCE_ITEM:
            if row["access"] == "on":
                return True
            if row["access"] == "off":
                return False
    return enabled_by_default(source)


def js_sort(items: Iterable[str]) -> list[str]:
    """``items`` sorted as JavaScript's ``Array.prototype.sort`` sorts strings."""
    return sorted(items, key=_js_order)
