"""The ``ai_audit`` row of each release of a secret to the agent (#5299 D5, #5342).

``ai_audit`` is the agent's append-only audit log (``agent/src/audit/log.ts``). A reveal of
a Claude credential and a release of a payload data key each write one row, in the same
transaction as the read, so no secret leaves the API without its row. The row never holds
the secret: ``detail`` names the credential by kind and last four characters, or the
subject. A payload key's release is written once per subject and window
(``payload_keys.py`` ``RELEASE_AUDIT_WINDOW``).
"""

from __future__ import annotations

from typing import Any, Literal

from psycopg import Connection

#: The kinds of these rows: a credential's reveal is ``credential``, a payload data key's
#: release ``payload_key`` (``migrations_agent/20261115T0736Z_audit_payload_key_kind.sql``).
Kind = Literal["credential", "payload_key"]
#: Who asked: the agent service's credential.
PRINCIPAL = ("service", "agent", "agent service")
#: The longest ``detail`` written, as the agent caps it (`audit/log.ts` ``DETAIL_MAX``).
DETAIL_MAX = 500

Outcome = Literal["ok", "error", "refused"]


def record(
    conn: Connection[Any],
    kind: Kind,
    action: str,
    outcome: Outcome,
    detail: str,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """One row, on ``conn`` (inside the caller's transaction)."""
    principal_kind, principal, label = PRINCIPAL
    conn.execute(
        "INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,"
        " principal_label, session_id, turn_id, outcome, detail, started_at, finished_at)"
        " VALUES (%s, %s, 'system', %s, %s, %s, %s, %s, %s, %s, now(), now())",
        (
            kind,
            action,
            principal_kind,
            principal,
            label,
            session_id,
            turn_id,
            outcome,
            detail[:DETAIL_MAX],
        ),
    )
