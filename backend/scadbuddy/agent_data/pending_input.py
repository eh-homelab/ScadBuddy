"""The gate's pending input and responses beyond its writers (#5299 step 10), for
``api/crud_agent_pending.py`` area ``pending-input``.

Each function is the SQL of an agent store, statement for statement: the reads of
``agent/src/gate/projection.ts`` `PendingProjection` (the badge's and the panel's list, which
entries are a flow run's, a tab wait, what a request recorded, an entry's trace); the orphan
sweep of ``gate/sweep.ts`` (``cancel_orphans`` resolves ``cancelled`` the entries no workflow run
will, one guarded ``DELETE … RETURNING`` and one transaction each, its events, the session's
``idle`` and an approval's audit row in that transaction); a durable turn's reads and its trace
mark in ``sessions/turnHost.ts`` (the mark only while the turn's placement holds the session,
checked ``FOR SHARE`` in the mark's transaction); and the recorded decisions a gated tool call
runs on (``temporal/toolActivities.ts`` `PgApprovalRecords`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from scadbuddy.agent_data import audit_log, gate

#: The most entries one list answers (the agent's ``LIMIT 500``).
ENTRIES_MAX = 500
#: The most runs one sweep pass describes (the agent's ``LIMIT 100``).
RUNS_MAX = 100
#: Why the sweep ended an entry (the agent's ``ORPHAN_REASON``).
ORPHAN_REASON = "the session's workflow run has ended"


def entries(
    conn: Connection[Any],
    *,
    session_id: str | None,
    owner: tuple[str, str] | None,
    all_approvals: bool,
    own_answers: bool,
) -> list[dict[str, Any]]:
    """`PendingProjection.entries`: of one session; of sessions ``owner`` owns or started (and
    every approval not in a flow run, with ``all_approvals``); a browser user's answers only."""
    kind, owner_id = owner if owner is not None else (None, None)
    return conn.execute(
        "SELECT p.request_id, p.kind, p.session_id::text AS session_id, p.tool, p.summary,"
        " p.input_hash, p.prompt, p.requested_by, p.responders, p.attention, p.flow_run_id, p.hop,"
        " p.outside_reason, p.created_at, p.expires_at, (p.expires_at <= now()) AS expiring"
        " FROM ai_pending_input p JOIN ai_sessions s ON s.id = p.session_id"
        " WHERE (%(sid)s::uuid IS NULL OR p.session_id = %(sid)s::uuid)"
        " AND (%(anyone)s"
        "  OR (%(all)s AND p.kind = 'approval' AND p.flow_run_id IS NULL)"
        "  OR (s.owner_kind = %(kind)s AND s.owner_id = %(id)s)"
        "  OR (s.creator_kind = %(kind)s AND s.creator_id = %(id)s))"
        " AND (%(any_answers)s OR p.kind = 'approval' OR p.flow_run_id IS NOT NULL"
        "  OR s.owner_kind = 'browser')"
        " ORDER BY p.created_at, p.request_id LIMIT %(limit)s",
        {
            "sid": session_id,
            "anyone": owner is None,
            "all": all_approvals,
            "kind": kind,
            "id": owner_id,
            "any_answers": not own_answers,
            "limit": ENTRIES_MAX,
        },
    ).fetchall()


def in_run(conn: Connection[Any], request_ids: list[str]) -> list[str]:
    """Which of ``request_ids`` are a flow run's entries."""
    rows = conn.execute(
        "SELECT request_id FROM ai_pending_input WHERE request_id = ANY(%s)"
        " AND flow_run_id IS NOT NULL",
        (request_ids,),
    ).fetchall()
    return [str(r["request_id"]) for r in rows]


def waits_for_tab(conn: Connection[Any], session_id: str) -> bool:
    """Whether an AgentSession session has a pending ``tab_disconnected`` entry (#5003)."""
    row = conn.execute(
        "SELECT 1 AS n FROM ai_pending_input p JOIN ai_sessions s ON s.id = p.session_id"
        " AND s.runtime = 'unified' WHERE p.session_id = %s::uuid"
        " AND p.attention->>'reason' = 'tab_disconnected' LIMIT 1",
        (session_id,),
    ).fetchone()
    return row is not None


def decided(conn: Connection[Any], request_ids: list[str]) -> list[str]:
    """Which of ``request_ids`` have ended in their record (``ai_input_responses``)."""
    rows = conn.execute(
        "SELECT request_id FROM ai_input_responses WHERE request_id = ANY(%s)", (request_ids,)
    ).fetchall()
    return [str(r["request_id"]) for r in rows]


def recorded(conn: Connection[Any], request_id: str) -> dict[str, Any] | None:
    """What the gate recorded for a resolved request, or None."""
    row: dict[str, Any] | None = conn.execute(
        "SELECT kind, outcome, response, response_hash, responder, reason, decision_traceparent"
        " FROM ai_input_responses WHERE request_id = %s",
        (request_id,),
    ).fetchone()
    return row


def park_trace(conn: Connection[Any], request_id: str) -> dict[str, Any] | None:
    """An open entry's tool, when it was asked, and the parked call's span context."""
    row: dict[str, Any] | None = conn.execute(
        "SELECT tool, created_at, traceparent FROM ai_pending_input WHERE request_id = %s",
        (request_id,),
    ).fetchone()
    return row


def mark_decision(conn: Connection[Any], request_id: str, traceparent: str) -> None:
    """The decision's span on the entry, before the workflow is asked (#5041)."""
    conn.execute(
        "UPDATE ai_pending_input SET decision_traceparent = %s WHERE request_id = %s",
        (traceparent, request_id),
    )


def session(conn: Connection[Any], session_id: str) -> dict[str, Any] | None:
    """Who owns and who started a session, or None (a malformed id names none)."""
    if not gate._is_uuid(session_id):
        return None
    row: dict[str, Any] | None = conn.execute(
        "SELECT owner_kind, owner_id, owner_label, creator_kind, creator_id FROM ai_sessions"
        " WHERE id = %s::uuid",
        (session_id,),
    ).fetchone()
    return row


def mark_park(
    conn: Connection[Any],
    request_id: str,
    traceparent: str,
    *,
    session_id: str,
    attempt: str,
    turn_id: str,
) -> bool:
    """A durable turn's parked call's span context on its entry (#5041), only while the turn's
    placement still holds the session (``turn_attempt`` and ``durable_turn_id``, checked ``FOR
    SHARE`` in this transaction) and the entry is that session's. False when nothing was marked."""
    if not (gate._is_uuid(session_id) and gate._is_uuid(turn_id)):
        return False
    with conn.transaction():
        holds = conn.execute(
            "SELECT 1 FROM ai_sessions WHERE id = %s::uuid AND turn_attempt = %s"
            " AND durable_turn_id = %s::uuid FOR SHARE",
            (session_id, attempt, turn_id),
        ).fetchone()
        if holds is None:
            return False
        marked = conn.execute(
            "UPDATE ai_pending_input SET traceparent = %s WHERE request_id = %s"
            " AND session_id = %s::uuid RETURNING request_id",
            (traceparent, request_id, session_id),
        ).fetchone()
        return marked is not None


def sweep_runs(conn: Connection[Any], min_age_s: int, recheck_s: int) -> list[dict[str, Any]]:
    """The runs whose rows are older than ``min_age_s`` and not checked within ``recheck_s``."""
    return conn.execute(
        "SELECT DISTINCT workflow_id, workflow_run_id FROM ai_pending_input"
        " WHERE created_at < now() - make_interval(secs => %(age)s)"
        " AND (last_checked_at IS NULL"
        "  OR last_checked_at < now() - make_interval(secs => %(recheck)s))"
        " LIMIT %(limit)s",
        {"age": min_age_s, "recheck": recheck_s, "limit": RUNS_MAX},
    ).fetchall()


def run_checked(conn: Connection[Any], workflow_id: str, run_id: str) -> None:
    """An open run's rows, stamped as found open now."""
    conn.execute(
        "UPDATE ai_pending_input SET last_checked_at = now()"
        " WHERE workflow_id = %s AND workflow_run_id = %s",
        (workflow_id, run_id),
    )


def _audit_cancelled(conn: Connection[Any], row: dict[str, Any], reason: str) -> None:
    """The sweep's ``approval`` row, as ``gate/sweep.ts`` ``auditCancelled`` records it."""
    requested = (row["requested_by"] or {}).get("label")
    detail = f"{row['tool']}: {reason} (requested by {'?' if requested is None else requested})"
    audit_log.insert(
        conn,
        audit_log.Entry(
            kind="approval",
            action="cancelled",
            surface="system",
            actor_kind=gate.SYSTEM["kind"],
            actor_id=gate.SYSTEM["id"],
            actor_label=gate.SYSTEM["label"],
            outcome="refused",
            session_id=row["session_id"],
            tool_use_id=gate._tool_use_id(row["request_id"]),
            tier="outward",
            input_hash=row["input_hash"],
            input_summary=audit_log.cap(row["summary"], 1000),
            request_id=audit_log.cap(row["request_id"], 500),
            detail=audit_log.cap(detail, 500),
            started_at=row["created_at"],
            finished_at=datetime.now(UTC),
        ),
    )


def cancel_orphans(
    conn: Connection[Any],
    *,
    run: tuple[str, str] | None = None,
    session_id: str | None = None,
    reason: str = ORPHAN_REASON,
) -> tuple[int, list[gate.Logged]]:
    """`cancelOrphanedEntries`: the entries of one ended run, or of a session whose workflow is
    gone, resolved ``cancelled``, each in its own transaction with its events, the session's
    ``idle`` once it holds none, and an approval's audit row. An entry the workflow resolved
    first is not written twice. How many it resolved, and the batches it appended."""
    if run is not None:
        ids = conn.execute(
            "SELECT request_id FROM ai_pending_input WHERE workflow_id = %s"
            " AND workflow_run_id = %s",
            run,
        ).fetchall()
    else:
        ids = conn.execute(
            "SELECT request_id FROM ai_pending_input WHERE session_id = %s::uuid", (session_id,)
        ).fetchall()
    removed = 0
    logged: list[gate.Logged] = []
    for found in ids:
        with conn.transaction():
            row = conn.execute(
                "DELETE FROM ai_pending_input WHERE request_id = %s RETURNING request_id,"
                " session_id::text AS session_id, kind, tool, summary, input_hash, requested_by,"
                " created_at",
                (found["request_id"],),
            ).fetchone()
            if row is None:
                continue
            sid, rid = row["session_id"], row["request_id"]
            conn.execute(
                "INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder,"
                " reason, tool, requested_at) VALUES (%s, %s, %s, 'cancelled', %s, %s, %s, %s)",
                (rid, sid, row["kind"], Jsonb(gate.SYSTEM), reason, row["tool"], row["created_at"]),
            )
            if row["kind"] == "approval":
                first = {
                    "type": "approval.resolved",
                    "sessionId": sid,
                    "id": rid,
                    "approved": False,
                    "decision": "cancelled",
                    "reason": reason,
                }
            else:
                first = {
                    "type": "question.resolved",
                    "sessionId": sid,
                    "id": rid,
                    "answered": False,
                    "reason": reason,
                }
            events = [
                gate._event(first),
                gate._event(
                    {
                        "type": "input.resolved",
                        "sessionId": sid,
                        "id": rid,
                        "kind": row["kind"],
                        "outcome": "cancelled",
                        **({"reason": reason} if reason else {}),
                    }
                ),
            ]
            idle = conn.execute(
                "UPDATE ai_sessions SET status = 'idle', updated_at = now() WHERE id = %(sid)s"
                " AND status IN ('waiting_approval', 'waiting_input')"
                " AND NOT EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = %(sid)s)"
                " RETURNING id",
                {"sid": sid},
            ).fetchone()
            if idle is not None:
                status = {"type": "session.status", "sessionId": sid, "status": "idle"}
                events.append(gate._event(status))
            gate._append(conn, logged, sid, events)
            if row["kind"] == "approval":
                _audit_cancelled(conn, row, reason)
            removed += 1
    return removed, logged


def approval(conn: Connection[Any], request_id: str, input_hash: str) -> dict[str, Any] | None:
    """The responder of a request's ``approved`` outcome given for this very call: its audit
    row's input hash is the call's (#2088). None for no such approval."""
    row = conn.execute(
        "SELECT r.responder FROM ai_input_responses r"
        " JOIN ai_audit a ON a.request_id = r.request_id AND a.kind = 'approval'"
        " AND a.action = 'approved'"
        " WHERE r.request_id = %s AND r.kind = 'approval' AND r.outcome = 'approved'"
        " AND a.input_hash = %s LIMIT 1",
        (request_id, input_hash),
    ).fetchone()
    if row is None:
        return None
    responder = row["responder"]
    return responder if isinstance(responder, dict) else {}
