"""The agent's command record, ``ai_operations`` (#5299 step 8; spec 2026-10-01 §4.2 "Our
record", #1055), for ``api/crud_agent_tasks.py`` area ``operations``: one row per
execution of an AgentOperation workflow, written only by its activities.

Each function is the SQL of ``agent/src/operations/store.ts`` `OperationStore`, as one
transaction. A row that was inserted or ended gets its ``activity`` row in the same
transaction (``ActivityIndex.ingest_agent_operation_in``), as the backend's own
``operations`` store writes its row's (``operations/store.py`` ``_announce``): the API reads
the row it has just written, in its own table. An agent pod still on SQL announces the
change with ``ai_operation.changed`` instead, which the activity component ingests
(``activity/component.py``).
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from scadbuddy.activity.index import ActivityIndex

#: Finished operations are kept this long: the namespace's 168 h (spec §3.3).
OPERATION_RETENTION_SECONDS = 7 * 24 * 3600

COLUMNS = (
    "id, kind, subject, status, result, error, created_at, finished_at,"
    " workflow_id, workflow_run_id"
)


def find(conn: Connection[Any], operation_key: str) -> dict[str, Any] | None:
    """The key's newest operation, whatever its status: one key is one effect."""
    return conn.execute(
        f"SELECT {COLUMNS} FROM ai_operations WHERE operation_key = %s"
        " ORDER BY created_at DESC LIMIT 1",
        (operation_key,),
    ).fetchone()


def get(conn: Connection[Any], operation_id: str) -> dict[str, Any] | None:
    return conn.execute(
        f"SELECT {COLUMNS} FROM ai_operations WHERE id = %s", (operation_id,)
    ).fetchone()


def insert(
    conn: Connection[Any],
    activity: ActivityIndex | None,
    *,
    operation_id: str,
    kind: str,
    subject: str,
    operation_key: str,
    request: Any,
    workflow_id: str,
    workflow_run_id: str,
    principal: str | None,
    session: str | None,
    via_tool: str | None,
    retention_seconds: int,
) -> dict[str, Any]:
    """Records an accepted operation, or returns the execution's row when it has one (a
    retried activity). Prunes the operations finished more than the retention ago."""
    with conn.transaction():
        conn.execute(
            "DELETE FROM ai_operations WHERE finished_at < now() - make_interval(secs => %s)",
            (retention_seconds,),
        )
        inserted: dict[str, Any] | None = conn.execute(
            "INSERT INTO ai_operations (id, kind, subject, operation_key, status, request,"
            " workflow_id, workflow_run_id, principal, session, via_tool)"
            " VALUES (%s, %s, %s, %s, 'running', %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING"
            f" RETURNING {COLUMNS}",
            (
                operation_id,
                kind,
                subject,
                operation_key,
                Jsonb(request),
                workflow_id,
                workflow_run_id,
                principal,
                session,
                via_tool,
            ),
        ).fetchone()
        if inserted is not None:
            if activity is not None:
                activity.ingest_agent_operation_in(conn, inserted["id"])
            return inserted
        existing: dict[str, Any] | None = conn.execute(
            f"SELECT {COLUMNS} FROM ai_operations WHERE workflow_id = %s AND workflow_run_id = %s",
            (workflow_id, workflow_run_id),
        ).fetchone()
        assert existing is not None, "the conflicting execution's row is there"
        return existing


def finish(
    conn: Connection[Any],
    activity: ActivityIndex | None,
    operation_id: str,
    *,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Ends a running operation (``error`` for a failure); a retried end finds it ended
    and changes nothing. The row as it stands after, or None when there is none."""
    failed = error is not None
    with conn.transaction():
        ended = conn.execute(
            "UPDATE ai_operations SET status = %s, result = %s, error = %s, finished_at = now()"
            " WHERE id = %s AND status = 'running' RETURNING id",
            (
                "failed" if failed else "succeeded",
                None if failed else Jsonb(result),
                Jsonb(error) if failed else None,
                operation_id,
            ),
        ).fetchone()
        if ended is not None and activity is not None:
            activity.ingest_agent_operation_in(conn, operation_id)
        return get(conn, operation_id)
