"""#5210: each caller's own role holds exactly its list (`auth/caller_grants.py`), and a
worker on such a role waits for the API rather than migrating, as the render worker does
(`tests/test_render_worker_role.py`)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.auth.caller_grants import (
    GRANTS,
    SEQUENCES,
    grant_callers,
    open_for_caller,
)
from scadbuddy.auth.service_credentials import ROLES, VIEWS, ServiceCaller
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.operations.store import OperationStore
from scadbuddy.render.pg_store import Migration, migrate
from scadbuddy.render.worker_role import (
    WorkerStoppedError,
    grant_role,
    missing_privileges,
    owns_schema,
)
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
        assert grant_role(conn, role, GRANTS[caller], sequences=SEQUENCES.get(caller, ()))


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
        for name in [*GRANTS[caller], *SEQUENCES.get(caller, ())]:
            row = conn.execute("SELECT to_regclass(%s) AS found", (name,)).fetchone()
            assert row is not None and row["found"] is not None, name


def test_no_deployment_role_is_granted_where_none_exists(owner: Pool) -> None:
    # The `scadbuddy_*` roles are the deployment's; a test cluster has none of them.
    with owner.connection() as conn:
        existing = {
            r["rolname"]
            for r in conn.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'scadbuddy\\_%'")
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
            assert missing_privileges(conn, GRANTS[caller]) == []
            assert owns_schema(conn) is False
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                migrate(conn, (Migration("29991231T2359Z_later", "CREATE TABLE later (id int)"),))
    finally:
        pool.close()


def test_a_worker_on_its_own_role_opens_reads_settings_and_runs_an_operation(
    owner: Pool, role: str, pg_conninfo: str, tmp_path: Path
) -> None:
    _grant(owner, role, "print")
    store = _store(make_conninfo(pg_conninfo, user=role, password=role), tmp_path)
    open_for_caller(store, "print")
    try:
        store.snapshot()
        operations = OperationStore(store.pool)

        async def run() -> Any:
            op_id = uuid.uuid4().hex
            await operations.insert(
                op_id,
                kind="test",
                subject="test:1",
                operation_key=op_id,
                request={},
                workflow_id=f"wf-{op_id}",
                workflow_run_id="run",
                retention=timedelta(days=1),
            )
            return await operations.finish(op_id, result={"ok": True})

        import asyncio

        assert asyncio.run(run()).status == "succeeded"
    finally:
        store.close()


def test_a_worker_on_a_role_without_its_grants_waits(
    owner: Pool, role: str, pg_conninfo: str, tmp_path: Path
) -> None:
    with owner.connection() as conn:
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(_schema(conn)), sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON scadbuddy_migrations TO {}").format(sql.Identifier(role))
        )
    store = _store(make_conninfo(pg_conninfo, user=role, password=role), tmp_path)
    looks = 0

    def stopping() -> bool:
        nonlocal looks
        looks += 1
        return looks > 3

    try:
        with pytest.raises(WorkerStoppedError):
            open_for_caller(store, "print", stopping=stopping)
    finally:
        store.close()


def test_a_worker_on_the_apis_own_role_still_migrates(pg_conninfo: str, tmp_path: Path) -> None:
    store = _store(pg_conninfo, tmp_path)
    open_for_caller(store, "print")
    try:
        with store.pool.connection() as conn:
            assert owns_schema(conn)
            row = conn.execute("SELECT to_regclass('workflow_runs') AS found").fetchone()
            assert row is not None and row["found"] is not None
    finally:
        store.close()


def test_the_projects_worker_has_no_list_and_no_role() -> None:
    """#5298: the projects worker holds no database; its flow runs, decisions, grants,
    operations and payload keys go through the CRUD routes, and the API takes back what
    its role held (`auth/revoke.py`, `tests/auth/test_revoke.py`)."""
    assert "projects" not in GRANTS and "projects" not in ROLES and "projects" not in VIEWS
