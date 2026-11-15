"""The agent's classic approvals (#5299 step 10), for ``api/crud_agent_approvals.py`` area
``approvals``: the SQL of the agent's ``approvals/service.ts`` `ApprovalService`, statement for
statement, ``ai_approvals`` and the session reads its checks take.

Who may see or decide an approval is the agent's check, made on the rows these reads answer, as
before; what the agent may no longer do is write a decision the SQL would not have written:
``settle`` is the guarded UPDATE (a pending row, before its expiry unless it expires or cancels
it), its ``decision_traceparent``, its session events and its ``approval`` audit row in one
transaction, so a decision is never without its audit row; ``consume`` uses an approved
sessionless row once, only for its asker and its input hash; ``create_prepared`` keeps the MCP
prepares' bounds under one advisory lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from psycopg import Connection

from scadbuddy.agent_data import audit_log, gate

Decision = Literal["approved", "denied", "expired", "cancelled"]

#: The expiry's bounds (the agent's ``MIN_APPROVAL_EXPIRY_SECONDS``, ``MAX_…``).
EXPIRY_MIN_S = 10
EXPIRY_MAX_S = 86_400
#: The most rows one list answers (the agent's ``LIMIT 500``).
LIST_MAX = 500
#: The MCP prepares' one lock (the agent's ``PREPARE_LOCK``).
PREPARE_LOCK = "scadbuddy:ai_approvals:mcp_prepare"

COLUMNS = (
    "id::text AS id, session_id::text AS session_id, turn_id::text AS turn_id, tool_use_id, tool,"
    " input_summary, input_hash, tier, requested_by_kind, requested_by_id, requested_by_label,"
    " requested_tiers, created_at, expires_at, decision, decided_by_kind, decided_by_id,"
    " decided_by_label, decided_at, reason, usable_until, resume_turn_id::text AS resume_turn_id,"
    " consumed_at, revoked_at, traceparent, decision_traceparent,"
    " (decision IS NULL AND expires_at <= now()) AS due"
)
USABLE = (
    "decision = 'approved' AND consumed_at IS NULL AND revoked_at IS NULL AND usable_until > now()"
)


def _ttl(seconds: int) -> int:
    return min(max(seconds, EXPIRY_MIN_S), EXPIRY_MAX_S)


def row(conn: Connection[Any], approval_id: str) -> dict[str, Any] | None:
    if not gate._is_uuid(approval_id):
        return None
    found: dict[str, Any] | None = conn.execute(
        f"SELECT {COLUMNS} FROM ai_approvals WHERE id = %s", (approval_id,)
    ).fetchone()
    return found


def session_access(conn: Connection[Any], session_id: str) -> dict[str, Any] | None:
    """Who owns and started a session, and a live handoff offer's target."""
    if not gate._is_uuid(session_id):
        return None
    found: dict[str, Any] | None = conn.execute(
        "SELECT owner_kind, owner_id, owner_label, creator_kind, creator_id,"
        " CASE WHEN pending_owner_until > now() THEN pending_owner_kind END AS offer_kind,"
        " CASE WHEN pending_owner_until > now() THEN pending_owner_id END AS offer_id"
        " FROM ai_sessions WHERE id = %s",
        (session_id,),
    ).fetchone()
    return found


def visible_pending(conn: Connection[Any], kind: str, principal: str) -> list[dict[str, Any]]:
    """The pending approvals of sessions ``principal`` owns or started, and its own sessionless."""
    return conn.execute(
        f"SELECT {COLUMNS} FROM ai_approvals a WHERE a.decision IS NULL AND ("
        " (a.session_id IS NULL AND a.requested_by_kind = %(k)s AND a.requested_by_id = %(i)s)"
        " OR EXISTS (SELECT 1 FROM ai_sessions s WHERE s.id = a.session_id"
        " AND ((s.owner_kind = %(k)s AND s.owner_id = %(i)s)"
        " OR (s.creator_kind = %(k)s AND s.creator_id = %(i)s))))"
        " ORDER BY a.created_at, a.id LIMIT %(limit)s",
        {"k": kind, "i": principal, "limit": LIST_MAX},
    ).fetchall()


def list_rows(conn: Connection[Any], session_id: str | None, pending: bool) -> list[dict[str, Any]]:
    """A session's approvals (pending only with ``pending``), or with none every pending one."""
    if session_id is None:
        return conn.execute(
            f"SELECT {COLUMNS} FROM ai_approvals WHERE decision IS NULL"
            " ORDER BY created_at, id LIMIT %s",
            (LIST_MAX,),
        ).fetchall()
    if not gate._is_uuid(session_id):
        return []
    only = " AND decision IS NULL" if pending else ""
    return conn.execute(
        f"SELECT {COLUMNS} FROM ai_approvals WHERE session_id = %s{only}"
        " ORDER BY created_at, id LIMIT %s",
        (session_id, LIST_MAX),
    ).fetchall()


def prepared(conn: Connection[Any], kind: str, principal: str) -> list[dict[str, Any]]:
    """A principal's sessionless approvals still waiting for a decision, oldest first."""
    return conn.execute(
        f"SELECT {COLUMNS} FROM ai_approvals WHERE session_id IS NULL"
        " AND requested_by_kind = %s AND requested_by_id = %s AND decision IS NULL"
        " AND expires_at > now() ORDER BY created_at, id LIMIT %s",
        (kind, principal, LIST_MAX),
    ).fetchall()


def due(conn: Connection[Any], tried: list[str], limit: int) -> list[str]:
    """The pending approvals past their time, oldest first, leaving out ``tried``."""
    rows = conn.execute(
        "SELECT id::text AS id FROM ai_approvals WHERE decision IS NULL AND expires_at <= now()"
        " AND NOT (id = ANY(%s::uuid[])) ORDER BY expires_at LIMIT %s",
        ([t for t in tried if gate._is_uuid(t)], limit),
    ).fetchall()
    return [str(r["id"]) for r in rows]


@dataclass(frozen=True)
class NewApproval:
    """A pending approval's row, its summary and hash made by the agent (its HMAC key)."""

    id: str
    session_id: str | None
    turn_id: str | None
    tool_use_id: str
    tool: str
    input_summary: str
    input_hash: str
    tier: str
    requested_by: dict[str, str]
    requested_tiers: list[str] | None
    expiry_s: int
    traceparent: str | None


def _insert(conn: Connection[Any], a: NewApproval) -> dict[str, Any]:
    by = a.requested_by
    inserted: dict[str, Any] | None = conn.execute(
        "INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id, tool, input_summary,"
        " input_hash, tier, requested_by_kind, requested_by_id, requested_by_label,"
        " requested_tiers, expires_at, traceparent) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
        f" %s, %s, %s, now() + (%s * interval '1 second'), %s) RETURNING {COLUMNS}",
        (
            a.id,
            a.session_id,
            a.turn_id,
            a.tool_use_id,
            a.tool,
            a.input_summary,
            a.input_hash,
            a.tier,
            by["kind"],
            by["id"],
            by["label"],
            a.requested_tiers,
            _ttl(a.expiry_s),
            a.traceparent,
        ),
    ).fetchone()
    if inserted is None:
        raise LookupError("approval vanished after insert")
    return inserted


def _entry(r: dict[str, Any]) -> dict[str, Any]:
    """The agent's ``approvalEntry``."""
    return {
        "id": f"approval:{r['id']}",
        "kind": "approval",
        "session_id": r["session_id"],
        "tool": r["tool"],
        "summary": r["input_summary"],
        "input_hash": r["input_hash"],
        "prompt": "",
        "requested_by": {
            "kind": r["requested_by_kind"],
            "id": r["requested_by_id"],
            "label": r["requested_by_label"],
        },
        "responders": ["browser", "grant"],
        "created_at": gate._iso(r["created_at"]),
        "expires_at": gate._iso(r["expires_at"]),
    }


def create(conn: Connection[Any], a: NewApproval) -> tuple[dict[str, Any], list[gate.Logged]]:
    """A pending approval and, in a session, its ``approval.required`` and ``input.requested``,
    committed together."""
    logged: list[gate.Logged] = []
    with conn.transaction():
        created = _insert(conn, a)
        sid = created["session_id"]
        if sid is not None:
            events = [
                gate._event(
                    {
                        "type": "approval.required",
                        "sessionId": sid,
                        "id": created["id"],
                        "tool": a.tool_use_id,
                        "summary": gate._cap(
                            f"{a.tool} {a.input_summary}", gate.APPROVAL_SUMMARY_MAX
                        ),
                        "risk": "outward",
                    }
                ),
                gate._event(
                    {"type": "input.requested", "sessionId": sid, "entry": _entry(created)}
                ),
            ]
            gate._append(conn, logged, sid, events)
    return created, logged


@dataclass(frozen=True)
class Settle:
    """A decision: who made it (None for the system), where, and its span's context."""

    approval_id: str
    decision: Decision
    by: dict[str, str] | None
    reason: str | None
    expiry_s: int
    traceparent: str | None
    surface: str | None = None
    client_ip: str | None = None


def settle(conn: Connection[Any], s: Settle) -> tuple[dict[str, Any] | None, list[gate.Logged]]:
    """Decides a pending approval once: the first decision wins. None when it was decided
    already (or, for approve and deny, has expired); nothing is written then."""
    if not gate._is_uuid(s.approval_id):
        return None, []
    ttl = _ttl(s.expiry_s) if s.decision == "approved" else 0
    by = s.by or {}
    logged: list[gate.Logged] = []
    with conn.transaction():
        settled: dict[str, Any] | None = conn.execute(
            "UPDATE ai_approvals SET decision = %(d)s, decided_by_kind = %(k)s,"
            " decided_by_id = %(i)s, decided_by_label = %(l)s, decided_at = now(),"
            " reason = %(r)s, usable_until = CASE WHEN %(d)s = 'approved'"
            " THEN now() + (%(ttl)s * interval '1 second') END,"
            " decision_traceparent = %(tp)s"
            " WHERE id = %(id)s AND decision IS NULL"
            " AND (%(d)s IN ('expired', 'cancelled') OR expires_at > now())"
            f" RETURNING {COLUMNS}",
            {
                "id": s.approval_id,
                "d": s.decision,
                "k": by.get("kind"),
                "i": by.get("id"),
                "l": by.get("label"),
                "r": s.reason,
                "ttl": ttl,
                "tp": s.traceparent,
            },
        ).fetchone()
        if settled is None:
            return None, []
        sid = settled["session_id"]
        if sid is not None:
            resolved: dict[str, Any] = {
                "type": "approval.resolved",
                "sessionId": sid,
                "id": s.approval_id,
                "approved": s.decision == "approved",
                "decision": s.decision,
            }
            if s.by is not None and s.decision in ("approved", "denied"):
                resolved["by"] = s.by
            if s.reason and s.decision in ("expired", "cancelled"):
                resolved["reason"] = s.reason
            ended: dict[str, Any] = {
                "type": "input.resolved",
                "sessionId": sid,
                "id": f"approval:{s.approval_id}",
                "kind": "approval",
                "outcome": s.decision,
            }
            if s.reason and s.decision in ("expired", "cancelled"):
                ended["reason"] = s.reason
            gate._append(conn, logged, sid, [gate._event(resolved), gate._event(ended)])
        _audited(conn, settled, s)
    return settled, logged


def _audited(conn: Connection[Any], a: dict[str, Any], s: Settle) -> None:
    """The decision's ``approval`` audit row (the agent's ``ApprovalService.audited``)."""
    actor = s.by if s.by is not None else gate.SYSTEM
    outcome = (
        "ok" if s.decision == "approved" else "denied" if s.decision == "denied" else "refused"
    )
    detail = (
        f"{a['tool']}{': ' + s.reason if s.reason else ''} (requested by {a['requested_by_label']})"
    )
    audit_log.insert(
        conn,
        audit_log.Entry(
            kind="approval",
            action=s.decision,
            surface=(s.surface or "http") if s.by is not None else "system",
            actor_kind=audit_log.cap(actor["kind"], 50),
            actor_id=audit_log.cap(actor["id"], 200),
            actor_label=audit_log.cap(actor["label"], 200),
            outcome=outcome,
            client_ip=s.client_ip,
            session_id=a["session_id"],
            turn_id=a["turn_id"],
            tool_use_id=a["tool_use_id"],
            tier=a["tier"],
            input_hash=a["input_hash"],
            input_summary=audit_log.cap(a["input_summary"], 1000),
            approval_id=a["id"],
            detail=audit_log.cap(detail, 500),
            started_at=a["created_at"],
            finished_at=a["decided_at"],
        ),
    )


def consume(
    conn: Connection[Any], approval_id: str, by: dict[str, str], digest: str
) -> dict[str, Any] | None:
    """Uses a sessionless approval once: its asker's, for its input, while usable."""
    if not gate._is_uuid(approval_id):
        return None
    used: dict[str, Any] | None = conn.execute(
        "UPDATE ai_approvals SET consumed_at = now() WHERE id = %s AND session_id IS NULL"
        f" AND requested_by_kind = %s AND requested_by_id = %s AND input_hash = %s AND {USABLE}"
        f" RETURNING {COLUMNS}",
        (approval_id, by["kind"], by["id"], digest),
    ).fetchone()
    return used


def create_prepared(
    conn: Connection[Any],
    a: NewApproval,
    *,
    per_principal: int,
    total: int,
    evict_reason: str,
    evict_traceparents: list[str],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """A sessionless pending approval within bounds, under the prepares' one advisory lock: the
    asker's oldest pending ones cancelled to keep it under ``per_principal`` (each its
    ``decision_traceparent`` from ``evict_traceparents``, in order, while they last); at
    ``total`` pending sessionless rows from anyone, nothing is written. The new row (None when
    full) and the rows it cancelled."""
    by = a.requested_by
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (PREPARE_LOCK,))
        own = conn.execute(
            "SELECT id::text AS id FROM ai_approvals WHERE session_id IS NULL"
            " AND requested_by_kind = %s AND requested_by_id = %s AND decision IS NULL"
            " AND expires_at > now() ORDER BY created_at, id",
            (by["kind"], by["id"]),
        ).fetchall()
        evict = [str(r["id"]) for r in own[: max(len(own) - per_principal + 1, 0)]]
        cancelled: list[dict[str, Any]] = []
        if evict:
            cancelled = conn.execute(
                "UPDATE ai_approvals SET decision = 'cancelled', decided_at = now(), reason = %s"
                f" WHERE id = ANY(%s::uuid[]) AND decision IS NULL RETURNING {COLUMNS}",
                (evict_reason, evict),
            ).fetchall()
            for gone, traceparent in zip(cancelled, evict_traceparents, strict=False):
                conn.execute(
                    "UPDATE ai_approvals SET decision_traceparent = %s WHERE id = %s",
                    (traceparent, gone["id"]),
                )
                gone["decision_traceparent"] = traceparent
        else:
            count = conn.execute(
                "SELECT count(*)::int AS n FROM ai_approvals WHERE session_id IS NULL"
                " AND decision IS NULL AND expires_at > now()"
            ).fetchone()
            if (count["n"] if count else 0) >= total:
                return None, []
        return _insert(conn, a), cancelled
