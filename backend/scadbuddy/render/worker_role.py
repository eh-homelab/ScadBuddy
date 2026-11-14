"""The render worker's own Postgres role (#601, spec 2026-09-27 §9), and the grants every
caller's role gets through `grant_role` (`auth/caller_grants.py`).

`python -m scadbuddy.worker` (the default `render` queue) holds no database (#5298): it
reads and writes through the API's CRUD routes, and opens no connection. The role
``scadbuddy_render`` stays until #5298 Task 22 revokes and drops it; until then the API
grants it `RENDER_GRANTS` (nothing now) at every start (`grant_render_role`), once the
role exists, and keeps `RENDER_ROWS` on what it was granted before.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg import Connection, sql
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

#: The role the API grants `RENDER_GRANTS` to, when it exists.
RENDER_ROLE = "scadbuddy_render"

#: Every relation the render worker touches, and what it does there: none, since it holds
#: no database (#5298 Task 20, `tests/test_render_worker_nodb.py`). A role granted any of
#: the relations below before keeps them until #5298 Task 22 revokes them; the API never
#: revokes at start.
RENDER_GRANTS: Mapping[str, tuple[str, ...]] = {
    # No `render_jobs`, `events` or `activity`: the job's row, its job.* events and its
    # activity row go through the API's CRUD routes (#5298, `crud/render.py`
    # `RemoteJobProjection`). Nor `blob_refs`, `piece_geometry`, `store_blobs`,
    # `store_folders` or `assets`: a finished job's blob refs, a finished piece's
    # geometry, the blob index, the Bambuddy store's folders and the uploads' rows go
    # through them too (`RemoteBlobRefs`, `RemoteGeometryIndex`, `RemoteBlobIndex`,
    # `RemoteStoreFolders`, `RemoteAssetRows`; the API finds a folder itself). Nor
    # `library_leases`, `library_pin_holds` or `library_install_slots`: the render
    # leases, pins in flight and install slots go through them as well
    # (`RemoteCheckoutLeases`, `RemoteInstallSlots`). A role granted them before keeps
    # them until #5298 Task 22 revokes them.
    # No `render_settings`: the store settings come from the API's CRUD route (#5298,
    # `crud/render.py`). A role granted it before keeps it until the role is revoked.
    # No `scadbuddy_migrations`: the worker no longer waits for the API's migrations; it
    # waits for the API to serve its CRUD areas (`crud/client.py` `wait_for_areas`).
}

#: Tables the role may reach only some rows of, and which: a restrictive row policy
#: for the role beside a permissive one for everyone else, so every other role reads
#: and writes as before (the owner, which migrates, bypasses row security anyway).
RENDER_ROWS: Mapping[str, str] = {
    # Every principal's jobs are listed there. The worker no longer writes there (#5298),
    # but a role granted `activity` before keeps it until Task 22 revokes it; until then
    # this still holds that role to its renders' rows.
    "activity": "id LIKE 'render:%'",
}

#: `pg_advisory_xact_lock` key for `grant_render_role` ("SCADRNDR" in ASCII), so two
#: API replicas starting together never race on its policies.
GRANT_LOCK = 0x5343_4144_524E_4452


def _role_exists(conn: Connection[Any], role: str) -> bool:
    found = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    return found is not None


def grant_render_role(conn: Connection[Any], role: str | None = None) -> bool:
    """Grant ``role`` (`RENDER_ROLE`) `RENDER_GRANTS` in the current schema, if it
    exists; whether it did. Idempotent, and never revokes: a grant this list no longer
    names stays until someone revokes it by hand."""
    return grant_role(conn, role or RENDER_ROLE, RENDER_GRANTS, rows=RENDER_ROWS)


def grant_role(
    conn: Connection[Any],
    role: str,
    grants: Mapping[str, tuple[str, ...]],
    *,
    rows: Mapping[str, str] | None = None,
    sequences: tuple[str, ...] = (),
) -> bool:
    """Grant ``role`` ``grants`` (relation to privileges) and USAGE on ``sequences`` in
    the current schema, if the role exists; whether it did. Idempotent, never revokes.
    Every caller's list goes through here (#5210: `auth/caller_grants.py`)."""
    if not _role_exists(conn, role):
        return False
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (GRANT_LOCK,))
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(_current_schema(conn)), sql.Identifier(role)
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
        for sequence in sequences:
            conn.execute(
                sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(
                    sql.Identifier(sequence), sql.Identifier(role)
                )
            )
        for table, predicate in (rows or {}).items():
            _hold_to_rows(conn, role, table, predicate)
    return True


def _hold_to_rows(conn: Connection[Any], role: str, table: str, rows: str) -> None:
    """Hold ``role`` to ``rows`` of ``table``. Enabling row security takes effect because
    the API's role owns the table (it migrates) and so bypasses it; `everyone`, the
    permissive policy, keeps every other non-owner role (the agent's) unrestricted, since
    with row security on and no policy a role
    sees no row. The role's policy is named by its predicate, so a changed predicate
    replaces the old one rather than leaving it in force (and so does af75581's
    unsuffixed ``only_<role>``)."""
    existing = {
        row[0]
        for row in conn.cursor(row_factory=tuple_row).execute(
            "SELECT policyname FROM pg_policies WHERE schemaname = current_schema()"
            " AND tablename = %s",
            (table,),
        )
    }
    conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(sql.Identifier(table)))
    if "everyone" not in existing:
        conn.execute(
            sql.SQL("CREATE POLICY everyone ON {} USING (true) WITH CHECK (true)").format(
                sql.Identifier(table)
            )
        )
    prefix = f"only_{role}_"
    policy = prefix + hashlib.sha256(rows.encode()).hexdigest()[:8]
    stale_policies = (p for p in existing if p.startswith(prefix) or p == f"only_{role}")
    for stale in sorted(p for p in stale_policies if p != policy):
        conn.execute(
            sql.SQL("DROP POLICY {} ON {}").format(sql.Identifier(stale), sql.Identifier(table))
        )
    if policy not in existing:
        conn.execute(
            sql.SQL(
                "CREATE POLICY {} ON {} AS RESTRICTIVE TO {} USING ({rows}) WITH CHECK ({rows})"
            ).format(
                sql.Identifier(policy),
                sql.Identifier(table),
                sql.Identifier(role),
                rows=sql.SQL(rows),
            )
        )


def grant_at_start(pool: ConnectionPool[Any]) -> None:
    """The API's start, after its migrations: `grant_render_role`, which grants the unused
    role nothing new until #5298 Task 22. A failure is logged, never fatal to the API."""
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
