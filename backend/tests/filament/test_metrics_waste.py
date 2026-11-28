"""Non-print waste apart from print waste (#7167): the ``non_print_waste`` events are
their own figure, never in ``wasted_g`` or ``used_g``."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from scadbuddy.core.metrics import Metrics
from scadbuddy.filament import metrics_export
from scadbuddy.filament.metrics_models import Snapshot
from scadbuddy.filament.spools import FilamentSpoolStore
from tests.conftest import PgPool
from tests.filament.test_metrics_store import _bb_end, _event, _spool, _store, _type

pytestmark = pytest.mark.requires_postgres

DAY = datetime(2026, 11, 5, 12, tzinfo=UTC)


def _waste(pool: PgPool, spool: UUID, grams: float, at: datetime = DAY) -> None:
    _event(
        pool, spool, "non_print_waste", source="person", absolute_g=0, at=at,
        data={"waste_g": grams, "cause": "archive"},
    )  # fmt: skip


def test_non_print_waste_is_not_in_wasted_g_or_used_g(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, grams_left=900)
    _bb_end(pg_pool, spool, DAY, 10, "a", status="failed")
    before = _store(pg_pool).history(spool)
    _waste(pg_pool, spool, 50.5)
    found = _store(pg_pool).history(spool)
    assert before is not None and found is not None
    assert found.metrics.wasted_g == 10
    assert found.metrics.used_g == before.metrics.used_g
    assert found.metrics.non_print_waste_g == 50.5
    assert before.metrics.non_print_waste_g == 0


def test_inventory_waste_rows_do_not_change_rate(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, grams_left=900)
    _bb_end(pg_pool, spool, DAY, 10, "a", status="failed")
    _bb_end(pg_pool, spool, DAY, 30, "b")
    args = (date(2026, 11, 1), date(2026, 11, 30))
    before = _store(pg_pool).inventory("material", "month", *args)
    _waste(pg_pool, spool, 50)
    after = _store(pg_pool).inventory("material", "month", *args)
    assert after.waste == before.waste
    assert after.consumption == before.consumption
    assert before.non_print_waste_g == 0
    assert before.non_print_waste == []
    assert after.non_print_waste_g == 50


def test_non_print_waste_by_material_and_type_in_range(pg_pool: PgPool) -> None:
    typed = _type(pg_pool)
    one = _spool(pg_pool, type_id=typed)
    two = _spool(pg_pool)
    old = _spool(pg_pool)
    _waste(pg_pool, one, 20)
    _waste(pg_pool, two, 5.5)
    _waste(pg_pool, old, 999, at=datetime(2026, 9, 1, tzinfo=UTC))
    FilamentSpoolStore(pg_pool).archive(two, True, actor="ann")
    found = _store(pg_pool).inventory("material", "month", date(2026, 11, 1), date(2026, 11, 30))
    assert found.non_print_waste_g == 25.5
    rows = {(r.dimension, r.label): r.wasted_g for r in found.non_print_waste}
    assert rows[("material", "PLA")] == 25.5
    assert rows[("type", "PLA-WHT")] == 20
    assert all(r.dimension in ("material", "type") for r in found.non_print_waste)
    assert 999 not in rows.values()


def test_snapshot_sums_waste_by_material_with_archived_spools(pg_pool: PgPool) -> None:
    one = _spool(pg_pool)
    two = _spool(pg_pool)
    _waste(pg_pool, one, 20)
    _waste(pg_pool, two, 5.5)
    _waste(pg_pool, two, 4, at=datetime(2026, 9, 1, tzinfo=UTC))
    FilamentSpoolStore(pg_pool).archive(two, True, actor="ann")
    assert _store(pg_pool).snapshot().non_print_waste == {"PLA": 29.5}


def test_export_has_a_separate_column() -> None:
    body: dict[str, object] = {
        "stock": [], "used": {("PLA", "failed"): 5.0}, "burn": {}, "days_of_stock": {},
        "low_stock": {}, "reorder": {}, "currency": "USD", "non_print_waste": {"PLA": 50.0},
    }  # fmt: skip
    metrics = Metrics()
    metrics_export.export(metrics, Snapshot.model_validate(body))
    text = metrics.exposition().decode()
    assert 'scadbuddy_filament_non_print_waste_grams{material="PLA"} 50.0' in text
    assert 'scadbuddy_filament_used_grams{material="PLA",outcome="failed"} 5.0' in text
    assert 'outcome="non_print_waste"' not in text
