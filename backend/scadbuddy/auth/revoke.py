"""Take back a caller's database role (#5298 Task 11): every privilege the API granted it
in the current schema, and its row policies, so the deployment can drop the role
(CNPG's `ensure: absent`). The role itself stays; dropping it is the deployment's.

The API granted a caller's role its tables and sequences (`render/worker_role.py`
`grant_role`, from `auth/caller_grants.py`), USAGE on the schema, its credential view
(`auth/service_credentials.py` `grant_caller_views`) and, for a held table, a restrictive
row policy named ``only_<role>_<hash>`` (af75581's was the unsuffixed ``only_<role>``).
Nothing granted functions, default privileges or memberships. The agent takes back its
own `ai_*` grants (`agent/src/db/grants.ts` `revokeRole`): a grant is revoked only by
the role that made it (or the owner's), so this revokes what a role this connection
belongs to granted, and leaves the rest.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection, sql
from psycopg.rows import tuple_row

from scadbuddy.render.worker_role import GRANT_LOCK

#: The roles the API takes back at every start (`revoke_retired`): the projects worker's,
#: unused since the worker holds no database (#5576).
RETIRED_ROLES: tuple[str, ...] = ("scadbuddy_projects",)


def revoke_retired(conn: Connection[Any]) -> list[str]:
    """`revoke_role` each of `RETIRED_ROLES`, after the grants at start; the roles that
    still held something."""
    return [role for role in RETIRED_ROLES if revoke_role(conn, role)]


def revoke_role(conn: Connection[Any], role: str) -> list[str]:
    """Revoke what this connection's roles granted ``role`` in the current schema and
    drop its ``only_<role>`` row policies, under `GRANT_LOCK` (so it waits for, never
    interleaves with, an older API's `grant_role`); the relations, schema and policies
    it took back. Idempotent; a missing role is a no-op."""
    revoked: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (GRANT_LOCK,))
        cur = conn.cursor(row_factory=tuple_row)
        if cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
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
            " AND (p.polname = %(bare)s OR starts_with(p.polname, %(prefix)s))"
            " ORDER BY c.relname, p.polname",
            {"bare": f"only_{role}", "prefix": f"only_{role}_"},
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
