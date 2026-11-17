"""The reads the Filament section needs (#6262, spec §5.5): a spool's detail, the grams
basis, the ledger page, the tags and the summary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.ledger import basis, grams_left, record
from scadbuddy.filament.models import (
    Material,
    SpoolCreate,
    SpoolEvent,
    SpoolPatch,
    SpoolTypeCreate,
    SpoolTypePatch,
)
from scadbuddy.filament.spools import FilamentDefaults, FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.spools.models import SeenTray
from tests.conftest import PgPool, open_pg_pool

pytestmark = pytest.mark.requires_postgres


def _material(pool: PgPool, name: str = "PLA") -> UUID:
    return next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == name
    )


def _spool(pool: PgPool, **fields: Any) -> tuple[FilamentSpoolStore, UUID]:
    store = FilamentSpoolStore(pool)
    made = store.create(SpoolCreate(material_id=_material(pool), **fields), actor="ann")
    return store, made.id


def test_detail_names_the_last_tag_and_its_holder(pg_pool: PgPool) -> None:
    store, a = _spool(pg_pool)
    _, b = _spool(pg_pool)
    store.bind_tag(a, tag_uid="04AABBCCDD", kind="ntag_openspool", actor="ann")
    detail = store.detail(a)
    assert detail is not None
    assert detail.tag is not None
    assert detail.tag.tag_uid == "04AABBCCDD"
    assert detail.last_tag is None
    store.archive(a, True, actor="ann")
    store.bind_tag(b, tag_uid="04AABBCCDD", kind="ntag_openspool", actor="ann")
    detail = store.detail(a)
    assert detail is not None
    assert detail.tag is None
    assert detail.last_tag is not None
    assert detail.last_tag.reason == "archived"
    spool_b = store.get(b)
    assert spool_b is not None
    assert detail.last_tag.held_by == spool_b.number
    store.archive(a, False, actor="ann")
    restored = store.detail(a)
    assert restored is not None
    assert restored.tag is None
    assert store.detail(UUID(int=9)) is None


def _event(minute: int, kind: str, source: str | None, **over: Any) -> SpoolEvent:
    values: dict[str, Any] = {
        "id": UUID(int=minute + 1),
        "spool_id": UUID(int=1),
        "at": datetime(2026, 11, 14, 12, tzinfo=UTC) + timedelta(minutes=minute),
        "kind": kind,
        "source": source,
        "printer_id": None,
        "ams_id": None,
        "tray_id": None,
        "grams_before": None,
        "grams_after": None,
        "delta_g": None,
        "ref_kind": None,
        "ref_id": None,
        "actor": None,
        "data": {},
        "seq": minute + 1,
    }
    return SpoolEvent.model_validate(values | over)


def test_basis_matches_grams_left() -> None:
    events = [
        _event(1, "weighed", "scale", grams_after=800),
        _event(2, "print_ended", "print_actual", delta_g=-50, ref_kind="print_run", ref_id="r"),
        _event(3, "ams_reading", "ams_remain", grams_after=700),
    ]
    found = basis(events, weight_locked=True, label_weight_g=1000)
    assert found.anchor is not None
    assert found.anchor.source == "scale"
    assert [e.delta_g for e in found.deltas] == [-50]
    assert grams_left(events, weight_locked=True, label_weight_g=1000) == 750
    assert basis([], weight_locked=False, label_weight_g=None).anchor is None


def test_detail_carries_the_basis_and_names(pg_pool: PgPool) -> None:
    store, spool_id = _spool(pg_pool, grams_left=800)
    detail = store.detail(spool_id)
    assert detail is not None
    assert detail.basis.anchor is not None
    assert detail.basis.anchor.grams_after == 800
    assert detail.basis.deltas == []
    assert detail.presets == []
    assert detail.k_values == []
    assert detail.names.material == "PLA"
    assert detail.names.brand is None


def _at_one_time(pool: PgPool, spool_id: UUID, count: int) -> datetime:
    at = datetime(2026, 11, 1, 12, tzinfo=UTC)
    with pool.connection() as conn:
        for _ in range(count):
            conn.execute(
                "INSERT INTO filament_spool_events (spool_id, at, kind, source, delta_g)"
                " VALUES (%s, %s, 'print_ended', 'print_actual', -1)",
                (spool_id, at),
            )
    return at


def test_events_page_by_at_and_seq(pg_pool: PgPool) -> None:
    store, spool_id = _spool(pg_pool)
    _at_one_time(pg_pool, spool_id, 3)
    seen: list[UUID] = []
    cursor = None
    pages = 0
    while True:
        page = store.ledger(spool_id=spool_id, kind="print_ended", cursor=cursor, limit=2)
        seen.extend(row.id for row in page.events)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert pages == 2
    assert len(seen) == 3
    assert len(set(seen)) == 3
    with pg_pool.connection() as conn:
        booked = conn.execute(
            "SELECT id FROM filament_spool_events WHERE spool_id = %s AND kind = 'print_ended'"
            " ORDER BY seq DESC",
            (spool_id,),
        ).fetchall()
    assert seen == [row["id"] for row in booked]
    everything = store.ledger(spool_id=spool_id, limit=500)
    assert [r.id for r in everything.events if r.kind == "print_ended"] == seen
    spool = store.get(spool_id)
    assert spool is not None
    assert everything.events[0].spool_number == spool.number
    assert everything.next_cursor is None


def test_events_filter_by_source_and_since(pg_pool: PgPool) -> None:
    store, spool_id = _spool(pg_pool, grams_left=500)
    at = _at_one_time(pg_pool, spool_id, 1)
    only = store.ledger(source="print_actual", since=at)
    assert [r.kind for r in only.events] == ["print_ended"]
    assert store.ledger(source="print_actual", since=at + timedelta(days=1)).events == []


def test_events_filter_by_until_which_is_excluded(pg_pool: PgPool) -> None:
    store, spool_id = _spool(pg_pool, grams_left=500)
    at = _at_one_time(pg_pool, spool_id, 1)
    printed = store.ledger(source="print_actual")
    assert len(printed.events) == 1
    assert store.ledger(source="print_actual", until=at).events == []
    assert len(store.ledger(source="print_actual", until=at + timedelta(seconds=1)).events) == 1
    # since is included and until is not: [since, until) tiles.
    day = timedelta(days=1)
    assert len(store.ledger(source="print_actual", since=at, until=at + day).events) == 1
    assert store.ledger(source="print_actual", since=at + day, until=at + 2 * day).events == []


def test_low_stock_uses_the_spool_then_the_default(pg_pool: PgPool) -> None:
    store, spool_id = _spool(pg_pool, label_weight_g=1000, grams_left=150, low_stock_pct=10)
    spool = store.get(spool_id)
    assert spool is not None
    assert spool.grams_left == 150
    assert spool.low_stock is False
    updated = store.update(spool_id, SpoolPatch(low_stock_pct=None), actor="ann")
    assert updated is not None
    assert updated.low_stock is True
    unknown = store.create(SpoolCreate(material_id=_material(pg_pool)), actor="ann")
    assert unknown.low_stock is False


def test_low_stock_reads_the_setting(pg_pool: PgPool) -> None:
    percent = 20
    store = FilamentSpoolStore(pg_pool, lambda: FilamentDefaults(percent, None))
    made = store.create(
        SpoolCreate(material_id=_material(pg_pool), label_weight_g=1000, grams_left=150),
        actor="ann",
    )
    assert made.low_stock is True
    percent = 10
    spool = store.get(made.id)
    assert spool is not None
    assert spool.low_stock is False
    assert store.summary().low_stock == 0


def test_the_summary_values_a_spool_with_no_cost_at_the_default_cost(pg_pool: PgPool) -> None:
    cost: Decimal | None = Decimal("24.50")
    store = FilamentSpoolStore(pg_pool, lambda: FilamentDefaults(20, cost))
    store.create(
        SpoolCreate(material_id=_material(pg_pool), label_weight_g=1000, grams_left=500),
        actor="ann",
    )
    assert store.summary().value_left == Decimal("12.25")
    cost = None
    assert store.summary().value_left is None


def test_no_read_of_the_settings_holds_a_pool_connection(pg_conninfo: str, pg_pool: PgPool) -> None:
    """A reader that takes a second connection from the one pool would deadlock a busy
    pool, so the store reads before it takes its own."""
    material = _material(pg_pool)
    small = open_pg_pool(pg_conninfo, size=1)
    try:

        def defaults() -> FilamentDefaults:
            with small.connection(timeout=3):
                return FilamentDefaults(20, None)

        store = FilamentSpoolStore(small, defaults)
        made = store.create(SpoolCreate(material_id=material, grams_left=100), actor="ann")
        assert store.list()
        assert store.get(made.id) is not None
        assert store.detail(made.id) is not None
        assert store.summary().spools == 1
        assert store.update(made.id, SpoolPatch(low_stock_pct=5), actor="ann") is not None
        printer = uuid4()
        with small.connection() as conn:
            conn.execute(
                "INSERT INTO printers (id, serial, name) VALUES (%s, %s, 'Garage')",
                (printer, f"SN{printer.hex[:8]}"),
            )
        linked = FilamentInventory(small, defaults).link(
            printer,
            0,
            1,
            made.id,
            SeenTray(ams_id=0, tray_id=1, material="PLA", colour="#FF0000FF"),
            actor="ann",
        )
        assert linked.id == made.id
    finally:
        small.close()


def test_in_use_follows_print_events(pg_pool: PgPool) -> None:
    store, spool_id = _spool(pg_pool)
    detail = store.detail(spool_id)
    assert detail is not None
    assert detail.in_use is False
    with pg_pool.connection() as conn, conn.transaction():
        record(conn, spool_id, "print_started", source="print_estimate", delta_g=-5)
    detail = store.detail(spool_id)
    assert detail is not None
    assert detail.in_use is True


def test_tags_list_and_detail(pg_pool: PgPool) -> None:
    store, a = _spool(pg_pool)
    store.bind_tag(a, tag_uid="04AA", kind="ntag_openspool", actor="ann")
    store.bind_tag(a, tag_uid="04BB", kind="ntag_openspool", actor="ann")
    bound = store.tags()
    assert len(bound) == 1
    assert bound[0].tag_uid == "04BB"
    assert bound[0].binding is not None
    assert bound[0].spool_number == 1
    everything = store.tags(include_unbound=True)
    assert len(everything) == 2
    old = next(t for t in everything if t.binding is None)
    assert old.spool_number is None
    detail = store.tag(old.id)
    assert detail is not None
    assert [h.reason for h in detail.history] == ["rebound"]
    assert store.tag(UUID(int=9)) is None


def test_summary_counts(pg_pool: PgPool) -> None:
    _spool(pg_pool, label_weight_g=1000, grams_left=100, cost_per_kg=20)
    store = FilamentSpoolStore(pg_pool)
    store.create(SpoolCreate(material_id=_material(pg_pool), label_weight_g=1000), actor="ann")
    summary = store.summary()
    assert summary.spools == 2
    assert summary.grams_left == 100
    assert summary.value_left is not None
    assert float(summary.value_left) == 2.0
    assert summary.low_stock == 1
    assert summary.needs_type == 2
    assert summary.loaded == 0
    assert [(r.material, r.spools) for r in summary.by_material] == [("PLA", 2)]


def test_a_types_label_weight_edit_moves_its_spools_stored_grams(pg_pool: PgPool) -> None:
    types = SpoolTypeStore(pg_pool)
    kind = types.create(
        SpoolTypeCreate(
            code="PLA-W", material_id=_material(pg_pool), label_weight_g=1000, rgba="#FFFFFFFF"
        )
    )
    store = FilamentSpoolStore(pg_pool)
    spool = store.create(SpoolCreate(type_id=kind.id), actor="ann")
    with pg_pool.connection() as conn, conn.transaction():
        record(conn, spool.id, "ams_reading", source="ams_remain", data={"remain_pct": 50})
    before = store.get(spool.id)
    assert before is not None
    assert before.grams_left == 500
    types.update(kind.id, SpoolTypePatch(label_weight_g=2000))
    after = store.get(spool.id)
    assert after is not None
    assert after.grams_left == 1000
