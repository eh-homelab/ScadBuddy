"""The render worker's own Postgres role (#601, spec 2026-09-27 §9): render workers do
not hold the API's settings store.

`python -m scadbuddy.worker` (the default `render` queue) connects as whatever
``SCADBUDDY_DATABASE_URL`` names. Given a role of its own, that role holds exactly
`RENDER_GRANTS`, which the API grants it at every start (`grant_render_role`), once the
role exists: the deployment creates the role and its password, the app owns the list.
The worker never migrates; it waits until the API has (`wait_until_ready`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import psycopg
from psycopg import Connection, sql
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

from scadbuddy.render.pg_store import MIGRATIONS, Migration

logger = logging.getLogger(__name__)

#: The role the API grants `RENDER_GRANTS` to, when it exists.
RENDER_ROLE = "scadbuddy_render"

#: Every relation the render worker touches, and what it does there. A change to what
#: the worker reads or writes changes this list in the same PR;
#: `tests/test_render_worker_role.py` renders as a role holding exactly this.
RENDER_GRANTS: Mapping[str, tuple[str, ...]] = {
    # The job's row (`render/projection.py`: accept, claims, project).
    "render_jobs": ("SELECT", "INSERT", "UPDATE"),
    # A finished job's blobs (`store/refs.py` `add`, `JobProjection.accept`).
    "blob_refs": ("INSERT",),
    # The job.* events, written in the row's transaction (`core/pg_events.py`
    # `write_event`; RETURNING seq reads).
    "events": ("SELECT", "INSERT"),
    # The blob index (`store/index.py`): get, put, touch, and `forget` of a row whose
    # object is gone.
    "store_blobs": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    # The Bambuddy store's folders (`store/bambuddy.py`): found, recorded, forgotten.
    "store_folders": ("SELECT", "INSERT", "DELETE"),
    # `// file` uploads a render reads (`library/assets.py` `use`, `adopt`,
    # `prune_local`'s row lock). Never deleted here: the API's sweep decides.
    "assets": ("SELECT", "INSERT", "UPDATE"),
    # Render leases, pins in flight and install slots (`library/libraries.py`).
    "library_leases": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "library_pin_holds": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "library_install_slots": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    # The store's settings, and nothing else of `settings`
    # (`library/settings_store.py` `load_render_store_settings`).
    "render_settings": ("SELECT",),
    # Whether the API has migrated to this build's schema (`wait_until_ready`).
    "scadbuddy_migrations": ("SELECT",),
}

#: Seconds between looks while the worker waits for the API's migrations or grants.
READY_POLL = 5.0


def _role_exists(conn: Connection[Any], role: str) -> bool:
    found = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    return found is not None


def grant_render_role(conn: Connection[Any], role: str | None = None) -> bool:
    """Grant ``role`` (`RENDER_ROLE`) `RENDER_GRANTS` in the current schema, if it
    exists; whether it did. Idempotent, and never revokes: a grant this list no longer
    names stays until someone revokes it by hand."""
    role = role or RENDER_ROLE
    if not _role_exists(conn, role):
        return False
    with conn.transaction():
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(_current_schema(conn)), sql.Identifier(role)
            )
        )
        for table, privileges in RENDER_GRANTS.items():
            conn.execute(
                sql.SQL("GRANT {} ON {} TO {}").format(
                    sql.SQL(", ").join(sql.SQL(p) for p in privileges),
                    sql.Identifier(table),
                    sql.Identifier(role),
                )
            )
    return True


def grant_at_start(pool: ConnectionPool[Any]) -> None:
    """The API's start, after its migrations: `grant_render_role`. A failure is logged,
    never fatal to the API; a worker on that role then waits for a start that grants."""
    try:
        with pool.connection() as conn:
            granted = grant_render_role(conn)
    except psycopg.Error:
        logger.exception("could not grant the render worker's database role")
        return
    if granted:
        logger.info("granted the render worker's database role", extra={"role": RENDER_ROLE})


def _current_schema(conn: Connection[Any]) -> str:
    row = conn.cursor(row_factory=tuple_row).execute("SELECT current_schema()").fetchone()
    assert row is not None and row[0] is not None
    return str(row[0])


def pending_migrations(
    conn: Connection[Any], migrations: tuple[Migration, ...] = MIGRATIONS
) -> list[str]:
    """This build's migrations the ledger does not record yet."""
    cur = conn.cursor(row_factory=tuple_row)
    ledger = cur.execute("SELECT to_regclass('scadbuddy_migrations')").fetchone()
    if ledger is None or ledger[0] is None:
        return [m.id for m in migrations]
    done = {row[0] for row in cur.execute("SELECT id FROM scadbuddy_migrations")}
    return sorted(m.id for m in migrations if m.id not in done)


def missing_privileges(conn: Connection[Any]) -> list[str]:
    """The `RENDER_GRANTS` this connection's role lacks, as ``table: PRIVILEGE``."""
    cur = conn.cursor(row_factory=tuple_row)
    missing: list[str] = []
    for table, privileges in RENDER_GRANTS.items():
        exists = cur.execute("SELECT to_regclass(%s)", (table,)).fetchone()
        if exists is None or exists[0] is None:
            missing.extend(f"{table}: {p}" for p in privileges)
            continue
        for privilege in privileges:
            row = cur.execute("SELECT has_table_privilege(%s, %s)", (table, privilege)).fetchone()
            if row is None or not row[0]:
                missing.append(f"{table}: {privilege}")
    return missing


class WorkerStoppedError(Exception):
    """The worker was stopped while it waited for the API's schema or grants."""


def wait_until_ready(
    pool: ConnectionPool[Any],
    *,
    stopping: Callable[[], bool] = lambda: False,
    poll: float = READY_POLL,
) -> None:
    """Return once the API has applied every migration this build has and this role
    holds `RENDER_GRANTS`; until then log what is missing and look again every
    ``poll`` seconds. A worker never migrates: in a rollout it may start before the
    API, and its role may not create anything. ``stopping`` ends the wait with
    `WorkerStoppedError`."""
    last: tuple[list[str], list[str]] | None = None
    while True:
        with pool.connection() as conn:
            pending = pending_migrations(conn)
            missing = [] if pending else missing_privileges(conn)
        if not pending and not missing:
            return
        if (pending, missing) != last:
            if pending:
                logger.warning(
                    "waiting for the API to apply this build's database migrations",
                    extra={"pending": pending},
                )
            else:
                logger.warning(
                    "waiting for the API to grant this worker's database role what it"
                    f" needs; the API grants role {RENDER_ROLE!r} at start",
                    extra={"missing": missing},
                )
            last = (pending, missing)
        deadline = time.monotonic() + poll
        while time.monotonic() < deadline:
            if stopping():
                raise WorkerStoppedError
            time.sleep(min(0.1, poll))
