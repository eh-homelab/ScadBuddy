"""What one process may take of Postgres's connection slots (#5226).

On 2026-11-03 prod's API pods held 23 connections each, almost all idle, and a rolling
update's surge pod found no slot left. These tests sum an API process's pools, so a new
pool, or a bigger one, changes a number here on purpose rather than quietly.
"""

from __future__ import annotations

import ast
import gc
import re
import uuid
from collections import Counter
from pathlib import Path

import psycopg
import pytest
from prometheus_client.parser import text_string_to_metric_families

import scadbuddy
from scadbuddy.api.deps import build_state
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.pg_pool import IDLE_SECONDS, pools, row_pool
from scadbuddy.core.settings import Settings
from tests.conftest import UNUSED_TEMPORAL_ADDRESS

#: What an API process may hold beyond ``SCADBUDDY_DATABASE_POOL_SIZE``: its pools by
#: name, at their most. README "Running it" says the same.
API_POOLS = {
    "scadbuddy-events": 2,
    "scadbuddy-rows": 6,
    "scadbuddy-auth": 2,
    "scadbuddy-rack-usage": 2,
    "scadbuddy-listener": 1,
}
API_FIXED = 13
#: psycopg_pool's classes (httpcore's ``AsyncConnectionPool`` is HTTP, not Postgres).
POOL_CLASSES = {"ConnectionPool", "AsyncConnectionPool", "NullConnectionPool"}


def _api_pools(pool_size: int) -> Counter[str]:
    url = f"postgresql://nobody@127.0.0.1:1/budget_{uuid.uuid4().hex}"
    settings = Settings(
        frontend_dir=Path("/nonexistent"),
        database_url=url,
        database_pool_size=pool_size,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
    )
    state = build_state(settings)  # builds every component; connects to nothing
    totals: Counter[str] = Counter()
    for use in pools():
        if use.conninfo == url:
            totals[use.name] += use.max_size
    del state
    gc.collect()
    return totals


@pytest.mark.parametrize("pool_size", [10, 3])
def test_an_api_process_holds_its_pool_size_plus_a_fixed_few(pool_size: int) -> None:
    totals = _api_pools(pool_size)
    assert totals == {"scadbuddy-api": pool_size, **API_POOLS}
    assert sum(API_POOLS.values()) == API_FIXED
    assert totals.total() == pool_size + API_FIXED


def test_no_module_makes_a_pool_of_its_own() -> None:
    """Every pool goes through `core/pg_pool.py`, so the registry, the idle timeout and the
    budget above see it."""
    root = Path(scadbuddy.__file__).parent
    found = []
    for path in sorted(root.rglob("*.py")):
        if path == root / "core" / "pg_pool.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                http = isinstance(func, ast.Attribute) and ast.unparse(func.value) == "httpcore"
                if not http and name in POOL_CLASSES:
                    found.append(f"{path.relative_to(root)}:{node.lineno}")
    assert found == []


@pytest.mark.requires_postgres
def test_a_pool_closes_what_it_no_longer_uses(
    pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None kept open but what it uses, each named in ``pg_stat_activity``."""
    assert IDLE_SECONDS == 60.0
    name = f"scadbuddy-test-{uuid.uuid4().hex[:8]}"
    pool = row_pool(pg_conninfo, name=name, max_size=2)
    assert pool.min_size == 0 and pool.max_idle == IDLE_SECONDS
    pool.open(wait=True, timeout=30)
    try:
        assert pool.get_stats().get("pool_size", 0) == 0
        with pool.connection() as conn:
            row = conn.execute("SELECT current_setting('application_name') AS name").fetchone()
            assert row is not None and row["name"] == name
        with psycopg.connect(pg_conninfo, autocommit=True) as watcher:
            held = watcher.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (name,)
            ).fetchone()
            assert held == (1,)
        # What `ShrinkPool` runs every `max_idle`: a connection unused all through it goes.
        pool._shrink_pool()
        pool._shrink_pool()
        assert pool.get_stats().get("pool_size", 0) == 0
    finally:
        pool.close()


def test_the_metrics_sum_each_pool() -> None:
    url = f"postgresql://nobody@127.0.0.1:1/metrics_{uuid.uuid4().hex}"
    name = f"scadbuddy-test-{uuid.uuid4().hex[:8]}"
    first = row_pool(url, name=name, max_size=2)
    second = row_pool(url, name=name, max_size=3)
    families = {
        family.name: family
        for family in text_string_to_metric_families(Metrics().exposition().decode())
    }
    most = {
        s.labels["pool"]: s.value for s in families["scadbuddy_db_pool_max_connections"].samples
    }
    now = {s.labels["pool"]: s.value for s in families["scadbuddy_db_pool_connections"].samples}
    waiting = {
        s.labels["pool"]: s.value for s in families["scadbuddy_db_pool_requests_waiting"].samples
    }
    assert most[name] == 5 and now[name] == 0 and waiting[name] == 0
    first.open()
    first.close()  # closed for good: it no longer counts
    exposed = Metrics().exposition().decode()
    assert re.search(rf'scadbuddy_db_pool_max_connections{{pool="{name}"}} 3\.0', exposed)
    second.close()
