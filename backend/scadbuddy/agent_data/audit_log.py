"""The agent's audit log (#5299 step 10), for ``api/crud_agent_audit.py`` area ``audit``: the
agent's ``audit/log.ts`` `AuditLog` statement for statement.

``insert`` is ``AuditLog.insert``: one ``ai_audit`` row, a tool call's approver copied from its
``ai_approvals`` row or, for a durable call, its request's ``approved`` response, in the same
statement. The agent caps every text before it sends it (its ``cap`` counts UTF-16 units); the
route refuses a longer one. ``page`` is ``AuditLog.list``: newest first, keyset by id. ``prune``
is ``AuditLog.prune``: the one DELETE the table's trigger lets through, as it sets
``scadbuddy.audit_prune`` for its own transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection

from scadbuddy.agent_data import gate

#: The retention's bounds (the agent's ``MIN_AUDIT_RETENTION_DAYS``, ``MAX_…``).
RETENTION_MIN_DAYS = 1
RETENTION_MAX_DAYS = 3650
#: The page sizes (the agent's ``DEFAULT_PAGE``, ``MAX_PAGE``).
DEFAULT_PAGE = 50
MAX_PAGE = 200


@dataclass(frozen=True)
class Entry:
    """The agent's ``AuditEntry``, its texts already capped."""

    kind: str
    action: str
    surface: str
    actor_kind: str
    actor_id: str
    actor_label: str
    outcome: str
    client_ip: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    tool_use_id: str | None = None
    tier: str | None = None
    input_hash: str | None = None
    input_summary: str | None = None
    approval_id: str | None = None
    request_id: str | None = None
    detail: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cost_usd: float | None = None
    cost_priced: bool | None = None
    cost_estimated_usd: float | None = None


def cap(text: str, limit: int) -> str:
    """The agent's ``audit/log.ts`` ``cap``: at most ``limit`` UTF-16 units, cut between code
    points (a surrogate pair at the boundary goes whole), with ``…`` when cut."""
    units = text.encode("utf-16-le", "surrogatepass")
    if len(units) // 2 <= limit:
        return text
    kept = units[: 2 * (limit - 1)]
    if kept and 0xD800 <= int.from_bytes(kept[-2:], "little") <= 0xDBFF:
        kept = kept[:-2]
    return kept.decode("utf-16-le", "surrogatepass") + "…"


def _uuid_or_none(value: str | None) -> str | None:
    return value if value and gate._is_uuid(value) else None


def _usd(value: float | None) -> float | None:
    """A cost as stored: never below zero, null when unknown (the agent's ``usdOrNull``)."""
    if value is None or value != value or value in (float("inf"), float("-inf")):
        return None
    return max(value, 0.0)


def insert(conn: Connection[Any], e: Entry) -> None:
    started, finished = e.started_at, e.finished_at
    duration = (
        max(0, int((finished - started).total_seconds() * 1000))
        if started is not None and finished is not None
        else None
    )
    approval_id = _uuid_or_none(e.approval_id)
    tool_call = e.kind == "tool_call"
    params: dict[str, Any] = {
        "kind": e.kind,
        "action": e.action,
        "surface": e.surface,
        "pkind": e.actor_kind,
        "pid": e.actor_id,
        "plabel": e.actor_label,
        "ip": e.client_ip,
        "session": _uuid_or_none(e.session_id),
        "turn": _uuid_or_none(e.turn_id),
        "tool_use": e.tool_use_id,
        "tier": e.tier,
        "hash": e.input_hash,
        "summary": e.input_summary,
        "approval": approval_id,
        "tool_call": tool_call,
        # A durable call has no ai_approvals row: its approver is its request's responder.
        "responder_of": e.request_id if tool_call else None,
        "request": e.request_id,
        "outcome": e.outcome,
        "detail": e.detail,
        "started": started,
        "finished": finished,
        "duration": duration,
        "cost": _usd(e.cost_usd),
        "priced": e.cost_priced,
        "estimated": _usd(e.cost_estimated_usd),
    }
    approved = (
        "FROM ai_approvals WHERE id = %(approval)s::uuid AND decision = 'approved'"
        " AND %(tool_call)s"
    )
    responder = (
        "FROM ai_input_responses WHERE request_id = %(responder_of)s AND outcome = 'approved'"
    )
    by = {
        field: f"COALESCE((SELECT decided_by_{field} {approved}),"
        f" (SELECT responder->>'{field}' {responder}))"
        for field in ("kind", "id", "label")
    }
    conn.execute(
        "INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,"
        " principal_label, client_ip, session_id, turn_id, tool_use_id, tier, input_hash,"
        " input_summary, approval_id, approved_by_kind, approved_by_id, approved_by_label,"
        " request_id, outcome, detail, started_at, finished_at, duration_ms, cost_usd,"
        " cost_priced, cost_estimated_usd)"
        " VALUES (%(kind)s, %(action)s, %(surface)s, %(pkind)s, %(pid)s, %(plabel)s, %(ip)s,"
        " %(session)s, %(turn)s, %(tool_use)s, %(tier)s, %(hash)s, %(summary)s, %(approval)s,"
        f" {by['kind']}, {by['id']}, {by['label']},"
        " %(request)s, %(outcome)s, %(detail)s, %(started)s, %(finished)s, %(duration)s,"
        " %(cost)s, %(priced)s, %(estimated)s)",
        params,
    )


@dataclass(frozen=True)
class Filter:
    """The agent's ``AuditFilter``."""

    kind: str | None = None
    outcome: str | None = None
    surface: str | None = None
    action: str | None = None
    session_id: str | None = None
    principal: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    before: int | None = None
    limit: int | None = None


_COLUMNS = (
    "id::text AS id, at, kind, action, surface, principal_kind, principal_id, principal_label,"
    " client_ip, session_id::text AS session_id, turn_id::text AS turn_id, tool_use_id, tier,"
    " input_hash, input_summary, approval_id::text AS approval_id, approved_by_kind,"
    " approved_by_id, approved_by_label, outcome, detail, started_at, finished_at, duration_ms,"
    " cost_usd, cost_priced, cost_estimated_usd"
)


def page(conn: Connection[Any], f: Filter) -> tuple[list[dict[str, Any]], str | None]:
    """A page of rows, newest first, and the next page's cursor (None on the last)."""
    if f.session_id is not None and not gate._is_uuid(f.session_id):
        return [], None
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for column, value, cast in (
        ("kind", f.kind, ""),
        ("outcome", f.outcome, ""),
        ("surface", f.surface, ""),
        ("action", f.action, ""),
        ("session_id", f.session_id, "::uuid"),
        ("principal_id", f.principal, ""),
    ):
        if value is not None:
            params[column] = value
            clauses.append(f"{column} = %({column})s{cast}")
    if f.since is not None:
        params["since"] = f.since
        clauses.append("at >= %(since)s")
    if f.until is not None:
        params["until"] = f.until
        clauses.append("at < %(until)s")
    if f.before is not None:
        params["before"] = f.before
        clauses.append("id < %(before)s")
    limit = min(max(f.limit if f.limit is not None else DEFAULT_PAGE, 1), MAX_PAGE)
    params["limit"] = limit + 1
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM ai_audit {where} ORDER BY ai_audit.id DESC LIMIT %(limit)s",
        params,
    ).fetchall()
    kept = rows[:limit]
    return kept, (str(kept[-1]["id"]) if len(rows) > limit and kept else None)


def prune(conn: Connection[Any], days: int) -> int:
    """Deletes the rows older than ``days`` (clamped to the retention's bounds); how many."""
    days = min(max(days, RETENTION_MIN_DAYS), RETENTION_MAX_DAYS)
    with conn.transaction():
        conn.execute("SELECT set_config('scadbuddy.audit_prune', 'on', true)")
        deleted = conn.execute(
            "DELETE FROM ai_audit WHERE at < now() - (%s * interval '1 day')", (days,)
        )
        return deleted.rowcount
