"""A flow archived or deleted (#2485): ``workflow_flow_states``, one row per name.

Both apply to the flow, every version of it, and keep its versions' rows: a run names
one, and a finished run's script is read from git at that version's commit. Archiving
only hides the flow and refuses its starts. Deleting is a commit that removes
``<name>/`` from ``data/flows`` (``FlowHistory.delete``), recorded here under the flows
repository's write lock; registering or restoring a version brings the flow back.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.flows.models import DefinitionSummary, FlowState

_STATE = "name, archived_at, archived_by, deleted_at, deleted_by, deleted_commit"


class FlowStates:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def exists(self, name: str) -> bool:
        """Whether the flow has a version."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM workflow_definitions WHERE name = %s LIMIT 1", (name,)
            ).fetchone()
        return row is not None

    def state(self, name: str) -> FlowState:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_STATE} FROM workflow_flow_states WHERE name = %s", (name,)
            ).fetchone()
        return FlowState.model_validate(row) if row else FlowState(name=name)

    def archive(self, name: str, by: dict[str, Any]) -> FlowState:
        """Archive it; one archived already keeps when and by whom."""
        return self._write(
            "INSERT INTO workflow_flow_states (name, archived_at, archived_by)"
            " VALUES (%(name)s, now(), %(by)s) ON CONFLICT (name) DO UPDATE SET"
            " archived_by = COALESCE(workflow_flow_states.archived_by, EXCLUDED.archived_by),"
            " archived_at = COALESCE(workflow_flow_states.archived_at, EXCLUDED.archived_at)",
            {"name": name, "by": Jsonb(by)},
        )

    def unarchive(self, name: str) -> FlowState:
        return self._write(
            "UPDATE workflow_flow_states SET archived_at = NULL, archived_by = NULL"
            " WHERE name = %(name)s",
            {"name": name},
        )

    def mark_deleted(self, name: str, by: dict[str, Any], commit: str) -> FlowState:
        """Record the commit that removed it; one deleted already keeps when and by whom."""
        return self._write(
            "INSERT INTO workflow_flow_states (name, deleted_at, deleted_by, deleted_commit)"
            " VALUES (%(name)s, now(), %(by)s, %(commit)s) ON CONFLICT (name) DO UPDATE SET"
            " deleted_by = COALESCE(workflow_flow_states.deleted_by, EXCLUDED.deleted_by),"
            " deleted_at = COALESCE(workflow_flow_states.deleted_at, EXCLUDED.deleted_at),"
            " deleted_commit = EXCLUDED.deleted_commit",
            {"name": name, "by": Jsonb(by), "commit": commit},
        )

    def revive(self, name: str) -> None:
        """A version was registered or restored: the flow is no longer deleted."""
        self._write(
            "UPDATE workflow_flow_states SET deleted_at = NULL, deleted_by = NULL,"
            " deleted_commit = NULL WHERE name = %(name)s",
            {"name": name},
        )

    def newest(self, *, hidden: bool) -> list[DefinitionSummary]:
        """Each flow at its newest version, by name, with its state: archived and
        deleted flows too when ``hidden``."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ON (d.name) d.id, d.name, d.version, d.commit,"
                " d.approval_timeout_s, d.created_by, d.created_at,"
                " s.archived_at, s.archived_by, s.deleted_at, s.deleted_by"
                " FROM workflow_definitions d"
                " LEFT JOIN workflow_flow_states s ON s.name = d.name"
                " WHERE %(hidden)s OR (s.archived_at IS NULL AND s.deleted_at IS NULL)"
                " ORDER BY d.name, d.version DESC",
                {"hidden": hidden},
            ).fetchall()
        return [DefinitionSummary.model_validate(row) for row in rows]

    def _write(self, sql: str, params: dict[str, Any]) -> FlowState:
        with self._pool.connection() as conn:
            conn.execute(sql, params)
            row = conn.execute(
                f"SELECT {_STATE} FROM workflow_flow_states WHERE name = %s", (params["name"],)
            ).fetchone()
        return FlowState.model_validate(row) if row else FlowState(name=params["name"])
