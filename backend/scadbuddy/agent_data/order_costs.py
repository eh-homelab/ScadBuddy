"""The order items a session revised and its spend's report to them, behind CRUD routes (#5299
step 9, part c): the agent's ``sessions/orderCosts.ts`` ``PgOrderCostRows`` statement for
statement, for ``api/crud_agent_order_costs.py`` (area ``session-order-costs``).

A session's report runs under its lease (``order-cost:<session id>``, ``leases.py``, #5299 D3),
which replaces the session advisory lock the agent held on a lock-client connection across its
posts. Each write of a report checks in its own transaction that the caller's token still holds
the lease (``held_in``), so a report whose lease lapsed writes nothing."""

from __future__ import annotations

from typing import Any, Literal

from psycopg import Connection

from scadbuddy.agent_data import leases

#: How long a row waits before the sweep reports it, and how long an idle session is not over
#: (``orderCosts.ts`` ``SWEEP_AFTER``, ``IDLE_OVER_AFTER``).
SWEEP_AFTER = "15 minutes"
IDLE_OVER_AFTER = "1 hour"


class LeaseLostError(Exception):
    """The caller's token no longer holds the session's lease: nothing is written."""


def lease_key(session_id: str) -> str:
    return f"order-cost:{session_id}"


def _held(conn: Connection[Any], session_id: str, token: str) -> None:
    if not leases.held_in(conn, lease_key(session_id), token):
        raise LeaseLostError(f"the report lease of session {session_id} is not held")


def insert(conn: Connection[Any], session_id: str, order_id: str, item_id: str) -> None:
    """A revision that succeeded, unless its order was already reported in this session."""
    conn.execute(
        "INSERT INTO ai_session_order_costs (session_id, order_id, order_item_id)"
        " SELECT %(session)s::uuid, %(order)s, %(item)s"
        " WHERE NOT EXISTS (SELECT 1 FROM ai_session_order_costs"
        " WHERE session_id = %(session)s::uuid AND order_id = %(order)s"
        " AND reported_at IS NOT NULL)"
        " AND EXISTS (SELECT 1 FROM ai_sessions WHERE id = %(session)s::uuid)"
        " ON CONFLICT DO NOTHING",
        {"session": session_id, "order": order_id, "item": item_id},
    )


def sweep_sessions(conn: Connection[Any], limit: int) -> list[str]:
    """The sessions no turn is running with rows left to report for ``SWEEP_AFTER``: those
    never tried first, then those tried longest ago."""
    rows = conn.execute(
        "SELECT c.session_id::text AS session_id FROM ai_session_order_costs c"
        " JOIN ai_sessions s ON s.id = c.session_id"
        " WHERE c.reported_at IS NULL AND c.at < now() - %s::interval"
        " AND s.status NOT IN ('running', 'waiting_approval', 'waiting_input')"
        " GROUP BY c.session_id ORDER BY max(c.tried_at) NULLS FIRST, min(c.at) LIMIT %s",
        (SWEEP_AFTER, limit),
    ).fetchall()
    return [r["session_id"] for r in rows]


def begin(conn: Connection[Any], session_id: str, token: str) -> dict[str, Any] | None:
    """A report's start under the lease, in one transaction: the rows still to report (each
    marked tried) and the session's spend, what earlier reports carried, its principal and
    whether it is over. None when nothing is left to report or the session is gone."""
    with conn.transaction():
        _held(conn, session_id, token)
        pending = conn.execute(
            "SELECT order_id, order_item_id, attempted_usd FROM ai_session_order_costs"
            " WHERE session_id = %s::uuid AND reported_at IS NULL"
            " ORDER BY at, order_id, order_item_id",
            (session_id,),
        ).fetchall()
        if not pending:
            return None
        conn.execute(
            "UPDATE ai_session_order_costs SET tried_at = now()"
            " WHERE session_id = %s::uuid AND reported_at IS NULL",
            (session_id,),
        )
        row = conn.execute(
            "SELECT s.cost_usd AS cost, s.owner_id AS principal,"
            " (s.status IN ('done', 'failed') OR (s.status = 'idle'"
            " AND s.updated_at < now() - %s::interval)) AS over,"
            " (SELECT COALESCE(SUM(COALESCE(reported_usd, 0) + COALESCE(attempted_usd, 0)), 0)"
            " FROM ai_session_order_costs WHERE session_id = s.id) AS carried"
            " FROM ai_sessions s WHERE s.id = %s::uuid",
            (IDLE_OVER_AFTER, session_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "pending": [
                {
                    "order_id": p["order_id"],
                    "order_item_id": p["order_item_id"],
                    "attempted_usd": p["attempted_usd"],
                }
                for p in pending
            ],
            "cost": float(row["cost"]),
            "carried": float(row["carried"]),
            "principal": row["principal"],
            "over": bool(row["over"]),
        }


def attempt(
    conn: Connection[Any],
    session_id: str,
    token: str,
    order_id: str,
    items: list[str],
    amount_usd: float,
) -> None:
    """The share a report is about to send, split over the order's items read."""
    with conn.transaction():
        _held(conn, session_id, token)
        conn.execute(
            "UPDATE ai_session_order_costs SET attempted_usd = %s"
            " WHERE session_id = %s::uuid AND order_id = %s AND order_item_id = ANY(%s)"
            " AND reported_at IS NULL",
            (amount_usd / len(items), session_id, order_id, items),
        )


Settle = Literal["reported", "unsent", "closed"]

_SETTLE = {
    # Recorded (or refused for good): reported, carrying what was sent.
    "reported": "reported_at = now(), reported_usd = COALESCE(attempted_usd, 0),"
    " attempted_usd = NULL",
    # Under one minor unit while the session goes on: sent again with more spend.
    "unsent": "attempted_usd = NULL",
    # A share that can never be written: reported, nothing carried, out of the sweep.
    "closed": "reported_at = now(), reported_usd = 0, attempted_usd = NULL",
}


def settle(
    conn: Connection[Any], session_id: str, token: str, order_id: str, outcome: Settle
) -> None:
    """An order's answer, written on its rows still to report."""
    with conn.transaction():
        _held(conn, session_id, token)
        conn.execute(
            (
                "UPDATE ai_session_order_costs SET " + _SETTLE[outcome] + " WHERE session_id ="
                " %s::uuid AND order_id = %s AND reported_at IS NULL"
            ).encode(),
            (session_id, order_id),
        )
