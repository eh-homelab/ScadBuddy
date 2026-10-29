"""The tool-access model (#2171, spec 2026-10-18 §4.1 to §4.3), as agent/src/access/model.ts and
resolve.ts have it: a source (where tools come from) and an item (a tool, or ``*`` for the
whole source) have an access, kept in ``ai_access``. The rules are pure; the agent's
test/fixtures/access-vectors.json pins them to the TypeScript (tests/test_access_vectors.py).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg

TOOL_ACCESS = ("off", "read", "write", "outward")
# agent/src/harness/permissions.ts RISK_TIERS, least to most.
_TIERS = ("read", "write", "outward")
# The item that stands for a whole source: its access is `on` or `off`.
SOURCE_ITEM = "*"
# `updated_by` of the rows migration 20261026T2201Z_access_copy.sql wrote.
MIGRATED_BY = "migration:2171"
BROWSER_SOURCE = "builtin:browser"
REGISTRY_SOURCES = ("builtin:scadbuddy", BROWSER_SOURCE)
OWN_PLUGIN_SOURCE = "builtin:own-plugin"
# A package's MCP server, ``package:<pkg>/mcp:<server>`` (agent/src/access/model.ts).
_PACKAGE_SERVER_RE = re.compile(r"package:.+/mcp:.+", re.DOTALL)


@dataclass(frozen=True)
class AccessRow:
    source: str
    item: str
    access: str
    updated_by: str


def registry_source(name: str) -> str:
    """The source of a registry tool: the browser's tools are their own."""
    return BROWSER_SOURCE if name.startswith("browser_") else "builtin:scadbuddy"


def _rank(tier: str) -> int:
    # A declared tier that is no tier ranks below every tier, as indexOf's -1 does (#3882).
    return _TIERS.index(tier) if tier in _TIERS else -1


def tool_access(declared: str, row: AccessRow | None, *, migrated_raises_only: bool) -> str:
    """A tool's effective access: the row's when it holds one, else the declared tier. With
    ``migrated_raises_only`` (a registry source), a row the migration copied counts only when
    it is ``off`` or above ``declared``: the old registry overrides could only raise."""
    if row is None or row.access not in TOOL_ACCESS:
        return declared
    if migrated_raises_only and row.updated_by == MIGRATED_BY and row.access != "off":
        return row.access if _rank(row.access) > _rank(declared) else declared
    return row.access


def enabled_by_default(source: str) -> bool:
    """Built-in sources and package servers are on until turned off (a server is offered while
    its package is approved and enabled); remote plugins and packages off until turned on."""
    return source.startswith("builtin:") or _PACKAGE_SERVER_RE.fullmatch(source) is not None


def source_enabled(source: str, rows: Sequence[AccessRow]) -> bool:
    """The source's ``*`` row when it is ``on`` or ``off``, else its default
    (``enabled_by_default``)."""
    for row in rows:
        if row.source == source and row.item == SOURCE_ITEM:
            if row.access == "on":
                return True
            if row.access == "off":
                return False
            break
    return enabled_by_default(source)


async def read_rows(conn: psycopg.AsyncConnection[Any], sources: Sequence[str]) -> list[AccessRow]:
    cur = await conn.execute(
        "SELECT source, item, access, updated_by FROM ai_access WHERE source = ANY(%s)",
        (list(sources),),
    )
    return [AccessRow(str(s), str(i), str(a), str(u)) for s, i, a, u in await cur.fetchall()]
