"""#5210: each caller's own role holds exactly its list (`auth/caller_grants.py`). The
print worker's list is empty (#5298 Task 31): it holds no pool."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.auth.caller_grants import GRANTS, SEQUENCES, grant_callers
from scadbuddy.auth.service_credentials import ROLES, VIEWS, ServiceCaller
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.render.pg_store import Migration, migrate
from scadbuddy.render.worker_role import grant_role
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


#: Every table the print worker's role was ever granted (#5210), each now behind a CRUD
#: route (#5298 Tasks 23 to 31), and the migrations ledger it waited on.
PRINT_RETIRED = (
    "scadbuddy_migrations",
    "settings",
    "model_print_choices",
    "operations",
    "print_runs",
    "print_outcomes",
    "events",
    "output_bambuddy_uploads",
    "output_bambuddy_slices",
    "print_slice_starts",
    "print_preview_slices",
    "folderless_projects",
    "destination_touches",
    "project_print_targets",
    "print_links",
    "print_sends",
    "output_last_prints",
    "printers",
    "printer_reports",
    "printer_bed_types",
    "activity",
    "rack_nozzle_seen",
    "rack_nozzle_picks",
    "rack_nozzle_pick_spools",
    "rack_nozzle_prints",
    # #5298 Task 31: the spools, the native spools, the kept presets and native slices.
    "physical_spools",
    "spool_provider_records",
    "spool_record_links",
    "spool_provider_refreshes",
    "tray_spool_links",
    "native_spools",
    "native_spool_uses",
    "held_local_presets",
    "native_slices",
)


def test_a_new_print_role_reads_no_table(owner: Pool, role: str, pg_conninfo: str) -> None:
    """#5298 Task 31: the print worker holds no pool, so a role granted its list reads
    none of the tables it once held, nor the sequence it inserted with."""
    _grant(owner, role, "print")
    pool = _pool(make_conninfo(pg_conninfo, user=role, password=role))
    try:
        with pool.connection() as conn:
            for table in PRINT_RETIRED:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(sql.SQL("SELECT 1 FROM {}").format(sql.Identifier(table)))
                conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT nextval('native_slices_id_seq')")
    finally:
        pool.close()


def test_the_projects_worker_has_no_list_and_no_role() -> None:
    """#5298: the projects worker holds no database; its flow runs, decisions, grants,
    operations and payload keys go through the CRUD routes, and the API takes back what
    its role held (`auth/revoke.py`, `tests/auth/test_revoke.py`)."""
    assert "projects" not in GRANTS and "projects" not in ROLES and "projects" not in VIEWS
