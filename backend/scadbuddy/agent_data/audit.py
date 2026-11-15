"""The ``ai_audit`` row of each release of a secret to the agent (#5299 D5, #5342).

``ai_audit`` is the agent's append-only audit log (``agent/src/audit/log.ts``). A reveal of
a Claude credential and a release of a payload data key each write one row, in the same
transaction as the read, so no secret leaves the API without its row. The row never holds
the secret: ``detail`` names the credential by kind and last four characters, or the
subject.
"""

from __future__ import annotations

from typing import Any, Literal

from psycopg import Connection

#: The kind of these rows: the agent's ``credential`` kind (a dedicated kind needs a
#: migration of the agent's table, which moves to the backend with #5299 step 2).
KIND = "credential"
#: Who asked: the agent service's credential.
PRINCIPAL = ("service", "agent", "agent service")
#: The longest ``detail`` written, as the agent caps it (`audit/log.ts` ``DETAIL_MAX``).
DETAIL_MAX = 500

Outcome = Literal["ok", "error", "refused"]


def record(
    conn: Connection[Any],
    action: str,
    outcome: Outcome,
    detail: str,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """One row, on ``conn`` (inside the caller's transaction)."""
    kind, principal, label = PRINCIPAL
    conn.execute(
        "INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,"
        " principal_label, session_id, turn_id, outcome, detail, started_at, finished_at)"
        " VALUES (%s, %s, 'system', %s, %s, %s, %s, %s, %s, %s, now(), now())",
        (
            KIND,
            action,
            kind,
            principal,
            label,
            session_id,
            turn_id,
            outcome,
            detail[:DETAIL_MAX],
        ),
    )
