"""Take back a caller's database role (#5298 Tasks 11, 22 and 35): every privilege the API
granted it in the current schema, and its row policies, so the deployment can drop the role
(CNPG's `ensure: absent`). The role itself stays; dropping it is the deployment's. A
caller that keeps its role loses only the tables dropped from its list
(`RETIRED_GRANTS`, `revoke_tables`).

The API granted a caller's role its tables (`auth/caller_grants.py` `grant_role`), USAGE
on the schema and its credential view (`auth/service_credentials.py`
`grant_caller_views`); an older API's `grant_role` (`render/worker_role.py`, gone since
#5298 Task 22) also granted sequences and, for a held table, a restrictive row policy
named ``only_<role>_<hash>`` (af75581's was the unsuffixed ``only_<role>``).
Nothing granted functions, default privileges or memberships. The agent takes back its
own `ai_*` grants (`agent/src/db/grants.ts` `revokeRole`): a grant is revoked only by
the role that made it (or the owner's), so this revokes what a role this connection
belongs to granted, and leaves the rest.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from psycopg import Connection, sql
from psycopg.rows import tuple_row

from scadbuddy.auth.caller_grants import GRANT_LOCK

#: The roles the API takes back at every start (`revoke_retired`): the projects worker's,
#: unused since the worker holds no database (#5576), the print worker's, unused since it
#: holds no pool (#5298 Tasks 31 and 33), and the render worker's, unused since it holds no
#: database (#5298 Task 20).
RETIRED_ROLES: tuple[str, ...] = ("scadbuddy_projects", "scadbuddy_print", "scadbuddy_render")


def revoke_retired(conn: Connection[Any]) -> list[str]:
    """`revoke_role` each of `RETIRED_ROLES`, and `revoke_tables` each of
    `RETIRED_GRANTS`, after the grants at start; the roles that still held something."""
    roles = [role for role in RETIRED_ROLES if revoke_role(conn, role)]
    roles += [role for role, tables in RETIRED_GRANTS.items() if revoke_tables(conn, role, tables)]
    return roles


#: The tables a caller's role no longer touches, taken back at every start: `grant_role`
#: never revokes, so a table dropped from a caller's list (`auth/caller_grants.py`) is
#: listed here. The agent's `blob_refs`: its workspace files are held through the CRUD
#: routes (`api/crud_agent.py`, #5299), no longer by its trigger.
RETIRED_GRANTS: Mapping[str, tuple[str, ...]] = {"scadbuddy_agent": ("blob_refs",)}


def revoke_tables(conn: Connection[Any], role: str, tables: tuple[str, ...]) -> list[str]:
    """Revoke what this connection's roles granted ``role`` on ``tables`` in the current
    schema, under `GRANT_LOCK`; the tables it took back. Idempotent; a missing role or
    table, or a role this connection is a member of, is a no-op."""
    revoked: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (GRANT_LOCK,))
        cur = conn.cursor(row_factory=tuple_row)
        found = cur.execute(MINE, (role,)).fetchone()
        if found is None or found[0]:
            return revoked
        held = cur.execute(
            "SELECT DISTINCT c.relname FROM pg_class c, aclexplode(c.relacl) a"
            " WHERE c.relnamespace = current_schema()::regnamespace AND c.relname = ANY(%s)"
            " AND a.grantee = %s::regrole AND pg_has_role(current_user, a.grantor, 'MEMBER')"
            " ORDER BY c.relname",
            (list(tables), role),
        ).fetchall()
        for (name,) in held:
            conn.execute(
                sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(
                    sql.Identifier(name), sql.Identifier(role)
                )
            )
            revoked.append(name)
    return revoked


#: Whether the role named is ``current_user`` or one it is a member of, through
#: ``pg_auth_members`` (``pg_has_role`` would count every role for a superuser); no row
#: for a missing role. The agent's `revokeRole` (`agent/src/db/grants.ts`) asks the same.
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
        # A role this connection is a member of is its own: never revoked here, as the
        # agent's `revokeRole` skips it too.
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
