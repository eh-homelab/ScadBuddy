"""Each service caller's own Postgres role, granted exactly what it touches (#5210, the
follow-up to #4975 phase 4, spec 2026-11-01 §7.2).

The deployment creates the roles and their passwords (`auth/service_credentials.py`
`ROLES`); the app owns the lists, as `render/worker_role.py` owns `RENDER_GRANTS`. The API
grants each existing role its list at every start (`grant_callers`), beside its credential
view (`grant_caller_views`). A worker on its own role never migrates: it waits until the
API has migrated and granted (`open_for_caller`), while a worker on the API's own role
(the deployment before it makes the roles) migrates as it always did.

The projects worker has no list and no role: it holds no database (#5298), and the API
takes back what its role `scadbuddy_projects` held at every start (`auth/revoke.py`).

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
#: no settings over SQL (#5298: the Bambuddy key comes from the API's CRUD route), but a
#: printer merge (`printers/store.py` `_merge`, from `bambuddy/print_run.py` `ensure_bambuddy`)
#: rewrites every table `PRINTER_REFERENCES` names, `settings` and `activity` included.
PRINT_GRANTS: Grants = {
    # The merge's rewrite of a printer's stored choices and plates (`printers/store.py`
    # `_merge`) until #5298 Task 29. The worker reads the settings through the API's CRUD
    # route (`crud/print.py`, #5298), so `model_print_choices` is gone from the list; an
    # existing role keeps it until the revoke.
    "settings": ("SELECT", "UPDATE"),
    "printer_bed_types": ("SELECT", "UPDATE", "DELETE"),
    # Whether the API has migrated (`wait_until_ready`).
    "scadbuddy_migrations": _READ,
    # The merge's repoint of activity rows until #5298 Task 29. The runs' and operations'
    # rows are the API's to write (#5298 Task 24).
    "activity": ("SELECT", "INSERT", "UPDATE"),
    # Read by a native print's spool uses (`spools/native.py`) and rewritten by the
    # printer merge (`PrinterReference("print_runs", "printer")`) until #5298 Task 31.
    # The runs themselves, their outcomes, the operations and the events go through the
    # API's CRUD routes (`crud/print.py`, `crud/operations.py`, #5298 Task 24); an
    # existing role keeps those grants until the revoke.
    "print_runs": _ALL,
    "print_links": ("SELECT", "INSERT"),
    "print_sends": ("SELECT", "INSERT", "UPDATE"),
    # Uploads, slices and projects (`bambuddy/uploads.py`).
    "output_bambuddy_uploads": _ALL,
    "output_bambuddy_slices": _ALL,
    "print_preview_slices": ("SELECT", "INSERT", "UPDATE"),
    "print_slice_starts": ("INSERT",),
    "project_print_targets": ("SELECT", "INSERT", "UPDATE"),
    "folderless_projects": _ALL,
    # ON CONFLICT DO UPDATE reads the row it updates.
    "destination_touches": ("SELECT", "INSERT", "UPDATE"),
    "output_last_prints": ("SELECT", "INSERT", "UPDATE"),
    # A native run's slice (`slicing/store.py` `start`) and held presets.
    "native_slices": ("SELECT", "INSERT", "UPDATE"),
    "held_local_presets": _READ,
    # Printers and their reports (`printers/store.py`, `printers/reports.py`).
    "printers": _ALL,
    "printer_reports": _ALL,
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
    "tray_spool_links": ("SELECT", "UPDATE", "DELETE"),
    "native_spools": ("SELECT", "UPDATE"),
    "native_spool_uses": ("SELECT", "INSERT", "UPDATE"),
}
#: The bigserial keys the print worker inserts with.
PRINT_SEQUENCES = (
    "print_preview_slices_id_seq",
    "print_slice_starts_id_seq",
    "native_slices_id_seq",
)

#: What the agent (`agent/`) touches of the backend's: it follows the event log, reads
#: flow runs and their decisions, and its session files' trigger
#: (`ai_session_files_hold_blob`, not SECURITY DEFINER) holds and drops `blob_refs`.
AGENT_GRANTS: Grants = {
    "events": _READ,
    "workflow_runs": _READ,
    "workflow_run_decisions": _READ,
    "flow_grants": ("SELECT", "UPDATE"),
    "flow_grant_uses": _READ,
    "blob_refs": ("SELECT", "INSERT", "DELETE"),
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
