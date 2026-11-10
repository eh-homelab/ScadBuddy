"""#5298 Task 11: `revoke_role` takes back everything the API granted a caller's role
(`auth/revoke.py`), so the deployment can then drop the role."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import DictRow, dict_row

from scadbuddy.auth.caller_grants import AGENT_GRANTS
from scadbuddy.auth.revoke import (
    RETIRED_GRANTS,
    RETIRED_ROLES,
    revoke_retired,
    revoke_role,
    revoke_tables,
)
from scadbuddy.render.pg_store import migrate
from scadbuddy.render.worker_role import grant_role

pytestmark = pytest.mark.requires_postgres

Conn = psycopg.Connection[DictRow]


@pytest.fixture
def conn(pg_conninfo: str) -> Iterator[Conn]:
    with psycopg.connect(pg_conninfo, autocommit=True, row_factory=dict_row) as connection:
        migrate(connection)
        yield connection


@pytest.fixture
def role(pg_conninfo: str) -> Iterator[str]:
    name = f"revoked_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_conninfo, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        with psycopg.connect(pg_conninfo, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def _held(conn: Conn, role: str) -> dict[str, set[str]]:
    """What ``role`` holds in the current schema: relations and the schema with any
    privilege, and the row policies that name it."""
    relations = conn.execute(
        "SELECT c.relname AS name FROM pg_class c, aclexplode(c.relacl) a"
        " WHERE c.relnamespace = current_schema()::regnamespace"
        " AND a.grantee = %s::regrole",
        (role,),
    ).fetchall()
    schema = conn.execute(
        "SELECT n.nspname AS name FROM pg_namespace n, aclexplode(n.nspacl) a"
        " WHERE n.nspname = current_schema() AND a.grantee = %s::regrole",
        (role,),
    ).fetchall()
    policies = conn.execute(
        "SELECT p.polname AS name FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid"
        " WHERE c.relnamespace = current_schema()::regnamespace"
        " AND %s::regrole = ANY(p.polroles)",
        (role,),
    ).fetchall()
    return {
        "relations": {r["name"] for r in relations},
        "schema": {r["name"] for r in schema},
        "policies": {r["name"] for r in policies},
    }


def _grant_like_the_api(conn: Conn, role: str) -> None:
    """A role as the API left `scadbuddy_projects`: tables, a sequence, its credential
    view, the schema, and a row policy (today's name, and af75581's unsuffixed one)."""
    assert grant_role(
        conn,
        role,
        {"settings": ("SELECT",), "activity": ("SELECT", "INSERT"), "operations": ("SELECT",)},
        rows={"activity": "id LIKE 'render:%'"},
        sequences=("native_slices_id_seq",),
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON service_credentials_projects TO {}").format(sql.Identifier(role))
    )
    conn.execute(
        sql.SQL("CREATE POLICY {} ON activity AS RESTRICTIVE TO {} USING (false)").format(
            sql.Identifier(f"only_{role}"), sql.Identifier(role)
        )
    )


def test_the_role_holds_nothing_after_the_revoke(conn: Conn, role: str) -> None:
    _grant_like_the_api(conn, role)
    held = _held(conn, role)
    assert held["relations"] == {
        "settings",
        "activity",
        "operations",
        "native_slices_id_seq",
        "service_credentials_projects",
    }
    assert held["schema"] and len(held["policies"]) == 2

    revoked = revoke_role(conn, role)

    assert _held(conn, role) == {"relations": set(), "schema": set(), "policies": set()}
    assert set(revoked) >= {"settings", "native_slices_id_seq", f"only_{role}"}
    # Every other role keeps reading the table the policy was on.
    row = conn.execute(
        "SELECT count(*) AS n FROM pg_policies"
        " WHERE schemaname = current_schema() AND tablename = 'activity'"
        " AND policyname = 'everyone'"
    ).fetchone()
    assert row is not None and row["n"] == 1


def test_a_second_revoke_does_nothing(conn: Conn, role: str) -> None:
    _grant_like_the_api(conn, role)
    assert revoke_role(conn, role)
    assert revoke_role(conn, role) == []
    assert _held(conn, role) == {"relations": set(), "schema": set(), "policies": set()}


def test_a_table_dropped_from_a_callers_list_is_taken_back_at_start(
    conn: Conn, role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5299: the agent's role held `blob_refs` for its trigger; `grant_role` never revokes,
    so the start takes the table back and leaves the rest of the list."""
    assert RETIRED_GRANTS == {"scadbuddy_agent": ("blob_refs",)}
    assert "blob_refs" not in AGENT_GRANTS
    assert grant_role(conn, role, {**AGENT_GRANTS, "blob_refs": ("SELECT", "INSERT", "DELETE")})
    monkeypatch.setattr("scadbuddy.auth.revoke.RETIRED_ROLES", ())
    monkeypatch.setattr("scadbuddy.auth.revoke.RETIRED_GRANTS", {role: ("blob_refs",)})
    assert revoke_retired(conn) == [role]
    assert revoke_retired(conn) == []
    held = _held(conn, role)
    assert held["relations"] == set(AGENT_GRANTS)
    assert held["schema"]
    assert revoke_tables(conn, f"absent_{role}", ("blob_refs",)) == []


def test_a_missing_role_is_no_op(conn: Conn) -> None:
    assert revoke_role(conn, f"absent_{uuid.uuid4().hex[:12]}") == []


def test_the_role_can_be_dropped_after_the_revoke(pg_conninfo: str, conn: Conn) -> None:
    name = f"dropped_{uuid.uuid4().hex[:12]}"
    conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(name)))
    try:
        _grant_like_the_api(conn, name)
        with pytest.raises(psycopg.errors.DependentObjectsStillExist):
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
        revoke_role(conn, name)
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
    finally:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def test_only_what_this_connections_roles_granted_is_taken_back(conn: Conn, role: str) -> None:
    """In the deployment the agent's role owns the `ai_*` tables and grants them itself
    (`agent/src/db/grants.ts`); the API takes back only its own grants and the policies on
    its own tables, and does not fail on the agent's."""
    suffix = uuid.uuid4().hex[:12]
    api, agent = f"api_{suffix}", f"agent_{suffix}"
    for name in (api, agent):
        conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(name)))
    try:
        conn.execute(
            sql.SQL("GRANT CREATE, USAGE ON SCHEMA {} TO {}, {}").format(
                sql.Identifier(_schema(conn)), sql.Identifier(api), sql.Identifier(agent)
            )
        )
        for owner, table in ((api, "api_owned"), (agent, "ai_owned")):
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(owner)))
            conn.execute(sql.SQL("CREATE TABLE {} (id int)").format(sql.Identifier(table)))
            conn.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)
                )
            )
            conn.execute("RESET ROLE")
        # A policy for the role on a table the API does not own: only its owner may drop it.
        conn.execute(
            sql.SQL("CREATE POLICY {} ON ai_owned TO {} USING (true)").format(
                sql.Identifier(f"only_{role}_elsewhere"), sql.Identifier(role)
            )
        )
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(api)))
        assert revoke_role(conn, role) == ["api_owned"]
        conn.execute("RESET ROLE")
        held = _held(conn, role)
        assert held["relations"] == {"ai_owned"}
        assert held["policies"] == {f"only_{role}_elsewhere"}
    finally:
        conn.execute("RESET ROLE")
        for name in (api, agent):
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def _schema(conn: Conn) -> str:
    row = conn.execute("SELECT current_schema() AS name").fetchone()
    assert row is not None
    return str(row["name"])


def test_the_api_start_revokes_the_retired_roles(
    conn: Conn, role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert RETIRED_ROLES == ("scadbuddy_projects",)
    _grant_like_the_api(conn, role)
    monkeypatch.setattr("scadbuddy.auth.revoke.RETIRED_ROLES", (role, f"absent_{role}"))
    assert revoke_retired(conn) == [role]
    assert revoke_retired(conn) == []
    assert _held(conn, role) == {"relations": set(), "schema": set(), "policies": set()}


def test_a_role_this_connection_is_a_member_of_is_kept(conn: Conn, role: str) -> None:
    _grant_like_the_api(conn, role)
    conn.execute(sql.SQL("GRANT {} TO current_user").format(sql.Identifier(role)))
    try:
        assert revoke_role(conn, role) == []
        assert _held(conn, role)["relations"]
    finally:
        conn.execute(sql.SQL("REVOKE {} FROM current_user").format(sql.Identifier(role)))


def test_a_role_whose_name_extends_this_one_keeps_its_policies(
    pg_conninfo: str, conn: Conn, role: str
) -> None:
    longer = f"{role}_extra"
    with psycopg.connect(pg_conninfo, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(longer)))
    try:
        assert grant_role(conn, longer, {"activity": ("SELECT",)}, rows={"activity": "true"})
        kept = _held(conn, longer)["policies"]
        assert kept
        revoke_role(conn, role)
        assert _held(conn, longer)["policies"] == kept
    finally:
        with psycopg.connect(pg_conninfo, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(longer)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(longer)))
