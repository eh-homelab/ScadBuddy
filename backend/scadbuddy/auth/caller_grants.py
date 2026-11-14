"""Each service caller's own Postgres role, granted exactly what it touches (#5210, the
follow-up to #4975 phase 4, spec 2026-11-01 §7.2).

The deployment creates the roles and their passwords (`auth/service_credentials.py`
`ROLES`); the app owns the lists, as `render/worker_role.py` owns `RENDER_GRANTS`. The API
grants each existing role its list at every start (`grant_callers`), beside its credential
view (`grant_caller_views`).

The projects worker has no list and no role: it holds no database (#5298), and the API
takes back what its role `scadbuddy_projects` held at every start (`auth/revoke.py`). The
print worker's list is empty: it holds no pool (#5298 Task 31), and its role keeps what
it held until #5298 Task 35 revokes it. A table dropped from a list is taken back at every
start only once it is listed in `auth/revoke.py` `RETIRED_GRANTS`: `grant_role` never
revokes.

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

#: What the print worker (`python -m scadbuddy.worker --queue bambuddy`) touches over SQL:
#: nothing (#5298). Every store it reads or writes goes through the API's CRUD routes
#: (`crud/print.py`): its settings, runs, outcomes, operations, events, uploads, slices,
#: background slices, project records, print links, sends, outputs' last prints, printer
#: registry, LAN reports, access codes and rack hotends' usage (Tasks 23 to 30), and its
#: spools, ScadBuddy's own spools and their uses, the kept presets and a native slice's
#: start (`RemoteSpoolStore`, `RemoteNativeSpools`, `RemoteHeldPresets`,
#: `RemoteNativeSlices`, Task 31). It no longer waits for the migrations ledger either: it
#: waits for its CRUD areas. With a proof it exchanges that proof only (`exchange_source`,
#: no view fallback), whether or not `SCADBUDDY_DATABASE_URL` is set; only with no proof
#: and the URL set does it read its sealed credential from its view (`grant_caller_views`,
#: until #5298 Task 34); with neither it refuses to start. An existing role keeps every table it
#: held, and the `native_slices_id_seq` sequence, until #5298 Task 35 revokes it. Never
#: add a table here: add a CRUD route instead.
PRINT_GRANTS: Grants = {}
#: The bigserial keys the print worker inserts with: none (`native_slices` is written on
#: the API, #5298 Task 31).
PRINT_SEQUENCES: tuple[str, ...] = ()

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

GRANTS: Mapping[ServiceCaller, Grants] = {
    "agent": AGENT_GRANTS,
    "print": PRINT_GRANTS,
}
SEQUENCES: Mapping[ServiceCaller, tuple[str, ...]] = {"print": PRINT_SEQUENCES}


def grant_callers(conn: psycopg.Connection[Any]) -> list[str]:
    """Grant each caller's role that exists its list; the roles granted."""
    granted: list[str] = []
    for caller, grants in GRANTS.items():
        role = ROLES[caller]
        if grant_role(conn, role, grants, sequences=SEQUENCES.get(caller, ())):
            granted.append(role)
    return granted
