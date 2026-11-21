"""The AADs of the agent's remote plugin secrets and sign-ins (#5299 step 5), and their
rotation: a plugin's header value (``ai_plugins``), a sign-in's tokens (``ai_plugin_oauth``)
and a sign-in under way (``ai_plugin_oauth_flows``). The AADs are the agent's
(``agent/src/plugins/registry.ts`` ``pluginAad``, ``plugins/oauthStore.ts`` ``grantAad``,
``flowAad``), byte for byte, so a row sealed on either side opens on the other. The routes
that seal and open them are `api/crud_agent_plugins.py`."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from psycopg import Connection

from scadbuddy.core.secrets import Envelope, Kek, SealError
from scadbuddy.core.secrets import rewrap as rewrap_envelope


def js_json(value: Any) -> str:
    """``JSON.stringify(value)`` for the strings, numbers, booleans and objects an AAD
    holds: no spaces, keys in the order given, non-ASCII text as it is."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def plugin_aad(name: str, url: str, header: str) -> str:
    return f"ai_plugins:{js_json({'name': name, 'url': url, 'auth_header': header})}"


def grant_aad(row: Mapping[str, Any]) -> str:
    return "ai_plugin_oauth:" + js_json(
        {
            "plugin": row["plugin"],
            "plugin_url": row["plugin_url"],
            "resource": row["resource"],
            "issuer": row["issuer"],
            "token_endpoint": row["token_endpoint"],
            "client_id": row["client_id"],
        }
    )


def flow_aad(state_hash: str, plugin: str, plugin_url: str, meta: Mapping[str, Any]) -> str:
    return "ai_plugin_oauth_flows:" + js_json(
        {
            "state_hash": state_hash,
            "plugin": plugin,
            "plugin_url": plugin_url,
            "issuer": meta["issuer"],
            "token_endpoint": meta["token_endpoint"],
            "client_id": meta["client_id"],
            "redirect_uri": meta["redirect_uri"],
        }
    )


def envelope_of(row: Mapping[str, Any]) -> Envelope:
    return Envelope(
        secret_sealed=bytes(row["secret_sealed"]),
        dek_sealed=bytes(row["dek_sealed"]),
        kek_id=row["kek_id"],
    )


def _rows(conn: Connection[Any], sql: str, kek_id: str) -> list[dict[str, Any]]:
    cur = conn.execute(sql, (kek_id,))
    names = [d.name for d in cur.description or []]
    return [
        dict(row) if isinstance(row, dict) else dict(zip(names, row, strict=True))
        for row in cur.fetchall()
    ]


def rewrap(conn: Connection[Any], previous: Kek, current: Kek) -> tuple[int, int]:
    """(re-wrapped, failed) over the plugins' header values and the sign-ins, as the agent's
    ``PluginStore.rewrapFrom`` and ``PluginOAuth.rewrapFrom`` did at its start. Each update
    is conditional on ``kek_id``, so two pods rotating at once do not overwrite each other;
    a row ``previous`` cannot open is left and counted as failed. A sign-in under way
    (a flow, at most ten minutes old) is dropped, not re-wrapped."""
    if previous.id == current.id:
        return 0, 0
    rewrapped = failed = 0
    for row in _rows(
        conn,
        "SELECT name, url, auth_header, secret_sealed, dek_sealed, kek_id FROM ai_plugins"
        " WHERE kek_id = %s AND secret_sealed IS NOT NULL AND auth_header IS NOT NULL",
        previous.id,
    ):
        aad = plugin_aad(row["name"], row["url"], row["auth_header"])
        try:
            resealed = rewrap_envelope(previous, current, envelope_of(row), aad)
        except SealError:
            failed += 1
            continue
        cur = conn.execute(
            "UPDATE ai_plugins SET dek_sealed = %s, kek_id = %s WHERE name = %s AND kek_id = %s",
            (resealed.dek_sealed, resealed.kek_id, row["name"], previous.id),
        )
        rewrapped += cur.rowcount
    conn.execute("DELETE FROM ai_plugin_oauth_flows WHERE kek_id = %s", (previous.id,))
    for row in _rows(
        conn,
        "SELECT plugin, plugin_url, resource, issuer, token_endpoint, client_id,"
        " secret_sealed, dek_sealed, kek_id FROM ai_plugin_oauth WHERE kek_id = %s",
        previous.id,
    ):
        try:
            resealed = rewrap_envelope(previous, current, envelope_of(row), grant_aad(row))
        except SealError:
            failed += 1
            continue
        cur = conn.execute(
            "UPDATE ai_plugin_oauth SET dek_sealed = %s, kek_id = %s"
            " WHERE plugin = %s AND kek_id = %s",
            (resealed.dek_sealed, resealed.kek_id, row["plugin"], previous.id),
        )
        rewrapped += cur.rowcount
    return rewrapped, failed
