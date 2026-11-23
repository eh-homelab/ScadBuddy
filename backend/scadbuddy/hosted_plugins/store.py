"""The SQL of hosted plugins (spec 2026-11-21 §6.1) over the ``ai_hosted_plugin*`` tables,
each function a part of its caller's transaction. A plugin's bytes are blobs
``attachment-<sha256>`` (`orders/attachments.py` `keep_file`), each draft file's and each
version zip's held in ``blob_refs`` by holder ``hosted_plugin`` and the plugin's id, which
`sync_holds` keeps equal to what the rows name."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from scadbuddy.agent_data.access import notify_warm_evict
from scadbuddy.agent_data.packages import forget_package
from scadbuddy.orders.attachments import blob_key

HOLDER = "hosted_plugin"

Conn = psycopg.Connection[Any]


class PackageNameTakenError(Exception):
    pass


class PluginExistsError(Exception):
    pass


def plugin(conn: Conn, name: str, *, lock: bool = False) -> Any | None:
    lock_sql = " FOR UPDATE" if lock else ""
    return conn.execute(
        f"SELECT * FROM ai_hosted_plugins WHERE name = %s{lock_sql}", (name,)
    ).fetchone()


def plugins(conn: Conn) -> list[Any]:
    return conn.execute("SELECT * FROM ai_hosted_plugins ORDER BY name").fetchall()


def lock_name(conn: Conn, name: str) -> None:
    """Holds the package name ``name`` until the caller's transaction ends. A hosted plugin's
    create and a package's install both take it before they check the other table, so the
    two can never both take one name (ruling R3 and its reverse, #6934)."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('ai_plugin_package_name:' || %s, 0))",
        (name,),
    )


def create(conn: Conn, name: str, description: str, category: str | None, tags: list[str]) -> Any:
    """A new plugin with an empty draft. Spec ruling R3: never a name an installed package
    has (a built-in is refused by the caller)."""
    lock_name(conn, name)
    taken = conn.execute(
        "SELECT 1 FROM ai_plugin_packages WHERE name = %s FOR KEY SHARE", (name,)
    ).fetchone()
    if taken is not None:
        raise PackageNameTakenError(name)
    row = conn.execute(
        "INSERT INTO ai_hosted_plugins (name, description, category, tags)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING RETURNING *",
        (name, description, category, tags),
    ).fetchone()
    if row is None:
        raise PluginExistsError(name)
    return row


def update(conn: Conn, plugin_row: Any, fields: dict[str, Any]) -> Any:
    """The plugin with ``fields`` (any of description, category, tags) changed."""
    if not fields:
        return plugin_row
    sets = ", ".join(f"{column} = %({column})s" for column in fields)
    return conn.execute(
        f"UPDATE ai_hosted_plugins SET {sets}, updated_at = now() WHERE id = %(id)s RETURNING *",
        {**fields, "id": plugin_row["id"]},
    ).fetchone()


def draft(conn: Conn, plugin_id: int) -> list[Any]:
    return conn.execute(
        "SELECT path, sha256, size, executable, updated_at FROM ai_hosted_plugin_files"
        " WHERE plugin_id = %s",
        (plugin_id,),
    ).fetchall()


def draft_file(conn: Conn, plugin_id: int, path: str) -> Any | None:
    return conn.execute(
        "SELECT path, sha256, size, executable, updated_at FROM ai_hosted_plugin_files"
        " WHERE plugin_id = %s AND path = %s",
        (plugin_id, path),
    ).fetchone()


def write_file(
    conn: Conn, plugin_id: int, path: str, sha256: str, size: int, executable: bool
) -> Any:
    row = conn.execute(
        "INSERT INTO ai_hosted_plugin_files (plugin_id, path, sha256, size, executable)"
        " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (plugin_id, path) DO UPDATE SET"
        " sha256 = EXCLUDED.sha256, size = EXCLUDED.size, executable = EXCLUDED.executable,"
        " updated_at = now() RETURNING path, sha256, size, executable, updated_at",
        (plugin_id, path, sha256, size, executable),
    ).fetchone()
    _touch(conn, plugin_id)
    sync_holds(conn, plugin_id)
    return row


def delete_file(conn: Conn, plugin_id: int, path: str) -> bool:
    gone = conn.execute(
        "DELETE FROM ai_hosted_plugin_files WHERE plugin_id = %s AND path = %s",
        (plugin_id, path),
    ).rowcount
    if gone:
        _touch(conn, plugin_id)
        sync_holds(conn, plugin_id)
    return bool(gone)


def move_file(conn: Conn, plugin_id: int, source: str, target: str) -> Any:
    row = conn.execute(
        "UPDATE ai_hosted_plugin_files SET path = %s, updated_at = now()"
        " WHERE plugin_id = %s AND path = %s RETURNING path, sha256, size, executable, updated_at",
        (target, plugin_id, source),
    ).fetchone()
    _touch(conn, plugin_id)
    return row


def latest(conn: Conn, plugin_row: Any) -> Any | None:
    if plugin_row["latest_version"] is None:
        return None
    return version(conn, plugin_row["id"], plugin_row["latest_version"])


def version(conn: Conn, plugin_id: int, number: int) -> Any | None:
    return conn.execute(
        "SELECT version, zip_sha256, content_hash, files, published_at, published_by"
        " FROM ai_hosted_plugin_versions WHERE plugin_id = %s AND version = %s",
        (plugin_id, number),
    ).fetchone()


def publish(
    conn: Conn,
    plugin_id: int,
    number: int,
    zip_sha256: str,
    content_hash: str,
    files: dict[str, Any],
    by: str,
) -> Any:
    """The version row, its zip's hold and ``latest_version``, in the caller's transaction."""
    row = conn.execute(
        "INSERT INTO ai_hosted_plugin_versions"
        " (plugin_id, version, zip_sha256, content_hash, files, published_by)"
        " VALUES (%s, %s, %s, %s, %s, %s)"
        " RETURNING version, zip_sha256, content_hash, files, published_at, published_by",
        (plugin_id, number, zip_sha256, content_hash, Jsonb(files), by),
    ).fetchone()
    conn.execute(
        "UPDATE ai_hosted_plugins SET latest_version = %s, updated_at = now() WHERE id = %s",
        (number, plugin_id),
    )
    sync_holds(conn, plugin_id)
    return row


def delete(conn: Conn, plugin_row: Any) -> None:
    """The plugin, its draft and versions, their holds, and its package when one was
    installed from it (spec §6.3: deleting the plugin deletes its package)."""
    package = conn.execute(
        "DELETE FROM ai_plugin_packages WHERE name = %s AND source_kind = 'hosted'",
        (plugin_row["name"],),
    ).rowcount
    if package:
        forget_package(conn, plugin_row["name"])
        notify_warm_evict(conn)
    conn.execute("DELETE FROM ai_hosted_plugins WHERE id = %s", (plugin_row["id"],))
    conn.execute(
        "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
        (HOLDER, str(plugin_row["id"])),
    )


def _touch(conn: Conn, plugin_id: int) -> None:
    conn.execute("UPDATE ai_hosted_plugins SET updated_at = now() WHERE id = %s", (plugin_id,))


def sync_holds(conn: Conn, plugin_id: int) -> None:
    """Holds exactly the blobs the plugin's draft and versions name."""
    prefix = blob_key("")
    named = (
        "SELECT %(prefix)s || sha256 AS key FROM ai_hosted_plugin_files WHERE plugin_id = %(id)s"
        " UNION SELECT %(prefix)s || zip_sha256 FROM ai_hosted_plugin_versions"
        " WHERE plugin_id = %(id)s"
    )
    args = {"prefix": prefix, "id": plugin_id, "kind": HOLDER, "holder": str(plugin_id)}
    conn.execute(
        "INSERT INTO blob_refs (key, holder_kind, holder_id)"
        f" SELECT key, %(kind)s, %(holder)s FROM ({named}) AS named ON CONFLICT DO NOTHING",
        args,
    )
    conn.execute(
        "DELETE FROM blob_refs WHERE holder_kind = %(kind)s AND holder_id = %(holder)s"
        f" AND key NOT IN ({named})",
        args,
    )
