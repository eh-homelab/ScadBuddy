"""A session's panel-protocol event log behind CRUD routes (#5299 step 9, part b): the
agent's ``sessions/eventLog.ts`` statement for statement, for ``api/crud_agent_events.py``
(area ``session-events``).

Order without gaps: ``seq`` comes from ``ai_sessions.event_seq``, bumped in the statement that
inserts the events, so two writers of one session commit in seq order. A fork reads its parent's
conversation events through its chain (``fork_parent_id``, ``fork_event_cut``), never past the
least cut below each ancestor (``LEAST(bound, cut)``), in one statement: one snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from psycopg import Connection

from scadbuddy.workflows.payload_codec import payload_key_lock

#: The conversation events a fork takes (``eventLog.ts`` ``FORK_EVENT_TYPES``); lifecycle
#: events (status, owner, result) are the parent's own.
FORK_EVENT_TYPES = [
    "user.turn",
    "assistant.text.delta",
    "assistant.text.done",
    "tool.call",
    "tool.result",
]


class SessionGoneError(LookupError):
    """The session has no row: nothing is appended."""


class ParentForgottenError(Exception):
    """A new fork's parent was forgotten since the fork's insert: the fork has no log."""


@dataclass(frozen=True)
class TurnFence:
    """The placement that writes a turn's events (``turnHost.ts`` ``holds(tx, 'update')``):
    the row still names it, checked ``FOR UPDATE`` in the append's transaction."""

    attempt: str
    turn_id: str


_APPEND = (
    "WITH s AS ("
    " UPDATE ai_sessions SET event_seq = event_seq + %(n)s, updated_at = now()"
    " WHERE id = %(session)s::uuid RETURNING event_seq - %(n)s AS base)"
    " INSERT INTO ai_session_events (session_id, seq, event)"
    " SELECT %(session)s::uuid, s.base + e.ord, e.event"
    " FROM s, unnest(%(events)s::text[]) WITH ORDINALITY AS e(event, ord)"
    " RETURNING seq"
)


def append_in(conn: Connection[Any], session_id: str, texts: list[str]) -> list[int]:
    """``appendEventsIn``: the events appended in the caller's transaction; their seqs."""
    if not texts:
        return []
    rows = conn.execute(
        _APPEND.encode(), {"n": len(texts), "session": session_id, "events": texts}
    ).fetchall()
    if len(rows) != len(texts):
        raise SessionGoneError(f"session {session_id} does not exist")
    return sorted(int(r["seq"]) for r in rows)


def append(
    conn: Connection[Any], session_id: str, texts: list[str], fence: TurnFence | None
) -> list[int] | None:
    """``EventLog.append``, or with ``fence`` turnHost's ``appendEvents``: in one transaction,
    only while the row names the placement (None when it does not)."""
    with conn.transaction():
        if fence is not None:
            held = conn.execute(
                "SELECT 1 FROM ai_sessions WHERE id = %s::uuid AND turn_attempt = %s"
                " AND durable_turn_id::text = %s FOR UPDATE",
                (session_id, fence.attempt, fence.turn_id),
            ).fetchone()
            if held is None:
                return None
        return append_in(conn, session_id, texts)


_CHAIN = (
    "WITH RECURSIVE chain (id, parent, cut, bound, depth) AS ("
    " SELECT id, fork_parent_id, fork_event_cut, NULL::bigint, 0 FROM ai_sessions"
    " WHERE id = %(session)s::uuid"
    " UNION ALL"
    " SELECT s.id, s.fork_parent_id, s.fork_event_cut, LEAST(c.bound, c.cut), c.depth + 1"
    " FROM ai_sessions s JOIN chain c ON s.id = c.parent WHERE c.cut IS NOT NULL)"
)


def read(
    conn: Connection[Any], session_id: str, after_seq: int, limit: int
) -> list[dict[str, Any]]:
    """``EventLog.read``: the rows after ``after_seq`` by seq, its own and those it takes through
    its chain: ``{seq, event, inherited}``, the event's JSON text as it was logged (an inherited
    one the agent re-addresses to the session, as ``chainRows`` does)."""
    rows = conn.execute(
        (
            _CHAIN + " SELECT e.seq, e.event, c.depth FROM chain c"
            " JOIN ai_session_events e ON e.session_id = c.id WHERE e.seq > %(after)s"
            " AND (c.depth = 0 OR (c.depth > 0 AND e.seq <= c.bound"
            " AND e.event::jsonb->>'type' = ANY(%(types)s)))"
            " ORDER BY e.seq LIMIT %(limit)s"
        ).encode(),
        {"session": session_id, "after": after_seq, "limit": limit, "types": FORK_EVENT_TYPES},
    ).fetchall()
    return [{"seq": int(r["seq"]), "event": r["event"], "inherited": r["depth"] > 0} for r in rows]


_USER_TURN = (
    "session_id = %(session)s::uuid AND event::jsonb->>'type' = 'user.turn'"
    " AND event::jsonb->>'turnId' = %(turn)s"
)


def user_turn_text(conn: Connection[Any], session_id: str, turn_id: str) -> str | None:
    """``EventLog.userTurnText``: the words of the last ``user.turn`` logged with ``turn_id``."""
    row = conn.execute(
        (
            "SELECT event FROM ai_session_events WHERE " + _USER_TURN + " ORDER BY seq DESC LIMIT 1"
        ).encode(),
        {"session": session_id, "turn": turn_id},
    ).fetchone()
    if row is None:
        return None
    turn = json.loads(row["event"])
    text = turn.get("text") if isinstance(turn, dict) and turn.get("type") == "user.turn" else None
    return text if isinstance(text, str) else None


def last_seq(conn: Connection[Any], session_id: str) -> int:
    """``EventLog.lastSeq``: the last seq handed out, 0 for a session with none."""
    row = conn.execute(
        "SELECT event_seq FROM ai_sessions WHERE id = %s::uuid", (session_id,)
    ).fetchone()
    return int(row["event_seq"]) if row is not None else 0


def turn_events(
    conn: Connection[Any], session_id: str, turn_id: str, after: int | None
) -> tuple[bool, int | None, list[str]]:
    """``flowSessions.ts`` ``turnReader``: whether the session stands, and its own events from the
    turn's ``user.turn`` on (``after`` None: find that start first; none yet is ``(True, None,
    [])``), past ``after``. One statement each, so one snapshot each."""
    here_sql = "EXISTS (SELECT 1 FROM ai_sessions WHERE id = %(session)s::uuid) AS here"
    params = {"session": session_id, "turn": turn_id}
    if after is None:
        start = conn.execute(
            (
                "SELECT " + here_sql + ", e.seq FROM (SELECT 1) AS one"
                " LEFT JOIN ai_session_events e ON e.session_id = %(session)s::uuid"
                " AND e.event::jsonb->>'type' = 'user.turn'"
                " AND e.event::jsonb->>'turnId' = %(turn)s"
                " ORDER BY e.seq DESC NULLS LAST LIMIT 1"
            ).encode(),
            params,
        ).fetchone()
        if start is None or not start["here"]:
            return False, None, []
        if start["seq"] is None:
            return True, None, []
        after = int(start["seq"])
    rows = conn.execute(
        (
            "SELECT " + here_sql + ", e.seq, e.event FROM (SELECT 1) AS one LEFT JOIN LATERAL ("
            " SELECT seq, event FROM ai_session_events WHERE session_id = %(session)s::uuid"
            " AND seq > %(after)s ORDER BY seq) e ON true"
        ).encode(),
        {**params, "after": after},
    ).fetchall()
    if not rows or not rows[0]["here"]:
        return False, None, []
    events = []
    for row in rows:
        if row["seq"] is None or row["event"] is None:
            continue
        events.append(row["event"])
        after = int(row["seq"])
    return True, after, events


def fork_log(
    conn: Connection[Any],
    child_id: str,
    parent_id: str,
    cut: int,
    started: list[str],
    then: list[str],
) -> list[int]:
    """``manager.ts`` ``fork``'s log: under the parent's payload-key lock, shared, as the fork's
    insert was (a forget of the parent since has cleared the child's link), the child's own
    start (its seq 1), the parent's log taken through ``cut`` (``inheritThrough``), then its own
    status, in one transaction. Their seqs."""
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
            (payload_key_lock(f"session-{parent_id}"),),
        )
        linked = conn.execute(
            "SELECT 1 FROM ai_sessions WHERE id = %s::uuid AND fork_parent_id = %s::uuid",
            (child_id, parent_id),
        ).fetchone()
        if linked is None:
            raise ParentForgottenError(f"session {parent_id} was forgotten while it was forked")
        first = append_in(conn, child_id, started)
        conn.execute(
            "UPDATE ai_sessions SET fork_event_cut = %s, event_seq = GREATEST(event_seq, %s)"
            " WHERE id = %s::uuid",
            (cut, cut, child_id),
        )
        return first + append_in(conn, child_id, then)
