"""Each service caller's own Postgres role, granted exactly what it touches (#5210, the
follow-up to #4975 phase 4, spec 2026-11-01 §7.2).

The deployment creates the roles and their passwords (`auth/service_credentials.py`
`ROLES`); the app owns the lists, as `render/worker_role.py` owns `RENDER_GRANTS`. The API
grants each existing role its list at every start (`grant_callers`), beside its credential
view (`grant_caller_views`). A worker on its own role never migrates: it waits until the
API has migrated and granted (`open_for_caller`), while a worker on the API's own role
(the deployment before it makes the roles) migrates as it always did.

The projects worker has no list and no role: it holds no database (#5298), and the API
takes back what its role `scadbuddy_projects` held at every start (`auth/revoke.py`). A
table dropped from a list is taken back at every start too (`auth/revoke.py`
`RETIRED_GRANTS`): `grant_role` never revokes.

A change to what a caller reads or writes changes its list in the same PR;
`tests/auth/test_caller_grants.py` runs each caller's stores as a role holding exactly
its list.

The `ai_*` tables are the agent's: its role creates and migrates them, so it owns them,
and the agent grants what the API's role reads of them (`agent/src/db/grants.ts`). The
lists here hold the backend's relations only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import psycopg

from scadbuddy.auth.service_credentials import ROLES, ServiceCaller
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.render.worker_role import grant_role, owns_schema, wait_until_ready

logger = logging.getLogger(__name__)

Grants = Mapping[str, tuple[str, ...]]

_ALL = ("SELECT", "INSERT", "UPDATE", "DELETE")
_READ = ("SELECT",)

#: What the print worker (`python -m scadbuddy.worker --queue bambuddy`) touches. It reads
#: no settings over SQL (#5298: the Bambuddy key comes from the API's CRUD route), and its
#: printer registry, LAN reports and access codes go through the API's CRUD routes too
#: (`crud/print.py` `RemotePrinterStore`, `RemoteReports`, #5298 Task 29). So a printer
#: merge (`printers/store.py` `_merge`) runs on the API alone, and the worker writes none
#: of the tables `PRINTER_REFERENCES` names for it. An existing role keeps what it held of
#: `printers`, `printer_reports`, `settings`, `printer_bed_types`, `activity` and
#: `project_print_targets`, and the merge's UPDATE and DELETE of `tray_spool_links`, until
#: the revoke. Never add a table here for a merge.
PRINT_GRANTS: Grants = {
    # Whether the API has migrated (`wait_until_ready`).
    "scadbuddy_migrations": _READ,
    # Read by a native print's spool uses (`spools/native.py`) until #5298 Task 31. The
    # runs themselves, their outcomes, the operations and the events go through the
    # API's CRUD routes (`crud/print.py`, `crud/operations.py`, #5298 Task 24), and so do
    # the print links, the sends and the outputs' last prints (`RemotePrintLinks`,
    # `RemoteOutputPrints`, #5298 Task 28); an existing role keeps those grants until
    # the revoke.
    "print_runs": _ALL,
    # A native run's slice (`slicing/store.py` `start`) and held presets.
    "native_slices": ("SELECT", "INSERT", "UPDATE"),
    "held_local_presets": _READ,
    # The rack's nozzles (`rack/usage.py`).
    "rack_nozzle_seen": ("SELECT", "INSERT", "UPDATE"),
    "rack_nozzle_picks": _ALL,
    "rack_nozzle_pick_spools": ("SELECT", "INSERT", "DELETE"),
    "rack_nozzle_prints": ("SELECT", "INSERT"),
    # Spools: the overlay's recache and a native run's uses (`spools/`).
    "physical_spools": ("SELECT", "INSERT", "UPDATE"),
    "spool_provider_records": ("SELECT", "INSERT", "DELETE"),
    "spool_record_links": ("SELECT", "INSERT", "DELETE"),
    "spool_provider_refreshes": ("SELECT", "INSERT", "UPDATE"),
    # Read by the overlay; a tray's link is written on the API, and the merge's
    # repoint (`spools/store.py` `repoint_tray_links`) runs there too (#5298 Task 29).
    "tray_spool_links": _READ,
    "native_spools": ("SELECT", "UPDATE"),
    "native_spool_uses": ("SELECT", "INSERT", "UPDATE"),
}
#: The bigserial keys the print worker inserts with.
PRINT_SEQUENCES = ("native_slices_id_seq",)

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


def open_for_caller(
    store: SettingsStore,
    caller: ServiceCaller,
    *,
    stopping: Callable[[], bool] = lambda: False,
) -> None:
    """Open a worker's settings store: on the API's own role, migrate and seal as
    before; on a role of its own, wait until the API has migrated and granted it."""
    store.open(migrate_schema=False)
    pool = store.pool
    with pool.connection() as conn:
        owner = owns_schema(conn)
    if owner:
        store.migrate_and_seal()
        return
    wait_until_ready(pool, stopping=stopping, grants=GRANTS[caller], role=ROLES[caller])
