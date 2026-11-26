"""A session's panel-protocol event log (#5299 step 9, part b): the agent's
``sessions/eventLog.ts`` statement for statement. The ``session-events`` routes are Go's
(``tools/go/core/internal/events``, #7667); here are the statements a forget runs to copy a
fork's range (``copy_fork_events``, ``copy_fork_blobs``), and the append and read its tests use.

Order without gaps: ``seq`` comes from ``ai_sessions.event_seq``, bumped in the statement that
inserts the events, so two writers of one session commit in seq order. A fork reads its parent's
conversation events through its chain (``fork_parent_id``, ``fork_event_cut``), never past the
least cut below each ancestor (``LEAST(bound, cut)``), in one statement: one snapshot."""

from __future__ import annotations

import json
from typing import Any

from psycopg import Connection

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


def copy_fork_events(conn: Connection[Any], session_id: str) -> list[str]:
    """``copyForkEvents``: before the parent of ``session_id`` goes (the forget's transaction),
    the events it reads through its chain become rows of its own, at the same seqs, re-addressed
    to it. The caller clears its link. The events' JSON texts, so their images can be copied."""
    rows = conn.execute(
        (
            _CHAIN + " SELECT e.seq, e.event FROM chain c"
            " JOIN ai_session_events e ON e.session_id = c.id"
            " WHERE c.depth > 0 AND e.seq <= c.bound"
            " AND e.event::jsonb->>'type' = ANY(%(types)s) ORDER BY e.seq"
        ).encode(),
        {"session": session_id, "types": FORK_EVENT_TYPES},
    ).fetchall()
    texts = []
    for row in rows:
        event = json.loads(row["event"])
        event["sessionId"] = session_id
        texts.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    for at in range(0, len(rows), 500):
        conn.execute(
            "INSERT INTO ai_session_events (session_id, seq, event)"
            " SELECT %s::uuid, e.seq, e.event"
            " FROM unnest(%s::bigint[], %s::text[]) AS e(seq, event)",
            (
                session_id,
                [int(r["seq"]) for r in rows[at : at + 500]],
                texts[at : at + 500],
            ),
        )
    return texts


def blob_names(texts: list[str]) -> list[str]:
    """``forkPoint.ts`` ``blobNames``: the images the events name."""
    names: dict[str, None] = {}
    for text in texts:
        event = json.loads(text)
        if event.get("type") == "tool.result":
            for image in event.get("images") or []:
                names[image["name"]] = None
        if event.get("type") == "user.turn":
            for image in event.get("images") or []:
                if image.get("name") is not None:
                    names[image["name"]] = None
                names[image["preview"]["name"]] = None
    return list(names)


def copy_fork_blobs(conn: Connection[Any], session_id: str, names: list[str]) -> None:
    """``blobs.ts`` ``copyForkBlobs``: the images ``names`` lists, found through the chain,
    become rows of the session's own; a new one keeps its ``created_at``, one the session
    already has is stamped now (#6413)."""
    if not names:
        return
    conn.execute(
        (
            _CHAIN + " INSERT INTO ai_session_blobs (session_id, name, media_type, data,"
            " created_at) SELECT DISTINCT ON (b.name) %(session)s::uuid, b.name, b.media_type,"
            " b.data, b.created_at FROM chain c JOIN ai_session_blobs b ON b.session_id = c.id"
            " WHERE c.depth > 0 AND b.name = ANY(%(names)s) ORDER BY b.name, c.depth"
            # An image the fork already holds is written again now: a queued message's images
            # are deleted only up to its own write's time (#6413), so every write sets it.
            " ON CONFLICT (session_id, name) DO UPDATE SET created_at = clock_timestamp()"
        ).encode(),
        {"session": session_id, "names": names},
    )
