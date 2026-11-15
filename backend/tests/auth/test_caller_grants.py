"""#5210: each caller's own role holds exactly its list (`auth/caller_grants.py`). The
projects, print and render workers have none (#5298): they hold no database."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.auth.caller_grants import GRANT_LOCK, GRANTS, grant_callers, grant_role
from scadbuddy.auth.service_credentials import ROLES, VIEWS, ServiceCaller
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.render.pg_store import Migration, migrate
from tests.conftest import UNUSED_TEMPORAL_ADDRESS

pytestmark = pytest.mark.requires_postgres

Pool = ConnectionPool[psycopg.Connection[DictRow]]


def _pool(conninfo: str) -> Pool:
    pool: Pool = ConnectionPool(
        conninfo,
        kwargs={"row_factory": dict_row, "autocommit": True},
        min_size=1,
        max_size=2,
        open=False,
    )
    pool.open(wait=True, timeout=30)
    return pool


@pytest.fixture
def owner(pg_conninfo: str) -> Iterator[Pool]:
    pool = _pool(pg_conninfo)
    with pool.connection() as conn:
        migrate(conn)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def role(pg_conninfo: str) -> Iterator[str]:
    name = f"caller_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(name)
            )
        )
    try:
        yield name
    finally:
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def _schema(conn: psycopg.Connection[DictRow]) -> str:
    row = conn.execute("SELECT current_schema() AS name").fetchone()
    assert row is not None
    return str(row["name"])


def _grant(owner: Pool, role: str, caller: ServiceCaller) -> None:
    with owner.connection() as conn:
        assert grant_role(conn, role, GRANTS[caller])


def _store(conninfo: str, tmp_path: Path) -> SettingsStore:
    return SettingsStore(
        Settings(
            data_dir=tmp_path, database_url=conninfo, temporal_address=UNUSED_TEMPORAL_ADDRESS
        ),
        connect_timeout=30.0,
    )


@pytest.mark.parametrize("caller", list(GRANTS))
def test_every_grant_names_a_relation_the_schema_has(owner: Pool, caller: ServiceCaller) -> None:
    with owner.connection() as conn:
        for name in GRANTS[caller]:
            row = conn.execute("SELECT to_regclass(%s) AS found", (name,)).fetchone()
            assert row is not None and row["found"] is not None, name


def test_no_deployment_role_is_granted_where_none_exists(owner: Pool) -> None:
    # The `scadbuddy_*` roles are the deployment's; a test cluster has none of them but
    # those another test made (`tests/auth/test_service_credentials.py`).
    with owner.connection() as conn:
        listed = [ROLES[caller] for caller in GRANTS]
        existing = {
            r["rolname"]
            for r in conn.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (listed,))
        }
        assert set(grant_callers(conn)) == existing


@pytest.mark.parametrize("caller", list(GRANTS))
def test_the_role_holds_its_list_and_cannot_migrate(
    owner: Pool, role: str, pg_conninfo: str, caller: ServiceCaller
) -> None:
    _grant(owner, role, caller)
    pool = _pool(make_conninfo(pg_conninfo, user=role, password=role))
    try:
        with pool.connection() as conn:
            for table, privileges in GRANTS[caller].items():
                for privilege in privileges:
                    row = conn.execute(
                        "SELECT has_table_privilege(%s, %s) AS held", (table, privilege)
                    ).fetchone()
                    assert row is not None and row["held"], f"{table}: {privilege}"
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                migrate(conn, (Migration("29991231T2359Z_later", "CREATE TABLE later (id int)"),))
    finally:
        pool.close()


@pytest.mark.parametrize("caller", ["projects", "print", "render"])
def test_a_worker_with_no_database_has_no_list_and_no_role(caller: ServiceCaller) -> None:
    """#5298: the projects, print and render workers hold no database; their data goes
    through the CRUD routes, and the API takes back what their roles held
    (`auth/revoke.py`, `tests/auth/test_revoke.py`)."""
    assert caller not in GRANTS and caller not in ROLES and caller not in VIEWS


def test_a_role_that_does_not_exist_is_granted_nothing(owner: Pool) -> None:
    with owner.connection() as conn:
        assert grant_role(conn, f"absent_{uuid.uuid4().hex[:8]}", GRANTS["agent"]) is False


def test_the_grant_waits_for_another_replicas_lock(
    owner: Pool, role: str, pg_conninfo: str
) -> None:
    """Two API replicas starting together, or the API and the agent, grant one after the
    other under `GRANT_LOCK`."""
    with psycopg.connect(pg_conninfo) as other:  # not autocommit: the lock holds till commit
        other.execute("SELECT pg_advisory_xact_lock(%s)", (GRANT_LOCK,))
        with ThreadPoolExecutor(1) as pool:
            pids: list[int] = []

            def grant() -> bool:
                with owner.connection() as conn:
                    pids.append(conn.info.backend_pid)
                    return grant_role(conn, role, GRANTS["agent"])

            def waiting() -> bool:
                return (
                    bool(pids)
                    and look.execute(
                        "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                        " AND pid = %s AND classid::bigint = %s AND objid::bigint = %s"
                        " AND objsubid = 1",
                        (pids[0], GRANT_LOCK >> 32, GRANT_LOCK & 0xFFFF_FFFF),
                    ).fetchone()
                    is not None
                )

            granting = pool.submit(grant)
            with psycopg.connect(pg_conninfo, autocommit=True) as look:
                deadline = time.monotonic() + 30.0
                while not waiting():
                    assert time.monotonic() < deadline, "the grant never waited on the lock"
                    if granting.done():
                        failed = granting.exception()
                        pytest.fail(f"the grant finished without waiting: {failed or 'granted'}")
                    time.sleep(0.05)
            assert not granting.done()
            other.commit()
            assert granting.result(timeout=30.0)
