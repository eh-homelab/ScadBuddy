"""Activity retention and repair (spec 2026-10-09 §6): the housekeeping sweep's work."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.activity.models import ActivityRow
from scadbuddy.library.settings_store import SettingsPatch, StoredSettings
from scadbuddy.operations.store import OperationStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.housekeeping import ACTIVITY_PATCH, ACTIVITY_SWEEP, SWEEPS
from tests.conftest import PgPool
from tests.test_activity_agent_ingest import AgentTables, agent_tables  # noqa: F401
from tests.test_activity_render import make_job

pytestmark = pytest.mark.requires_postgres

DAY = 86400.0


@pytest.fixture
def index(pg_pool: PgPool) -> ActivityIndex:
    return ActivityIndex(pg_pool, events=None)


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        yield projection
    finally:
        projection.close()


def seed(pg_pool: PgPool, index: ActivityIndex, id: str, status: str) -> None:
    with pg_pool.connection() as conn:
        index.upsert_in(
            conn,
            ActivityRow(
                id=id,
                kind="render",
                status=status,  # type: ignore[arg-type]
                title="t",
                source="render_jobs",
                source_id=id.split(":")[1],
            ),
        )


def age(pg_pool: PgPool, id: str, interval: str) -> None:
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE activity SET created_at = now() - %s::interval,"
            " finished_at = CASE WHEN finished_at IS NULL THEN NULL"
            " ELSE now() - %s::interval END WHERE id = %s",
            (interval, interval, id),
        )


def test_sweep_is_last_and_patched() -> None:
    assert SWEEPS[-1] == ACTIVITY_SWEEP == "housekeeping_activity"
    assert ACTIVITY_PATCH == "housekeeping-activity"


def test_settings_fields() -> None:
    stored = StoredSettings()
    assert stored.activity_ttl_seconds is None
    assert stored.activity_repair_window_seconds is None
    assert stored.activity_follow is None
    with pytest.raises(ValueError):
        SettingsPatch(activity_ttl_seconds=100)
    with pytest.raises(ValueError):
        SettingsPatch(activity_repair_window_seconds=10)
    with pytest.raises(ValueError):
        SettingsPatch(activity_repair_window_seconds=8 * DAY)
    assert SettingsPatch(activity_follow=["browser"]).activity_follow == ["browser"]


async def test_prune_deletes_old_finished_only(pg_pool: PgPool, index: ActivityIndex) -> None:
    seed(pg_pool, index, "render:old", "succeeded")
    seed(pg_pool, index, "render:fresh", "succeeded")
    seed(pg_pool, index, "render:open", "running")
    age(pg_pool, "render:old", "40 days")
    age(pg_pool, "render:open", "40 days")
    assert await index.prune(30 * DAY, DAY) == 1
    assert await index.get("render:old") is None
    assert await index.get("render:fresh") is not None
    assert await index.get("render:open") is not None


async def test_prune_drops_stale_placeholders(pg_pool: PgPool, index: ActivityIndex) -> None:
    await index.attribute("render:ghost", principal="browser", session=None, via_tool=None)
    await index.attribute("render:new", principal="browser", session=None, via_tool=None)
    age(pg_pool, "render:ghost", "2 days")
    assert await index.prune(30 * DAY, DAY) == 1
    with pg_pool.connection() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM activity").fetchall()]
    assert ids == ["render:new"]


async def test_repair_fills_an_operation_placeholder(
    pg_pool: PgPool, index: ActivityIndex, projection: JobProjection
) -> None:
    await index.attribute("operation:o8", principal="token:t1", session=None, via_tool=None)
    store = OperationStore(projection.pool)  # no activity: the source's own write is lost
    await store.insert(
        "o8",
        kind="model_import",
        subject="k",
        operation_key="k8",
        request={},
        workflow_id="w",
        workflow_run_id="r",
        retention=None,
    )
    assert await index.get("operation:o8") is None  # still a placeholder
    assert await index.repair(DAY) >= 1
    got = await index.get("operation:o8")
    assert got is not None and got.source == "operations"
    assert (got.principal, got.kind, got.workflow_id, got.run_id) == (
        "token:t1",
        "operation.model_import",
        "w",
        "r",
    )


async def test_repair_restores_a_missed_operation(
    index: ActivityIndex, projection: JobProjection
) -> None:
    store = OperationStore(projection.pool)
    await store.insert(
        "o9",
        kind="model_import",
        subject="k",
        operation_key="k",
        request={},
        workflow_id="w",
        workflow_run_id="r",
        retention=None,
    )
    assert await index.get("operation:o9") is None
    assert await index.repair(DAY) >= 1
    got = await index.get("operation:o9")
    assert got is not None and got.principal is None
    assert await index.repair(DAY) == 0  # nothing left to restore


async def test_repair_restores_a_render(index: ActivityIndex, projection: JobProjection) -> None:
    job = make_job(state="pending")
    projection.accept(job, "k1", workflow_id="render-k1", run_id="run-1")
    assert await index.get(f"render:{job.id}") is None
    assert await index.repair(DAY) >= 1
    got = await index.get(f"render:{job.id}")
    assert got is not None and (got.kind, got.status) == ("render", "queued")
    assert got.workflow_id == "render-k1"


async def test_repair_restores_a_print_run(index: ActivityIndex, pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, workflow_id,"
            " workflow_run_id, slug) VALUES ('p1', 'abc', 'k', 'running', 'print-k', 'w1', 'demo')"
        )
    await index.attribute("print_run:p1", principal="token:t2", session=None, via_tool=None)
    assert await index.repair(DAY) >= 1
    got = await index.get("print_run:p1")
    assert got is not None
    assert (got.kind, got.principal, got.title, got.workflow_id) == (
        "print_run",
        "token:t2",
        "Print demo",
        "print-k",
    )


async def test_repair_ignores_rows_outside_the_window(
    pg_pool: PgPool, index: ActivityIndex, projection: JobProjection
) -> None:
    job = make_job(state="pending")
    projection.accept(job, "k2", workflow_id="render-k2", run_id="run-2")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE render_jobs SET created_at = now() - interval '3 days'")
    assert await index.repair(DAY) == 0
    assert await index.get(f"render:{job.id}") is None


async def test_repair_ingests_agent_operations(
    index: ActivityIndex,
    agent_tables: AgentTables,  # noqa: F811
) -> None:
    agent_tables.insert_operation(
        id="a7",
        kind="plugin_package_install",
        subject="x",
        status="running",
        principal="browser",
        via_tool=None,
        request={},
    )
    assert await index.repair(DAY) >= 1
    got = await index.get("agent_operation:a7")
    assert got is not None and got.kind == "agent.plugin_package_install"


async def test_repair_without_agent_tables_is_fine(index: ActivityIndex) -> None:
    assert await index.repair(DAY) == 0
