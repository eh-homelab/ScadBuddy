"""Take back a caller's database role (#5298 Tasks 11, 22 and 35; #5299 step 12): every
privilege the API granted it in the current schema, and its row policies, so the deployment
can drop the role (CNPG's `ensure: absent`). The role itself stays; dropping it is the
deployment's. No caller has a role now: every one is an HTTP client of the CRUD routes.

An older API granted a caller's role its tables (`auth/caller_grants.py` `grant_role`, gone
since #5299 step 12), USAGE on the schema and its credential views (`grant_caller_views`,
gone too); one older still (`render/worker_role.py`, gone since #5298 Task 22) also granted
sequences and, for a held table, a restrictive row policy named ``only_<role>_<hash>``
(af75581's was the unsuffixed ``only_<role>``). Nothing granted functions, default
privileges or memberships. A grant is revoked only by the role that made it (or the
owner's), so this revokes what a role this connection belongs to granted, and leaves the
rest. Production connects the API as the owner, which made every grant.

`retired_state` reports what each retired role still has in the whole database (relations
it is granted or owns, and sessions), for the start's log: the retirement order's checks
(root CLAUDE.md "Database access") read there, not with a shell on the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from psycopg import Connection, sql
from psycopg.rows import tuple_row

#: `pg_advisory_xact_lock` key for `revoke_role` ("SCADRNDR" in ASCII, from when the render
#: worker's role was the only one), which an older API's `grant_role` took too, so a revoke
#: never interleaves with an older replica's `grant_role` grants during a roll. An older
#: `grant_caller_views` took no lock: an old replica that starts mid-roll may grant its views
#: again, and the next start revokes them.
GRANT_LOCK = 0x5343_4144_524E_4452

#: The roles the API takes back at every start (`revoke_retired`): the projects worker's,
#: unused since the worker holds no database (#5576), the print worker's, unused since it
#: holds no pool (#5298 Tasks 31 and 33), the render worker's, unused since it holds no
#: database (#5298 Task 20), and the agent's, which nothing connects as (the agent sidecar
#: and worker connected as the owner, and then hold no database at all, #5299 step 12).
RETIRED_ROLES: tuple[str, ...] = (
    "scadbuddy_projects",
    "scadbuddy_print",
    "scadbuddy_render",
    "scadbuddy_agent",
)


def revoke_retired(conn: Connection[Any]) -> list[str]:
    """`revoke_role` each of `RETIRED_ROLES`; the roles that still held something."""
    return [role for role in RETIRED_ROLES if revoke_role(conn, role)]


@dataclass(frozen=True)
class RetiredState:
    """What a retired role still has in this database: relations in any schema with a
    privilege granted to it (by anyone), relations it owns, and its open sessions."""

    role: str
    granted: int
    owned: int
    sessions: int


def retired_state(conn: Connection[Any]) -> list[RetiredState]:
    """`RetiredState` of each of `RETIRED_ROLES` that exists, after `revoke_retired`. Reads
    only catalogs every role may read (`pg_class`, `pg_stat_activity`'s ``usename``)."""
    cur = conn.cursor(row_factory=tuple_row)
    states: list[RetiredState] = []
    for role in RETIRED_ROLES:
        row = cur.execute(
            "SELECT"
            " (SELECT count(DISTINCT c.oid) FROM pg_class c, aclexplode(c.relacl) a"
            "  WHERE a.grantee = r.oid),"
            " (SELECT count(*) FROM pg_class c WHERE c.relowner = r.oid),"
            " (SELECT count(*) FROM pg_stat_activity s WHERE s.usesysid = r.oid)"
            " FROM pg_roles r WHERE r.rolname = %s",
            (role,),
        ).fetchone()
        if row is not None:
            states.append(RetiredState(role, int(row[0]), int(row[1]), int(row[2])))
    return states


#: Whether the role named is ``current_user`` or one it is a member of, through
#: ``pg_auth_members`` (``pg_has_role`` would count every role for a superuser); no row
#: for a missing role.
MINE = (
    "WITH RECURSIVE mine(oid) AS (SELECT oid FROM pg_roles WHERE rolname = current_user"
    " UNION SELECT m.roleid FROM pg_auth_members m JOIN mine ON m.member = mine.oid)"
    " SELECT r.oid IN (SELECT oid FROM mine) FROM pg_roles r WHERE r.rolname = %s"
)


def revoke_role(conn: Connection[Any], role: str) -> list[str]:
    """Revoke what this connection's roles granted ``role`` in the current schema and
    drop its ``only_<role>`` row policies on the tables this connection's roles own (only
    a table's owner may drop one), under `GRANT_LOCK` (so it waits for, never
    interleaves with, an older API's `grant_role`); the relations, schema and policies
    it took back. Idempotent; a missing role, or one this connection's role is a member
    of, is a no-op."""
    revoked: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (GRANT_LOCK,))
        cur = conn.cursor(row_factory=tuple_row)
        found = cur.execute(MINE, (role,)).fetchone()
        # A role this connection is a member of is its own: never revoked here.
        if found is None or found[0]:
            return revoked
        relations = cur.execute(
            "SELECT DISTINCT c.relname, c.relkind = 'S' FROM pg_class c, aclexplode(c.relacl) a"
            " WHERE c.relnamespace = current_schema()::regnamespace"
            " AND a.grantee = %s::regrole AND pg_has_role(current_user, a.grantor, 'MEMBER')"
            " ORDER BY c.relname",
            (role,),
        ).fetchall()
        for name, sequence in relations:
            conn.execute(
                sql.SQL("REVOKE ALL ON {} {} FROM {}").format(
                    sql.SQL("SEQUENCE" if sequence else "TABLE"),
                    sql.Identifier(name),
                    sql.Identifier(role),
                )
            )
            revoked.append(name)
        policies = cur.execute(
            "SELECT p.polname, c.relname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid"
            " WHERE c.relnamespace = current_schema()::regnamespace"
            " AND pg_has_role(current_user, c.relowner, 'MEMBER')"
            " AND (p.polname = %(bare)s OR p.polname ~ %(hashed)s)"
            " ORDER BY c.relname, p.polname",
            # An older API's `grant_role` (`render/worker_role.py` `_hold_to_rows`) named
            # one ``only_<role>_<8 hex>``, so a role whose name only starts with this one
            # keeps its policies.
            {"bare": f"only_{role}", "hashed": f"^only_{re.escape(role)}_[0-9a-f]{{8}}$"},
        ).fetchall()
        for policy, table in policies:
            conn.execute(
                sql.SQL("DROP POLICY {} ON {}").format(
                    sql.Identifier(policy), sql.Identifier(table)
                )
            )
            revoked.append(policy)
        schema = cur.execute(
            "SELECT n.nspname FROM pg_namespace n, aclexplode(n.nspacl) a"
            " WHERE n.nspname = current_schema() AND a.grantee = %s::regrole"
            " AND pg_has_role(current_user, a.grantor, 'MEMBER')",
            (role,),
        ).fetchone()
        if schema is not None:
            conn.execute(
                sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(
                    sql.Identifier(schema[0]), sql.Identifier(role)
                )
            )
            revoked.append(schema[0])
    return revoked
