"""The agent's plugin packages (``ai_plugin_packages``) as the CRUD service writes them
(#5299 step 5, area ``plugin-packages``, `api/crud_agent_packages.py`): the statements of
``agent/src/plugins/packages/store.ts`` that run inside a package write's transaction.

`forget_package` deletes every ``ai_access`` and ``ai_tool_listings`` row of a package and
its servers, and `prune_stale_rows` deletes the rows of the items and servers the current
review no longer has (#4757). Both match a server's source with ``starts_with``, never
``LIKE``: a package's name may hold ``_``. The item names are ``access/items.ts``'s."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import Connection

from scadbuddy.agent_data.access import package_source

#: ``access/items.ts`` ``PREFIXES``: an item's kind by the prefix of its name.
_PREFIXES = (
    ("skill:", "skill"),
    ("subagent:", "subagent"),
    ("builtin:", "builtin"),
    ("hook:", "hook"),
)
_OWN_PLUGIN_SOURCE = "builtin:own-plugin"


def server_prefix(name: str) -> str:
    """Where ``package:<name>``'s servers' sources start."""
    return f"{package_source(name)}/mcp:"


def item_kind_of(item: str) -> str:
    for prefix, kind in _PREFIXES:
        if item.startswith(prefix):
            return kind
    return "tool"


def holds_items(source: str) -> bool:
    """``access/items.ts`` ``holdsItems``: a package's own source and the own plugin."""
    return source == _OWN_PLUGIN_SOURCE or (source.startswith("package:") and "/mcp:" not in source)


def item_kind_in(source: str, item: str) -> str:
    return item_kind_of(item) if holds_items(source) else "tool"


def forget_package(conn: Connection[Any], name: str) -> None:
    """Every ``ai_access`` and ``ai_tool_listings`` row of ``package:<name>`` and its
    servers (``store.ts`` ``forgetPackage``)."""
    source = package_source(name)
    prefix = server_prefix(name)
    conn.execute(
        "DELETE FROM ai_access WHERE source = %s OR starts_with(source, %s)", (source, prefix)
    )
    conn.execute(
        "DELETE FROM ai_tool_listings WHERE source = %s OR starts_with(source, %s)",
        (source, prefix),
    )


def _declared_servers(review: Mapping[str, Any]) -> set[str] | None:
    local = review.get("local_servers")
    if local is None:
        return None
    return {s["name"] for s in review.get("mcp_servers", [])} | set(local)


def _names_items(review: Mapping[str, Any]) -> bool:
    return "described" in review and all("id" in h for h in review.get("hooks", []))


def _first(row: Any) -> Any:
    return next(iter(row.values())) if isinstance(row, Mapping) else row[0]


def prune_stale_rows(conn: Connection[Any], name: str, review: Mapping[str, Any]) -> bool:
    """``store.ts`` ``pruneStaleRows``: deletes what ``package:<name>`` stores for items
    ``review`` (the pin current after the write) no longer has: a skill or command, a
    subagent, a built-in and a hook (by its id), and the rows and listings of the servers it
    no longer declares. What the review cannot tell is kept: no item row for an older vet's
    review, no built-in's without ``builtin_tools``, no server's without ``local_servers``.
    Returns whether it deleted anything."""
    deleted = False
    if _names_items(review):
        source = package_source(name)
        prefix = f"{name}:"

        def bare(qualified: str) -> str:
            return qualified[len(prefix) :] if qualified.startswith(prefix) else qualified

        builtins = review.get("builtin_tools")
        kept = {
            *(f"skill:{bare(s)}" for s in [*review.get("skills", []), *review.get("commands", [])]),
            *(f"subagent:{bare(a)}" for a in review.get("agents", [])),
            *(f"builtin:{b}" for b in builtins or []),
        }
        hook_ids = {h.get("id") for h in review.get("hooks", [])}

        def stale(item: str) -> bool:
            kind = item_kind_in(source, item)
            if kind == "tool":
                return False
            if kind == "hook":
                return item[item.rfind(":") + 1 :] not in hook_ids
            if kind == "builtin" and "builtin_tools" not in review:
                return False
            return item not in kept

        rows = conn.execute("SELECT item FROM ai_access WHERE source = %s", (source,)).fetchall()
        items = [i for i in (_first(r) for r in rows) if stale(i)]
        if items:
            conn.execute(
                "DELETE FROM ai_access WHERE source = %s AND item = ANY(%s)", (source, items)
            )
            deleted = True

    declared = _declared_servers(review)
    if declared is None:
        return deleted
    prefix = server_prefix(name)
    rows = conn.execute(
        "SELECT source FROM ai_access WHERE starts_with(source, %s)"
        " UNION SELECT source FROM ai_tool_listings WHERE starts_with(source, %s)",
        (prefix, prefix),
    ).fetchall()
    gone = [s for s in (_first(r) for r in rows) if s[len(prefix) :] not in declared]
    if gone:
        conn.execute("DELETE FROM ai_access WHERE source = ANY(%s)", (gone,))
        conn.execute("DELETE FROM ai_tool_listings WHERE source = ANY(%s)", (gone,))
        deleted = True
    return deleted
