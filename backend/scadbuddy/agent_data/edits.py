"""An edit of a session behind a CRUD route (#5299 step 9, #6449), for ``api/crud_agent_edits.py``
(area ``session-edits``): a rename, marking it done and archiving it, the agent's
``sessions/edits.ts`` ``PgEditRows.edit`` transaction statement for statement.

Under the row's lock, taken only while ``principal`` owns it: a done refused while a turn holds
the row, an archive refused while a turn holds it or anything is parked on the person (an
approval, a question, an attention request, a durable entry); the row written; the warm process
evicted on commit (``scadbuddy_warm_evict``); an undismissed ``done`` summary dismissed with an
archive; and their events, in one transaction.

The session's workflow is told it is done by the agent (Temporal is the agent's), so a done that
this transaction would newly write needs ``told``: without it nothing is written, never a done the
workflow was not told of. ``untold`` is the last check, so it means every other check passed: the
agent sends the edit untold first and tells the workflow only on that answer."""

from __future__ import annotations

from typing import Any, Literal

from psycopg import Connection

from scadbuddy.agent_data import access, gate, turns

#: Why a done summary dismissed by an archive ended (the agent's ``ARCHIVED_REASON``).
ARCHIVED_REASON = "the chat was archived"
#: The agent's ``isDoneSummary``.
_DONE = "(q.attention_reason IS NOT DISTINCT FROM 'done' AND q.expires_at IS NULL)"

Refusal = Literal["gone", "changed_owner", "busy_done", "busy_archive", "parked", "untold"]


class EditRefusedError(Exception):
    def __init__(self, why: Refusal) -> None:
        super().__init__(why)
        self.why = why


def edit(
    conn: Connection[Any],
    session_id: str,
    principal: tuple[str, str],
    title: str | None,
    done: bool,
    archived: bool | None,
    told: bool,
) -> tuple[list[gate.Logged], list[dict[str, Any]]]:
    """The edit in one transaction: what it logged, and the summaries an archive dismissed."""
    archive = archived is True
    unarchive = archived is False
    logged: list[gate.Logged] = []
    with turns.write_transaction(conn):
        before = conn.execute(
            (
                "SELECT status AS was, archived_at IS NOT NULL AS archived,"
                " status IN ('running', 'waiting_approval', 'waiting_input') AS busy,"
                " (EXISTS (SELECT 1 FROM ai_approvals WHERE session_id = %(id)s"
                " AND decision IS NULL)"
                " OR EXISTS (SELECT 1 FROM ai_questions q WHERE q.session_id = %(id)s"
                f" AND q.outcome IS NULL AND NOT {_DONE})"
                " OR EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = %(id)s)) AS parked"
                " FROM ai_sessions"
                " WHERE id = %(id)s AND owner_kind = %(kind)s"
                " AND (owner_id = %(owner)s OR owner_kind = 'browser')"
                " FOR UPDATE"
            ).encode(),
            {"id": session_id, "kind": principal[0], "owner": principal[1]},
        ).fetchone()
        if before is None:
            here = conn.execute("SELECT 1 FROM ai_sessions WHERE id = %s", (session_id,)).fetchone()
            raise EditRefusedError("gone" if here is None else "changed_owner")
        if done and before["busy"]:
            raise EditRefusedError("busy_done")
        if archive and not before["archived"]:
            if before["busy"]:
                raise EditRefusedError("busy_archive")
            if before["parked"]:
                raise EditRefusedError("parked")
        newly_done = done and before["was"] != "done"
        # Last, after every other refusal: `untold` tells the agent the edit would pass, so it tells
        # the workflow then and sends the edit again. Never a done the workflow was not told of.
        if newly_done and not told:
            raise EditRefusedError("untold")
        conn.execute(
            "UPDATE ai_sessions SET title = coalesce(%(title)s::text, title),"
            " status = CASE WHEN %(done)s THEN 'done' ELSE status END,"
            " archived_at = CASE WHEN %(archive)s THEN coalesce(archived_at, now())"
            " WHEN %(unarchive)s THEN NULL ELSE archived_at END,"
            " updated_at = now() WHERE id = %(id)s",
            {
                "title": title,
                "done": done,
                "archive": archive,
                "unarchive": unarchive,
                "id": session_id,
            },
        )
        # Its warm process on every replica goes with it: sent on commit.
        if newly_done or (archive and not before["archived"]):
            access.notify_warm_evict(conn, session_id)
        events: list[dict[str, Any]] = []
        if newly_done:
            events.append(
                gate._event({"type": "session.status", "sessionId": session_id, "status": "done"})
            )
        gone: list[dict[str, Any]] = []
        if archive:
            gone = conn.execute(
                (
                    "UPDATE ai_questions q SET outcome = 'cancelled', reason = %s,"
                    " resolved_at = now()"
                    f" WHERE q.session_id = %s AND q.outcome IS NULL AND {_DONE}"
                    " RETURNING q.id::text AS id, q.turn_id::text AS turn_id, q.tool,"
                    " q.tool_use_id, q.created_at"
                ).encode(),
                (ARCHIVED_REASON, session_id),
            ).fetchall()
            for r in gone:
                events.append(
                    gate._event(
                        {
                            "type": "question.resolved",
                            "sessionId": session_id,
                            "id": r["id"],
                            "answered": False,
                            "reason": ARCHIVED_REASON,
                        }
                    )
                )
                events.append(
                    gate._event(
                        {
                            "type": "input.resolved",
                            "sessionId": session_id,
                            "id": f"question:{r['id']}",
                            "kind": "answer",
                            "outcome": "cancelled",
                            "reason": ARCHIVED_REASON,
                        }
                    )
                )
        gate._append(conn, logged, session_id, events)
    return logged, list(gone)
