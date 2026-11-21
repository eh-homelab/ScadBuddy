"""A flow run's calls at the starting session's hop (#5299 step 10), for
``api/crud_agent_escalations.py`` area ``escalations``: the SQL of the agent's
``flows/escalations.ts`` `Escalations` and ``flows/queue.ts`` `FlowQueue.forRun`'s read of a run's
child entries, statement for statement, on ``ai_pending_input`` and ``ai_escalation_deliveries``.

Who may answer a call is still the agent's check (the caller's session is the entry's parent,
owned by the run's starter); every write of an answer or a delivery (pass up, a deny's claim and
release, a delivery's claim, hand back and skip) names the parent session again in its WHERE, so a
caller that names another session moves nothing. The hop's timeout sweep and the record of a run's
own calls sent are the agent's own housekeeping and name no session. A hop only moves up: a
parent's pass up, the sweep's timeout and a skip set hop ``person``; a deny's claim holds the
entry at ``infinity`` until its release or its lease ends.

A delivery's claim is committed before its turn starts (over HTTP it cannot share the turn's
transaction, as the agent's SQL did): the agent hands the claim back (``unclaim``) when the turn
does not start. A process that dies between the two leaves the calls marked sent; each still goes
to the person when its hop ends, so nothing is approved or lost, and only the parent's turn is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from scadbuddy.agent_data import gate

#: How long a deny's claim may hold its entry before the sweep moves it (``CLAIM_LEASE_S``).
CLAIM_LEASE_S = 300
#: The most entries one delivery pass reads (the agent's ``LIMIT 200``).
WAITING_LIMIT = 200


def step(action: str, actor: str, reason: str | None = None) -> dict[str, str]:
    """One step of an entry's way up (``hops``), as the agent's ``step`` writes it."""
    now = datetime.now(UTC)
    at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    out = {"hop": "parent", "action": action, "actor": actor, "at": at}
    if reason:
        out["reason"] = reason
    return out


def entry(conn: Connection[Any], request_id: str) -> dict[str, Any] | None:
    row: dict[str, Any] | None = conn.execute(
        "SELECT session_id::text AS session_id, hop, coalesce(hop_until <= now(), false) AS due,"
        " coalesce(hop_until = 'infinity', false) AS claimed,"
        " parent_session::text AS parent_session, parent_principal"
        " FROM ai_pending_input WHERE request_id = %s AND flow_run_id IS NOT NULL",
        (request_id,),
    ).fetchone()
    return row


def pass_up(conn: Connection[Any], request_id: str, session: str, reason: str | None) -> bool:
    if not gate._is_uuid(session):
        return False
    rows = conn.execute(
        "UPDATE ai_pending_input SET hop = 'person', hop_until = NULL, hops = hops || %s"
        " WHERE request_id = %s AND hop = 'parent' AND parent_session = %s::uuid"
        " AND hop_until > now() AND hop_until <> 'infinity' RETURNING request_id",
        (Jsonb([step("pass_up", f"session:{session}", reason)]), request_id, session),
    ).fetchall()
    return len(rows) > 0


def claim_deny(conn: Connection[Any], request_id: str, session: str) -> datetime | None:
    """The entry held for a deny (``hop_until = 'infinity'``): its deadline before, or None."""
    if not gate._is_uuid(session):
        return None
    with conn.transaction():
        row = conn.execute(
            "UPDATE ai_pending_input p SET hop_until = 'infinity', hop_claimed_at = now()"
            " FROM (SELECT request_id, hop_until FROM ai_pending_input WHERE request_id = %s"
            " FOR UPDATE) old"
            " WHERE p.request_id = old.request_id AND p.hop = 'parent'"
            " AND p.parent_session = %s::uuid"
            " AND p.hop_until > now() AND p.hop_until <> 'infinity'"
            " RETURNING old.hop_until",
            (request_id, session),
        ).fetchone()
    return None if row is None else row["hop_until"]


def release_deny(conn: Connection[Any], request_id: str, session: str, hop_until: datetime) -> bool:
    """The deadline put back, only on a row of ``session`` that still holds a deny's claim.

    The deadline is never past the entry's ``expires_at``, so a release cannot park an entry. One
    already past is kept: the claim may have outlived the deadline it took, and the sweep then
    moves the entry to the person, as it would have without the claim.
    """
    if not gate._is_uuid(session):
        return False
    rows = conn.execute(
        "UPDATE ai_pending_input SET hop_until = LEAST(%s, expires_at), hop_claimed_at = NULL"
        " WHERE request_id = %s AND hop = 'parent' AND parent_session = %s::uuid"
        " AND hop_until = 'infinity' RETURNING request_id",
        (hop_until, request_id, session),
    ).fetchall()
    return len(rows) > 0


def move_due(conn: Connection[Any]) -> int:
    rows = conn.execute(
        "UPDATE ai_pending_input SET hop = 'person', hop_until = NULL, hop_claimed_at = NULL,"
        " hops = hops || %s"
        " WHERE hop = 'parent' AND (hop_until <= now() OR (hop_until = 'infinity'"
        " AND hop_claimed_at <= now() - make_interval(secs => %s)))"
        " RETURNING request_id",
        (Jsonb([step("timeout", "system")]), CLAIM_LEASE_S),
    ).fetchall()
    return len(rows)


def waiting(conn: Connection[Any], grace_s: float) -> list[dict[str, Any]]:
    """The children's calls at hop ``parent`` not yet sent, each session's oldest first."""
    return conn.execute(
        "SELECT request_id, flow_run_id, tool, summary, outside_reason, hop_until,"
        " created_at < now() - make_interval(secs => %s) AS past_grace,"
        " session_id::text AS session_id, parent_session::text AS parent_session,"
        " parent_principal FROM ("
        " SELECT *, row_number() OVER (PARTITION BY parent_session ORDER BY created_at) AS nth"
        " FROM ai_pending_input WHERE hop = 'parent' AND hop_until > now()"
        " AND hop_until <> 'infinity' AND delivered_at IS NULL) waiting"
        " ORDER BY nth, created_at LIMIT %s",
        (grace_s, WAITING_LIMIT),
    ).fetchall()


def deliveries_page(conn: Connection[Any], after: str, limit: int) -> list[str]:
    rows = conn.execute(
        "SELECT request_id FROM ai_escalation_deliveries WHERE request_id > %s"
        " ORDER BY request_id LIMIT %s",
        (after, limit),
    ).fetchall()
    return [r["request_id"] for r in rows]


def forget_deliveries(conn: Connection[Any], ids: list[str]) -> None:
    conn.execute("DELETE FROM ai_escalation_deliveries WHERE request_id = ANY(%s)", (ids,))


def sent(conn: Connection[Any], ids: list[str]) -> list[str]:
    rows = conn.execute(
        "SELECT request_id FROM ai_escalation_deliveries WHERE request_id = ANY(%s)", (ids,)
    ).fetchall()
    return [r["request_id"] for r in rows]


def claim(conn: Connection[Any], session: str, child: list[str], own: list[str]) -> list[str]:
    """The calls marked sent, once across replicas; those this call claimed. Rows another
    replica's open claim holds are skipped, not waited on."""
    if not gate._is_uuid(session):
        return []
    won: list[str] = []
    with conn.transaction():
        if child:
            won += [
                r["request_id"]
                for r in conn.execute(
                    "UPDATE ai_pending_input SET delivered_at = now() WHERE request_id IN ("
                    " SELECT request_id FROM ai_pending_input WHERE request_id = ANY(%s)"
                    " AND delivered_at IS NULL AND hop = 'parent' AND parent_session = %s::uuid"
                    " FOR UPDATE SKIP LOCKED) RETURNING request_id",
                    (child, session),
                ).fetchall()
            ]
        for request_id in own:
            row = conn.execute(
                "INSERT INTO ai_escalation_deliveries (request_id, session_id) VALUES (%s, %s)"
                " ON CONFLICT (request_id) DO NOTHING RETURNING request_id",
                (request_id, session),
            ).fetchone()
            if row is not None:
                won.append(row["request_id"])
    return won


def unclaim(conn: Connection[Any], session: str, child: list[str], own: list[str]) -> None:
    """A claim whose turn did not start, handed back so a later tick sends it."""
    if not gate._is_uuid(session):
        return
    with conn.transaction():
        if child:
            conn.execute(
                "UPDATE ai_pending_input SET delivered_at = NULL WHERE request_id = ANY(%s)"
                " AND hop = 'parent' AND parent_session = %s::uuid",
                (child, session),
            )
        if own:
            conn.execute(
                "DELETE FROM ai_escalation_deliveries WHERE request_id = ANY(%s)"
                " AND session_id = %s::uuid",
                (own, session),
            )


def skip(conn: Connection[Any], session: str, child: list[str], own: list[str]) -> None:
    """The starter session cannot be asked: a child's entries go to the person now, and a run's
    own calls are marked sent for ``session``."""
    if not gate._is_uuid(session):
        return
    with conn.transaction():
        if child:
            reason = "the starting session cannot be asked"
            conn.execute(
                "UPDATE ai_pending_input SET hop = 'person', hop_until = NULL, hops = hops || %s"
                " WHERE request_id = ANY(%s) AND hop = 'parent' AND parent_session = %s::uuid"
                " AND hop_until <> 'infinity'",
                (Jsonb([step("pass_up", "system", reason)]), child, session),
            )
        for request_id in own:
            conn.execute(
                "INSERT INTO ai_escalation_deliveries (request_id, session_id) VALUES (%s, %s)"
                " ON CONFLICT (request_id) DO NOTHING",
                (request_id, session),
            )


def run_entries(conn: Connection[Any], flow_run_id: str) -> list[dict[str, Any]]:
    """A run's children's entries (``FlowQueue.forRun``). ``hop_until`` as JSON's text of it, so
    a deny's claim reads ``infinity``, which no ``datetime`` holds."""
    return conn.execute(
        "SELECT request_id, kind, session_id::text AS session_id, tool, summary, input_hash, hop,"
        " to_json(hop_until) #>> '{}' AS hop_until, outside_reason, hops, created_at, expires_at,"
        " (expires_at <= now()) AS expiring"
        " FROM ai_pending_input WHERE flow_run_id = %s ORDER BY created_at, request_id",
        (flow_run_id,),
    ).fetchall()
