"""The agent's plugin packages (``ai_plugin_packages``): the statement a hosted plugin's
delete runs in its transaction (``hosted_plugins/store.py``). The ``plugin-packages`` routes
are Go's (``tools/go/core/internal/packages``, #7667).

`forget_package` deletes every ``ai_access`` and ``ai_tool_listings`` row of a package and
its servers. It matches a server's source with ``starts_with``, never ``LIKE``: a package's
name may hold ``_``."""

from __future__ import annotations

from typing import Any

from psycopg import Connection

from scadbuddy.agent_data.access import package_source


def server_prefix(name: str) -> str:
    """Where ``package:<name>``'s servers' sources start."""
    return f"{package_source(name)}/mcp:"


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
