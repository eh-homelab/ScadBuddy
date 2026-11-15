"""A session's background tasks, a Monitor's events and a Workflow's journal (#5299 step 8),
for ``api/crud_agent_tasks.py`` area ``background-tasks``: ``ai_background_tasks``,
``ai_background_events``, ``ai_workflow_calls`` and ``ai_workflow_children``.

Each function is the SQL of ``agent/src/background/store.ts`` `BackgroundTaskStore`,
statement for statement, as one transaction. ``create`` takes the session's transaction
advisory lock (``bgtask:<session>``) before it counts the active tasks, so two creates
cannot both pass ``MAX_ACTIVE``; an agent pod still on SQL takes the same lock.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

#: At most this many tasks active in one session at once (the agent's ``MAX_ACTIVE``).
MAX_ACTIVE = 10
#: A monitor that makes more events than this is stopped (the agent's ``MAX_EVENTS``).
MAX_EVENTS = 200
#: One recorded notice at most, its wrapper included: the column's cap.
NOTICE_MAX_CHARS = 16384
#: One delivery's events at most, by their text (the agent's ``DELIVER_MAX_CHARS``).
DELIVER_MAX_CHARS = 64_000

TASK_COLUMNS = (
    "id, session_id::text AS session_id, kind, description, spec, created_by, state, next_at,"
    " fired, last_fired_at, expires_at, detail, created_at, updated_at"
)


def create(
    conn: Connection[Any],
    *,
    session_id: str,
    kind: str,
    description: str,
    spec: dict[str, Any],
    created_by: dict[str, Any],
    next_at: datetime | None,
    expires_at: datetime | None,
) -> dict[str, Any] | None:
    """Inserts an active task, or None when the session already has ``MAX_ACTIVE``."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"bgtask:{session_id}",))
        row = conn.execute(
            "SELECT count(*)::int AS n FROM ai_background_tasks"
            " WHERE session_id = %s AND state = 'active'",
            (session_id,),
        ).fetchone()
        if row is not None and row["n"] >= MAX_ACTIVE:
            return None
        return conn.execute(
            "INSERT INTO ai_background_tasks"
            " (id, session_id, kind, description, spec, created_by, next_at, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {TASK_COLUMNS}",
            (
                secrets.token_hex(4),
                session_id,
                kind,
                description,
                Jsonb(spec),
                Jsonb(created_by),
                next_at,
                expires_at,
            ),
        ).fetchone()


def get(conn: Connection[Any], task_id: str) -> dict[str, Any] | None:
    return conn.execute(
        f"SELECT {TASK_COLUMNS} FROM ai_background_tasks WHERE id = %s", (task_id,)
    ).fetchone()


def list_tasks(conn: Connection[Any], session_id: str, *, all_states: bool) -> list[dict[str, Any]]:
    """A session's tasks, newest first; the active ones only unless ``all_states``."""
    active = "" if all_states else " AND state = 'active'"
    return conn.execute(
        f"SELECT {TASK_COLUMNS} FROM ai_background_tasks WHERE session_id = %s{active}"
        " ORDER BY created_at DESC, id",
        (session_id,),
    ).fetchall()


def active_recurring_crons(conn: Connection[Any]) -> list[dict[str, Any]]:
    """Every session's active recurring cron jobs, oldest first."""
    return conn.execute(
        f"SELECT {TASK_COLUMNS} FROM ai_background_tasks"
        " WHERE state = 'active' AND kind = 'cron'"
        " AND coalesce(spec->>'recurring', 'true') <> 'false'"
        " ORDER BY created_at, id"
    ).fetchall()


def scheduled(conn: Connection[Any], task_id: str, next_at: datetime) -> None:
    conn.execute(
        "UPDATE ai_background_tasks SET next_at = %s, updated_at = now()"
        " WHERE id = %s AND state = 'active'",
        (next_at, task_id),
    )


def fired(conn: Connection[Any], task_id: str, at: datetime) -> None:
    conn.execute(
        "UPDATE ai_background_tasks SET fired = fired + 1, last_fired_at = %s, next_at = NULL,"
        " updated_at = now() WHERE id = %s AND state = 'active'",
        (at, task_id),
    )


def finish(conn: Connection[Any], task_id: str, state: str, detail: str | None) -> bool:
    """Ends an active task; False when it had ended already."""
    return (
        conn.execute(
            "UPDATE ai_background_tasks SET state = %s, detail = %s, next_at = NULL,"
            " updated_at = now() WHERE id = %s AND state = 'active' RETURNING id",
            (state, detail, task_id),
        ).fetchone()
        is not None
    )


def set_spec(conn: Connection[Any], task_id: str, spec: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE ai_background_tasks SET spec = %s, updated_at = now() WHERE id = %s",
        (Jsonb(spec), task_id),
    )


def progress(conn: Connection[Any], task_id: str, detail: str) -> None:
    conn.execute(
        "UPDATE ai_background_tasks SET detail = %s, updated_at = now()"
        " WHERE id = %s AND state = 'active'",
        (detail, task_id),
    )


def append_event(conn: Connection[Any], task_id: str, text: str) -> bool:
    """Records one event of a monitor; False once it has ``MAX_EVENTS``."""
    return (
        conn.execute(
            "INSERT INTO ai_background_events (task_id, text) SELECT %(task)s, %(text)s"
            " WHERE (SELECT count(*) FROM ai_background_events WHERE task_id = %(task)s)"
            " < %(max)s RETURNING seq",
            {"task": task_id, "text": text[:NOTICE_MAX_CHARS], "max": MAX_EVENTS},
        ).fetchone()
        is not None
    )


def append_notice(conn: Connection[Any], task_id: str, text: str) -> None:
    """Records a monitor's last notice (its end), past ``MAX_EVENTS``."""
    conn.execute(
        "INSERT INTO ai_background_events (task_id, text) VALUES (%s, %s)",
        (task_id, text[:NOTICE_MAX_CHARS]),
    )


def pending_events(conn: Connection[Any], task_id: str, max_chars: int) -> list[dict[str, Any]]:
    """A task's undelivered events, oldest first, as many as fit in ``max_chars`` of text,
    and always the oldest one. Ordered by the bigint ``seq``, never its text (``"10"`` sorts
    before ``"9"``), so the last one is the newest: ``delivered`` marks up to it."""
    return conn.execute(
        "SELECT seq::text AS seq, text FROM ("
        " SELECT seq, text, sum(length(text)) OVER (ORDER BY seq) AS through,"
        " row_number() OVER (ORDER BY seq) AS n"
        " FROM ai_background_events WHERE task_id = %s AND delivered_at IS NULL"
        ") pending WHERE n = 1 OR through <= %s ORDER BY pending.seq",
        (task_id, max_chars),
    ).fetchall()


def event_count(conn: Connection[Any], task_id: str, *, pending: bool) -> int:
    """How many events a task has; only those not yet sent to its session with ``pending``."""
    undelivered = " AND delivered_at IS NULL" if pending else ""
    row = conn.execute(
        f"SELECT count(*)::int AS n FROM ai_background_events WHERE task_id = %s{undelivered}",
        (task_id,),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def delivered(conn: Connection[Any], task_id: str, up_to: int) -> None:
    conn.execute(
        "UPDATE ai_background_events SET delivered_at = now()"
        " WHERE task_id = %s AND seq <= %s AND delivered_at IS NULL",
        (task_id, up_to),
    )


def journalled(
    conn: Connection[Any], task_id: str, key: str, occurrence: int
) -> dict[str, Any] | None:
    """A Workflow's journalled ``agent()`` result, ``{value}`` as stored, and ``ok``."""
    return conn.execute(
        "SELECT result, ok FROM ai_workflow_calls"
        " WHERE task_id = %s AND call_key = %s AND occurrence = %s",
        (task_id, key, occurrence),
    ).fetchone()


def journal(
    conn: Connection[Any],
    task_id: str,
    key: str,
    occurrence: int,
    *,
    result: Any,
    ok: bool,
    child_id: str | None,
) -> None:
    """Records one ``agent()`` result; a second write of the same call keeps the first."""
    conn.execute(
        "INSERT INTO ai_workflow_calls (task_id, call_key, occurrence, result, ok, child_id)"
        " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (task_id, key, occurrence, Jsonb({"value": result}), ok, child_id),
    )


def record_child(conn: Connection[Any], task_id: str, child_id: str) -> None:
    conn.execute(
        "INSERT INTO ai_workflow_children (task_id, child_id) VALUES (%s, %s)"
        " ON CONFLICT DO NOTHING",
        (task_id, child_id),
    )


def open_children(conn: Connection[Any], task_id: str) -> list[str]:
    """The child chats a Workflow's earlier attempts left open: not done, and named by no
    journal row."""
    rows = conn.execute(
        "SELECT c.child_id::text AS child_id FROM ai_workflow_children c"
        " JOIN ai_sessions s ON s.id = c.child_id AND s.status <> 'done'"
        " WHERE c.task_id = %s AND NOT EXISTS (SELECT 1 FROM ai_workflow_calls w"
        " WHERE w.task_id = c.task_id AND w.child_id = c.child_id)"
        " ORDER BY c.created_at",
        (task_id,),
    ).fetchall()
    return [r["child_id"] for r in rows]
