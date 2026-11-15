"""The questions the agent asks of the flow tables (#5299 D7), answered for its CRUD routes
(`api/crud_agent.py`). Each query is the one the agent ran over SQL before
(``agent/src/flows/backendReads.ts`` `SqlFlowRunReads` keeps a copy until every API pod
serves these routes), so the answers do not change. The agent reads; nothing here writes.

The JSON in ``workflow_runs.waiting_on`` and ``steps`` is passed on as the backend's
projection wrote it (`workflows/flow_models.py`), field by field: a time stays the
string it was written as.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

Pool = ConnectionPool[Connection[DictRow]]

#: A parked call's fields the agent reads (`FlowWaiting`), and a hop record's (`HopRecord`).
WAITING_FIELDS = ("call_id", "kind", "fn", "prompt", "since", "hop", "hop_until", "reason")
HOP_FIELDS = ("hop", "action", "actor", "at", "reason")
#: A step's fields the agent reads (`FlowStep`).
STEP_FIELDS = ("seq", "fn", "call_id", "status", "session_id")
#: What `decision_checks` answers.
DECISION_CHECKS = ("approved", "approved_unresolved", "by_person", "lapsed")
#: The most runs `waiting_runs` lists: past that, the longest-waiting ones are left out.
WAITING_RUNS_MAX = 200

#: A run grant's approval holds only while the grant is in force (#2481).
_GRANT_IN_FORCE = (
    "EXISTS (SELECT 1 FROM flow_grants g WHERE 'grant:' || g.id = d.responder"
    " AND g.status = 'approved' AND g.revoked_at IS NULL AND now() < g.expires_at)"
)
#: One approval of the call with this hash.
_APPROVED = (
    "FROM workflow_run_decisions d WHERE d.request_id = %(request_id)s"
    " AND d.kind = 'approval' AND d.outcome = 'approved' AND d.input_hash = %(input_hash)s"
)
#: A parked call at hop parent that is an approval, and whose hop has not timed out by
#: Postgres's clock (#3411); a malformed ``hop_until`` is that call skipped.
_PARENT_APPROVAL_LIVE = (
    "w->>'hop' = 'parent' AND w->>'kind' = 'approval'"
    " AND CASE WHEN pg_input_is_valid(w->>'hop_until', 'timestamptz')"
    " THEN (w->>'hop_until')::timestamptz > now() ELSE false END"
)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _objects(raw: Any) -> list[dict[str, Any]]:
    """The JSON objects of a list; none when ``raw`` is not a list. Anything else in it is
    no entry (the route checks each object's fields, `api/crud_agent.py` `_kept`)."""
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def waiting_entry(raw: Any) -> dict[str, Any]:
    """A parked call's fields the agent reads."""
    entry = {name: raw.get(name) for name in WAITING_FIELDS}
    entry["proposed"] = raw.get("proposed") is True
    hops = raw.get("hops")
    entry["hops"] = [{name: h.get(name) for name in HOP_FIELDS} for h in _objects(hops)]
    return entry


def steps_of(raw: Any) -> list[dict[str, Any]]:
    """A run's steps, the fields the agent reads; none when ``steps`` is not a list."""
    return [{name: s.get(name) for name in STEP_FIELDS} for s in _objects(raw)]


def run_starter(pool: Pool, run_id: str, execution: str) -> dict[str, Any] | None:
    """Who started run ``run_id``, whether ``execution`` is its open execution, and
    whether the row has not recorded it (a Reset not committed yet). Under the row's
    share lock: a Reset of a closed run holds the row FOR UPDATE while it starts the new
    execution (`flows/store.py` `_reset_run`), so this waits for it."""
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "SELECT started_by,"
            " status IN ('running', 'waiting') AND NOT (%(e)s = ANY(superseded_run_ids)) AS open,"
            " workflow_run_id IS DISTINCT FROM %(e)s"
            " AND NOT (%(e)s = ANY(superseded_run_ids)) AS unrecorded"
            " FROM workflow_runs WHERE id = %(id)s FOR SHARE",
            {"e": execution, "id": run_id},
        ).fetchone()
    if row is None:
        return None
    by = row["started_by"] if isinstance(row["started_by"], dict) else {}
    return {
        "open": bool(row["open"]),
        "unrecorded": bool(row["unrecorded"]),
        "started_by": {name: _text(by.get(name)) for name in ("kind", "principal", "session")},
    }


def decision_checks(pool: Pool, request_id: str, input_hash: str) -> dict[str, bool]:
    """Whether the call's approval with this hash holds (a run grant's only while the
    grant is in force), holds without having seen what the call resolves to (a grant's
    or a narrowing's, #4445), was a person's in the browser (#959), or was a grant's
    that has lapsed (#2481)."""
    params = {"request_id": request_id, "input_hash": input_hash}
    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT EXISTS (SELECT 1 {_APPROVED}"
            f" AND (d.responder NOT LIKE 'grant:%%' OR {_GRANT_IN_FORCE})) AS approved,"
            f" EXISTS (SELECT 1 {_APPROVED}"
            " AND (d.responder LIKE 'grant:%%' OR d.response ? 'narrowed_by')"
            f" AND (d.responder NOT LIKE 'grant:%%' OR {_GRANT_IN_FORCE})) AS approved_unresolved,"
            f" EXISTS (SELECT 1 {_APPROVED} AND d.responder = 'browser') AS by_person,"
            " EXISTS (SELECT 1 FROM workflow_run_decisions d"
            " JOIN flow_grants g ON 'grant:' || g.id = d.responder"
            " WHERE d.request_id = %(request_id)s AND d.kind = 'approval'"
            " AND d.outcome = 'approved' AND d.input_hash = %(input_hash)s"
            " AND (g.status <> 'approved' OR g.revoked_at IS NOT NULL OR g.expires_at IS NULL"
            " OR now() >= g.expires_at)) AS lapsed",
            params,
        ).fetchone()
    assert row is not None
    return {name: bool(row[name]) for name in DECISION_CHECKS}


def grant_in_force(pool: Pool, grant_id: str) -> bool:
    """Whether run grant ``grant_id`` is approved, not revoked and not expired."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM flow_grants WHERE id = %s AND status = 'approved'"
            " AND revoked_at IS NULL AND now() < expires_at",
            (grant_id,),
        ).fetchone()
    return row is not None


def _run_fields(row: DictRow) -> dict[str, Any]:
    by = row["started_by"] if isinstance(row["started_by"], dict) else {}
    waiting = row["waiting_on"] if isinstance(row["waiting_on"], list) else []
    return {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "workflow_run_id": row["workflow_run_id"],
        "starter_session": _text(by.get("session")),
        "waiting_on": [waiting_entry(w) for w in _objects(waiting)],
        "steps": steps_of(row["steps"]),
    }


def run_queue(pool: Pool, run_id: str) -> dict[str, Any] | None:
    """Run ``run_id``'s own parked calls and steps, and its grant while it is requested."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, name, status, workflow_run_id, started_by, waiting_on, steps"
            " FROM workflow_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        grant = conn.execute(
            "SELECT id, created_at FROM flow_grants WHERE run_id = %s AND status = 'requested'",
            (run_id,),
        ).fetchone()
    return {
        **_run_fields(row),
        "requested_grant": None
        if grant is None
        else {"id": grant["id"], "created_at": grant["created_at"]},
    }


def waiting_runs(pool: Pool) -> list[dict[str, Any]]:
    """The open runs with parked calls, the `WAITING_RUNS_MAX` most recently moved first."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, name, status, workflow_run_id, started_by, waiting_on, steps, updated_at"
            " FROM workflow_runs WHERE status IN ('running', 'waiting')"
            " AND jsonb_array_length(waiting_on) > 0 ORDER BY updated_at DESC LIMIT %s",
            (WAITING_RUNS_MAX,),
        ).fetchall()
    return [{**_run_fields(row), "updated_at": row["updated_at"]} for row in rows]


def run_steps(pool: Pool, run_id: str) -> list[dict[str, Any]]:
    """Run ``run_id``'s steps; none for no such run."""
    with pool.connection() as conn:
        row = conn.execute("SELECT steps FROM workflow_runs WHERE id = %s", (run_id,)).fetchone()
    return [] if row is None else steps_of(row["steps"])


def parent_hop_approvals(
    pool: Pool, after: tuple[datetime, str] | None, limit: int
) -> list[dict[str, Any]]:
    """The waiting runs with a parked approval at hop ``parent`` whose hop is live, oldest
    move first, ``limit`` at most after ``after`` (``updated_at``, ``id``); each with
    those calls only."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, workflow_run_id, started_by, steps, updated_at,"
            " (SELECT coalesce(jsonb_agg(w), '[]'::jsonb) FROM jsonb_array_elements(r.waiting_on) w"
            f"   WHERE {_PARENT_APPROVAL_LIVE}) AS calls"
            " FROM workflow_runs r"
            " WHERE status = 'waiting' AND waiting_on @> '[{\"hop\": \"parent\"}]'::jsonb"
            " AND EXISTS (SELECT 1 FROM jsonb_array_elements(r.waiting_on) w"
            f"   WHERE {_PARENT_APPROVAL_LIVE})"
            " AND (%(after)s::timestamptz IS NULL OR (updated_at, id) > (%(after)s, %(after_id)s))"
            " ORDER BY updated_at, id LIMIT %(limit)s",
            {
                "after": None if after is None else after[0],
                "after_id": None if after is None else after[1],
                "limit": limit,
            },
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        by = row["started_by"] if isinstance(row["started_by"], dict) else {}
        out.append(
            {
                "id": row["id"],
                "workflow_run_id": row["workflow_run_id"],
                "session": _text(by.get("session")),
                "principal": _text(by.get("principal")),
                "calls": [waiting_entry(w) for w in _objects(row["calls"])],
                "steps": steps_of(row["steps"]),
                "updated_at": row["updated_at"],
            }
        )
    return out


def still_at_parent_hop(pool: Pool, request_ids: list[str]) -> list[str]:
    """Which of ``request_ids`` (``flow:<run>:<execution>:<call>``) still wait at hop
    ``parent`` of a waiting run, in the order asked."""
    if not request_ids:
        return []
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT x.id FROM unnest(%s::text[]) WITH ORDINALITY x(id, n) WHERE EXISTS ("
            " SELECT 1 FROM workflow_runs r CROSS JOIN LATERAL jsonb_array_elements(r.waiting_on) w"
            " WHERE r.id = split_part(x.id, ':', 2) AND r.status = 'waiting'"
            " AND w->>'hop' = 'parent'"
            " AND x.id = 'flow:' || r.id || ':' || r.workflow_run_id || ':' || (w->>'call_id'))"
            " ORDER BY x.n",
            (request_ids,),
        ).fetchall()
    return [row["id"] for row in rows]
