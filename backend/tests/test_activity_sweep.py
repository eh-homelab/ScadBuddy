"""Activity retention and repair (spec 2026-10-09 §6): the housekeeping sweep's work."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy import main
from scadbuddy.activity.index import ACTIVITY_PRUNE_MARGIN, ActivityIndex
from scadbuddy.activity.models import ActivityRow
from scadbuddy.library.settings_store import (
    RETENTION_MAX_SECONDS,
    SettingsPatch,
    StoredSettings,
)
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


def test_a_retention_stored_past_the_maximum_is_read_as_it() -> None:
    """A row saved before the bound (656868686867 s) cannot put the prune out of range."""
    stored = StoredSettings.model_validate(
        {
            "activity_ttl_seconds": 656868686867.0,
            "print_run_retention_seconds": 656868686867.0,
            "operation_retention_seconds": 7 * DAY,
        }
    )
    assert stored.activity_ttl_seconds == RETENTION_MAX_SECONDS
    assert stored.print_run_retention_seconds == RETENTION_MAX_SECONDS
    assert stored.operation_retention_seconds == 7 * DAY
    with pytest.raises(ValueError):
        SettingsPatch(activity_ttl_seconds=RETENTION_MAX_SECONDS + 1)


async def test_prune_with_the_maximum_ttl_is_in_range(index: ActivityIndex) -> None:
    assert await index.prune(RETENTION_MAX_SECONDS, DAY) == 0


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


async def test_repair_keeps_the_sources_timestamps(
    pg_pool: PgPool, index: ActivityIndex, projection: JobProjection
) -> None:
    job = make_job(state="pending")
    projection.accept(job, "k3", workflow_id="render-k3", run_id="run-3")
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE render_jobs SET created_at = now() - interval '20 hours',"
            " finished_at = now() - interval '19 hours', state = 'done'"
        )
        source = conn.execute("SELECT created_at, finished_at FROM render_jobs").fetchone()
    assert source is not None
    assert await index.repair(DAY) >= 1
    got = await index.get(f"render:{job.id}")
    assert got is not None
    assert (got.created_at, got.finished_at) == (source["created_at"], source["finished_at"])


async def test_a_pruned_row_is_not_restored_when_the_window_fits_the_ttl(
    pg_pool: PgPool, index: ActivityIndex, projection: JobProjection
) -> None:
    job = make_job(state="pending")
    projection.accept(job, "k4", workflow_id="render-k4", run_id="run-4")
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE render_jobs SET created_at = now() - interval '30 hours',"
            " finished_at = now() - interval '29 hours', state = 'done'"
        )
    ttl = DAY
    window = min(7 * DAY, ttl - ACTIVITY_PRUNE_MARGIN)  # what the sweep passes
    assert await index.repair(window) == 0  # older than the window
    assert await index.prune(ttl, window) == 0
    assert await index.get(f"render:{job.id}") is None


async def test_repair_leaves_a_row_that_is_no_longer_a_placeholder(
    pg_pool: PgPool, index: ActivityIndex, projection: JobProjection
) -> None:
    job = make_job(state="pending")
    projection.accept(job, "k5", workflow_id="render-k5", run_id="run-5")
    seed_row = ActivityRow(
        id=f"render:{job.id}",
        kind="render",
        status="succeeded",
        title="landed",
        source="render_jobs",
        source_id=job.id,
    )
    with pg_pool.connection() as conn:
        index.upsert_in(conn, seed_row)
    assert await index.repair(DAY) == 0
    got = await index.get(f"render:{job.id}")
    assert got is not None and (got.status, got.title) == ("succeeded", "landed")


async def test_one_bad_source_row_does_not_stop_the_rest(
    pg_pool: PgPool, index: ActivityIndex, projection: JobProjection
) -> None:
    good = make_job(state="pending")
    projection.accept(good, "k6", workflow_id="render-k6", run_id="run-6")
    with pg_pool.connection() as conn:
        conn.execute("ALTER TABLE print_runs DROP COLUMN slug")
    try:
        assert await index.repair(DAY) == 1  # the print_runs query fails; renders restored
    finally:
        with pg_pool.connection() as conn:
            conn.execute("ALTER TABLE print_runs ADD COLUMN slug text")
    assert await index.get(f"render:{good.id}") is not None


class RecordingIndex:
    def __init__(self, fail_repair: bool = False) -> None:
        self.calls: list[tuple[str, float, float | None]] = []
        self.fail_repair = fail_repair

    async def repair(self, window: float) -> int:
        self.calls.append(("repair", window, None))
        if self.fail_repair:
            raise RuntimeError("boom")
        return 0

    async def prune(self, ttl: float, placeholder_ttl: float) -> int:
        self.calls.append(("prune", ttl, placeholder_ttl))
        return 0


def sweep_state(stored: StoredSettings, index: RecordingIndex) -> Any:
    components = SimpleNamespace(get=lambda key: SimpleNamespace(index=index))
    return SimpleNamespace(
        settings_store=SimpleNamespace(load=lambda: stored), components=components
    )


async def test_the_sweep_repairs_then_prunes_with_the_defaults() -> None:
    index = RecordingIndex()
    await main._sweep_activity_logged(sweep_state(StoredSettings(), index))
    assert index.calls == [("repair", DAY, None), ("prune", 30 * DAY, DAY)]


async def test_the_sweep_reads_the_settings_each_run() -> None:
    stored = StoredSettings(activity_ttl_seconds=7 * DAY, activity_repair_window_seconds=3600)
    index = RecordingIndex()
    state = sweep_state(stored, index)
    await main._sweep_activity_logged(state)
    stored.activity_ttl_seconds = 2 * DAY
    stored.activity_repair_window_seconds = 5 * DAY  # longer than the TTL: clamped inside it
    await main._sweep_activity_logged(state)
    assert index.calls == [
        ("repair", 3600, None),
        ("prune", 7 * DAY, 3600),
        ("repair", 2 * DAY - ACTIVITY_PRUNE_MARGIN, None),
        ("prune", 2 * DAY, 2 * DAY - ACTIVITY_PRUNE_MARGIN),
    ]


async def test_the_sweep_prunes_even_when_repair_fails() -> None:
    index = RecordingIndex(fail_repair=True)
    with pytest.raises(RuntimeError):
        await main._sweep_activity_logged(sweep_state(StoredSettings(), index))
    assert [call[0] for call in index.calls] == ["repair", "prune"]


async def test_the_sweep_prunes_nothing_when_the_settings_cannot_be_read() -> None:
    def broken() -> StoredSettings:
        raise RuntimeError("database down")

    index = RecordingIndex()
    state = sweep_state(StoredSettings(), index)
    state.settings_store.load = broken
    with pytest.raises(RuntimeError, match="database down"):
        await main._sweep_activity_logged(state)
    assert index.calls == []


def test_a_repair_commits_each_row_it_restores(pg_pool: PgPool, index: ActivityIndex) -> None:
    """A restore takes the event log's lock, held to commit: one transaction over the
    whole pass would hold every publisher up until its end. Each row commits as it goes,
    so the next row's build already sees the last one from another connection."""
    with pg_pool.connection() as conn:
        for run in ("p1", "p2"):
            conn.execute(
                "INSERT INTO print_runs (id, output_id, idempotency_key, status, workflow_id,"
                " workflow_run_id, slug) VALUES (%s, 'abc', %s, 'running', %s, 'r', 'demo')",
                (run, f"k-{run}", f"print-{run}"),
            )
    seen: list[int] = []

    def build(found: Any) -> ActivityRow:
        with pg_pool.connection() as probe:
            count = probe.execute(
                "SELECT count(*) AS n FROM activity WHERE id LIKE 'print_run:%'"
            ).fetchone()
        seen.append(0 if count is None else count["n"])
        return ActivityRow(
            id=f"print_run:{found['id']}",
            kind="print_run",
            status="running",
            title="t",
            source="print_runs",
            source_id=found["id"],
            created_at=found["created_at"],
            finished_at=found["finished_at"],
        )

    with pg_pool.connection() as conn:
        # What this relies on: the repair's own pool commits each row's transaction.
        assert conn.autocommit, "the repair's pool is no longer autocommit"
    restored = index._repair_source(
        DAY, "print_run:", "print_runs", "r.id, r.created_at, r.finished_at", build
    )
    assert restored == 2
    assert seen == [0, 1]
