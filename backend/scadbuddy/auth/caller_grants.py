"""Each service caller's own Postgres role, granted exactly what it touches (#5210, the
follow-up to #4975 phase 4, spec 2026-11-01 §7.2).

The deployment creates the roles and their passwords (`auth/service_credentials.py`
`ROLES`); the app owns the lists. The API grants each existing role its list at every start
(`grant_callers`, through `grant_role`), beside its credential view (`grant_caller_views`).

The projects, render and print workers have no list: they hold no database (#5298), and
the API takes back what their roles `scadbuddy_projects`, `scadbuddy_render` and
`scadbuddy_print` held at every start (`auth/revoke.py`). A table dropped from a list is
taken back at every start only once it is listed in `auth/revoke.py` `RETIRED_GRANTS`:
`grant_role` never revokes.

A change to what a caller reads or writes changes its list in the same PR;
`tests/auth/test_caller_grants.py` runs each caller's stores as a role holding exactly
its list.

The `ai_*` tables are the agent's: its role creates and migrates them, so it owns them,
and the agent grants what the API's role reads of them (`agent/src/db/grants.ts`). The
lists here hold the backend's relations only.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from scadbuddy.auth.service_credentials import ROLES, ServiceCaller

logger = logging.getLogger(__name__)

Grants = Mapping[str, tuple[str, ...]]

_READ = ("SELECT",)

#: What the agent (`agent/`) touches of the backend's. Its gate reads ``flow_grants`` and
#: ``flow_grant_uses`` under the grant row's share lock (``agent/src/gate/store.ts``
#: `grantInput`, until the gate moves here, #5299 step 10). Its other reads (the event log,
#: flow runs, their decisions and grants) go through the CRUD routes of areas ``events``
#: and ``flows`` (`api/crud_agent.py`, #5299 D7), over SQL only while an older API pod
#: lacks them; once that deploy is done, ``events``, ``workflow_runs`` and
#: ``workflow_run_decisions`` are unused here and their revoke follows in its own PR. Its
#: workspace files' `blob_refs` holds go through the CRUD routes too; `auth/revoke.py`
#: `RETIRED_GRANTS` takes back the table it once held.
AGENT_GRANTS: Grants = {
    "events": _READ,
    "workflow_runs": _READ,
    "workflow_run_decisions": _READ,
    "flow_grants": ("SELECT", "UPDATE"),
    "flow_grant_uses": _READ,
}

GRANTS: Mapping[ServiceCaller, Grants] = {"agent": AGENT_GRANTS}

#: `pg_advisory_xact_lock` key for `grant_role` and `auth/revoke.py` ("SCADRNDR" in ASCII,
#: from when the render worker's role was the only one), so two API replicas starting
#: together, or the API and the agent (`agent/src/db/grants.ts` `GRANT_LOCK`), never race.
GRANT_LOCK = 0x5343_4144_524E_4452


def grant_role(conn: psycopg.Connection[Any], role: str, grants: Grants) -> bool:
    """Grant ``role`` USAGE on the current schema and ``grants`` (relation to privileges)
    there, under `GRANT_LOCK`, if the role exists; whether it did. Idempotent, never
    revokes (`auth/revoke.py` `RETIRED_GRANTS` does)."""
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
        return False
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (GRANT_LOCK,))
        schema = conn.cursor(row_factory=tuple_row).execute("SELECT current_schema()").fetchone()
        assert schema is not None and schema[0] is not None
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(str(schema[0])), sql.Identifier(role)
            )
        )
        for table, privileges in grants.items():
            conn.execute(
                sql.SQL("GRANT {} ON {} TO {}").format(
                    sql.SQL(", ").join(sql.SQL(p) for p in privileges),
                    sql.Identifier(table),
                    sql.Identifier(role),
                )
            )
    return True


def grant_callers(conn: psycopg.Connection[Any]) -> list[str]:
    """Grant each caller's role that exists its list; the roles granted."""
    granted: list[str] = []
    for caller, grants in GRANTS.items():
        role = ROLES[caller]
        if grant_role(conn, role, grants):
            granted.append(role)
    return granted
