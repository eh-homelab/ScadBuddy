"""DurableSession's own activities on the ``agent`` queue: a turn's settings and its end.

``finish_turn`` writes what a classic turn's end writes (agent/src/sessions/manager.ts):
``session.result`` with the lineage's spend and budget, the error if the turn did not
end well, and the final ``session.status``, in one transaction with the status. It
writes only while the session is still running a turn, so a retried activity writes
nothing twice (plan 5c Ruling 7).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import psycopg
from temporalio import activity

from scadbuddy_durable.gate.store import PROTOCOL_VERSION, _append
from scadbuddy_durable.session.models import MESSAGE_MAX, FinishTurn, GateSettings

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]

# approvals/service.ts and questions/service.ts: keys, defaults, bounds.
APPROVAL_EXPIRY_KEY = "approval_expiry_seconds"
QUESTION_EXPIRY_KEY = "question_expiry_seconds"
DEFAULT_APPROVAL_EXPIRY_S = 600
DEFAULT_QUESTION_EXPIRY_S = 3600
EXPIRY_MIN_S = 10
EXPIRY_MAX_S = 86_400
_RUNNING = ("running", "waiting_approval", "waiting_input")


def _seconds(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        return default
    return min(max(round(value), EXPIRY_MIN_S), EXPIRY_MAX_S)


def usd(amount: float) -> str:
    return f"${amount:.2f}"


def turn_events(
    session_id: str, args: FinishTurn, *, spent: float, budget: float, turns: int
) -> list[dict[str, Any]]:
    base = {"v": PROTOCOL_VERSION, "sessionId": session_id}
    events: list[dict[str, Any]] = [
        {**base, "type": "session.result", "costUsd": spent, "turns": turns, "budgetUsd": budget}
    ]
    if args.outcome == "budget_exhausted":
        message = f"this chat used its {usd(budget)} budget ({usd(spent)} spent)"
        events.append({**base, "type": "error", "code": "error_max_budget_usd", "message": message})
    elif args.outcome == "interrupted":
        events.append(
            {**base, "type": "error", "code": "interrupted", "message": "the turn was interrupted"}
        )
    elif args.outcome == "failed":
        text = (args.message or "the turn ended without a result")[:MESSAGE_MAX]
        events.append({**base, "type": "error", "code": "turn_failed", "message": text})
    status = "failed" if args.outcome == "failed" else "idle"
    events.append({**base, "type": "session.status", "status": status})
    return events


class SessionActivities:
    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @activity.defn(name="gate_settings")
    async def gate_settings(self) -> GateSettings:
        async with self._connect() as conn:
            cur = await conn.execute(
                "SELECT key, value FROM ai_settings WHERE key = ANY(%s)",
                ([APPROVAL_EXPIRY_KEY, QUESTION_EXPIRY_KEY],),
            )
            values = {
                k: (json.loads(v) if isinstance(v, str) else v) for k, v in await cur.fetchall()
            }
        return GateSettings(
            approval_expiry_s=_seconds(values.get(APPROVAL_EXPIRY_KEY), DEFAULT_APPROVAL_EXPIRY_S),
            question_expiry_s=_seconds(values.get(QUESTION_EXPIRY_KEY), DEFAULT_QUESTION_EXPIRY_S),
        )

    @activity.defn(name="finish_turn")
    async def finish_turn(self, args: FinishTurn) -> bool:
        sid = args.session_id
        async with self._connect() as conn, conn.transaction():
            cur = await conn.execute(
                """
                SELECT s.turns,
                  coalesce((SELECT r.budget_usd FROM ai_sessions r
                            WHERE r.id = coalesce(s.budget_root_id, s.id)), s.budget_usd),
                  (SELECT sum(m.cost_usd) FROM ai_sessions m
                   WHERE coalesce(m.budget_root_id, m.id) = coalesce(s.budget_root_id, s.id))
                FROM ai_sessions s WHERE s.id = %s AND s.status = ANY(%s) FOR UPDATE OF s
                """,
                (sid, list(_RUNNING)),
            )
            row = await cur.fetchone()
            if row is None:
                return False  # written already, or the session is gone
            turns, budget, spent = int(row[0]), float(row[1]), float(row[2] or 0)
            events = turn_events(sid, args, spent=spent, budget=budget, turns=turns)
            await conn.execute(
                "UPDATE ai_sessions SET status = %s, updated_at = now() WHERE id = %s",
                (events[-1]["status"], sid),
            )
            await _append(conn, sid, events)
            return True
