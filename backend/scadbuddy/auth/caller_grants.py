"""Each service caller's own Postgres role, granted exactly what it touches (#5210, the
follow-up to #4975 phase 4, spec 2026-11-01 §7.2).

The deployment creates the roles and their passwords (`auth/service_credentials.py`
`ROLES`); the app owns the lists, as `render/worker_role.py` owns `RENDER_GRANTS`. The API
grants each existing role its list at every start (`grant_callers`), beside its credential
view (`grant_caller_views`).

The projects and print workers have no list: they hold no database (#5298), and the API
takes back what their roles `scadbuddy_projects` and `scadbuddy_print` held at every start
(`auth/revoke.py`). A table dropped from a list is taken back at every start only once it
is listed in `auth/revoke.py` `RETIRED_GRANTS`: `grant_role` never revokes.

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

from scadbuddy.auth.service_credentials import ROLES, ServiceCaller
from scadbuddy.render.worker_role import grant_role

logger = logging.getLogger(__name__)

Grants = Mapping[str, tuple[str, ...]]

_READ = ("SELECT",)

#: What the agent (`agent/`) touches of the backend's: it follows the event log, and reads
#: flow runs and their decisions. Its workspace files' `blob_refs` holds go through the
#: CRUD routes (`api/crud_agent.py`, #5299); `auth/revoke.py` `RETIRED_GRANTS` takes back
#: the table it once held.
AGENT_GRANTS: Grants = {
    "events": _READ,
    "workflow_runs": _READ,
    "workflow_run_decisions": _READ,
    "flow_grants": ("SELECT", "UPDATE"),
    "flow_grant_uses": _READ,
}

GRANTS: Mapping[ServiceCaller, Grants] = {"agent": AGENT_GRANTS}


def grant_callers(conn: psycopg.Connection[Any]) -> list[str]:
    """Grant each caller's role that exists its list; the roles granted."""
    granted: list[str] = []
    for caller, grants in GRANTS.items():
        role = ROLES[caller]
        if grant_role(conn, role, grants):
            granted.append(role)
    return granted
