"""The rotation of the agent's Web Push keys and notification channel tokens (#5299 step 5):
each data key sealed under the previous KEK sealed again under the current one, as the
agent's ``PushStore.rewrapFrom`` and ``ChannelStore.rewrapFrom`` did at its start. The AADs
are the agent's (``agent/src/notify/pushStore.ts``, ``notify/channels.ts``), and the routes
that seal them are `api/crud_agent_notify.py`."""

from __future__ import annotations

from typing import Any

from psycopg import Connection

from scadbuddy.core.secrets import Envelope, Kek, SealError
from scadbuddy.core.secrets import rewrap as rewrap_envelope

VAPID_AAD = "push-vapid:1"


def subscription_aad(sub_id: str, endpoint: str) -> str:
    return f"push-subscription:{sub_id}:{endpoint}"


def channel_aad(channel_id: str, kind: str, url: str) -> str:
    return f"notify-channel:{channel_id}:{kind}:{url}"


def _rows(conn: Connection[Any], sql: str, kek_id: str) -> list[dict[str, Any]]:
    cur = conn.execute(sql, (kek_id,))
    names = [d.name for d in cur.description or []]
    return [
        dict(row) if isinstance(row, dict) else dict(zip(names, row, strict=True))
        for row in cur.fetchall()
    ]


def _rewrap_table(
    conn: Connection[Any],
    previous: Kek,
    current: Kek,
    *,
    select: str,
    update: str,
    aad: Any,
) -> tuple[int, int]:
    rewrapped = failed = 0
    for row in _rows(conn, select, previous.id):
        envelope = Envelope(
            secret_sealed=bytes(row["secret_sealed"]),
            dek_sealed=bytes(row["dek_sealed"]),
            kek_id=row["kek_id"],
        )
        try:
            resealed = rewrap_envelope(previous, current, envelope, aad(row))
        except SealError:
            failed += 1
            continue
        cur = conn.execute(update, (resealed.dek_sealed, resealed.kek_id, row["id"], previous.id))
        rewrapped += cur.rowcount
    return rewrapped, failed


def rewrap(conn: Connection[Any], previous: Kek, current: Kek) -> tuple[int, int]:
    """(re-wrapped, failed) over the VAPID key, the subscriptions and the channels; each
    update conditional on ``kek_id``, so two pods rotating at once do not overwrite each
    other, and a row ``previous`` cannot open is left and counted as failed."""
    if previous.id == current.id:
        return 0, 0
    totals = [
        _rewrap_table(
            conn,
            previous,
            current,
            select="SELECT id, secret_sealed, dek_sealed, kek_id FROM ai_push_vapid"
            " WHERE kek_id = %s",
            update="UPDATE ai_push_vapid SET dek_sealed = %s, kek_id = %s"
            " WHERE id = %s AND kek_id = %s",
            aad=lambda _row: VAPID_AAD,
        ),
        _rewrap_table(
            conn,
            previous,
            current,
            select="SELECT id::text AS id, endpoint, secret_sealed, dek_sealed, kek_id"
            " FROM ai_push_subscriptions WHERE kek_id = %s",
            update="UPDATE ai_push_subscriptions SET dek_sealed = %s, kek_id = %s"
            " WHERE id = %s AND kek_id = %s",
            aad=lambda row: subscription_aad(row["id"], row["endpoint"]),
        ),
        _rewrap_table(
            conn,
            previous,
            current,
            select="SELECT id::text AS id, kind, url, secret_sealed, dek_sealed, kek_id"
            " FROM ai_notify_channels WHERE kek_id = %s",
            update="UPDATE ai_notify_channels SET dek_sealed = %s, kek_id = %s"
            " WHERE id = %s AND kek_id = %s",
            aad=lambda row: channel_aad(row["id"], row["kind"], row["url"]),
        ),
    ]
    return sum(r for r, _ in totals), sum(f for _, f in totals)
