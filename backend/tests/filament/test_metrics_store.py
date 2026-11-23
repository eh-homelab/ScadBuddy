"""The metrics store (#6269): a spool's history, the forecast, the inventory metrics,
purchasing and the gauges' snapshot, read from the database into `filament/metrics.py`."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament import ledger
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.metrics_store import MetricsSettings, MetricsStore
from scadbuddy.filament.models import LocationKind, Material, SpoolCreate, SpoolTypeCreate
from scadbuddy.filament.purchasing_models import (
    InvoiceLineIn,
    PricePointCreate,
    ProductCreate,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.conftest import PgPool
from tests.support.purchasing import Shop

pytestmark = pytest.mark.requires_postgres

#: Later than every spool's ``created`` event, so the plates below are after its reading.
NOW = datetime.now(UTC) + timedelta(days=60)


def _material(pool: PgPool) -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, Material) and r.name == "PLA")


def _type(pool: PgPool, code: str = "PLA-WHT", **over: Any) -> UUID:
    body = {"code": code, "material_id": _material(pool), "rgba": "#FFFFFF"}
    return (
        SpoolTypeStore(pool)
        .create(SpoolTypeCreate.model_validate(body | {"label_weight_g": 1000} | over))
        .id
    )


def _store(pool: PgPool, **over: Any) -> MetricsStore:
    spools = FilamentSpoolStore(pool)
    return MetricsStore(pool, spools, lambda: MetricsSettings(**over))


def _spool(pool: PgPool, **over: Any) -> UUID:
    body = SpoolCreate.model_validate({"material_id": _material(pool)} | over)
    return FilamentSpoolStore(pool).create(body, actor="ann").id


def _event(pool: PgPool, spool: UUID, kind: Any, **kw: Any) -> None:
    with pool.connection() as conn:
        assert ledger.record(conn, spool, kind, **kw) is not None


def _sb_end(pool: PgPool, spool: UUID, at: datetime | None, grams: float, job: str) -> None:
    _event(
        pool, spool, "print_ended", source="print_actual", delta_g=-grams, at=at,
        ref_kind="print_run", ref_id=f"run-{job}/{job}/1", data={"job": job, "basis": "sliced"},
    )  # fmt: skip


def _bb_end(
    pool: PgPool,
    spool: UUID,
    at: datetime,
    grams: float,
    job: str,
    status: str = "completed",
    printer: UUID | None = None,
    archive: int | None = None,
) -> None:
    data: dict[str, Any] = {"job": job, "basis": "bb_usage", "bb_status": status}
    if archive is not None:
        data["bb_archive_id"] = archive
    _event(
        pool, spool, "print_ended", source="print_actual", delta_g=-grams, at=at,
        ref_kind="bb_usage", ref_id=str(uuid4()), printer=printer, data=data,
    )  # fmt: skip


# -- history ----------------------------------------------------------------------------


def test_history_links_order_and_archive(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, grams_left=900)
    _sb_end(pg_pool, spool, NOW - timedelta(days=2), 30, "a")
    _bb_end(pg_pool, spool, NOW - timedelta(days=1), 20, "b", archive=41)
    with pg_pool.connection() as conn:
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c1', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o1', 'ORD-1', 'c1', 'USD', '{}')"
        )
        conn.execute(
            "INSERT INTO order_items (id, order_id, position, quantity, title, kind)"
            " VALUES ('i1', 'o1', 0, 1, 'Bracket', 'custom')"
        )
        conn.execute(
            "INSERT INTO order_item_fulfilments (order_item_id, attempt, quantity, outcome,"
            " created_by, print_run_id) VALUES ('i1', 1, 1, 'succeeded', '{}', 'run-a')"
        )
    found = _store(pg_pool).history(spool)
    assert found is not None
    links = {row.event.kind + (row.event.ref_kind or ""): row for row in found.events}
    assert links["print_endedprint_run"].order_id == "o1"
    assert links["print_endedprint_run"].archive_id is None
    assert links["print_endedbb_usage"].archive_id == 41
    assert links["print_endedbb_usage"].order_id is None
    assert links["created"].order_id is None


def test_history_of_a_new_spool(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool)
    found = _store(pg_pool).history(spool)
    assert found is not None
    assert [row.event.kind for row in found.events] == ["created"]
    assert found.series == []
    assert found.metrics.burn_g_per_day is None
    assert found.metrics.empty_on is None
    assert found.type_ams_error is None
    assert found.currency == "USD"


def test_history_of_no_spool_is_none(pg_pool: PgPool) -> None:
    assert _store(pg_pool).history(uuid4()) is None


def test_history_is_newest_first(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, grams_left=900)
    _sb_end(pg_pool, spool, NOW - timedelta(days=3), 10, "a")
    _sb_end(pg_pool, spool, NOW - timedelta(days=3), 10, "b")
    found = _store(pg_pool).history(spool)
    assert found is not None
    keys = [(row.event.at, row.event.seq) for row in found.events]
    assert len(keys) == 3
    assert keys == sorted(keys, reverse=True)


def test_history_has_the_window(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool)
    found = _store(pg_pool).history(spool)
    assert found is not None
    assert found.window_days == 30
    wider = _store(pg_pool, window_days=90).history(spool)
    assert wider is not None
    assert wider.window_days == 90


def test_type_ams_error_spans_the_types_spools(pg_pool: PgPool) -> None:
    type_id = _type(pg_pool)
    one = _spool(pg_pool, type_id=type_id)
    two = _spool(pg_pool, type_id=type_id)
    for spool, ams in ((one, 510.0), (two, 496.0)):
        _event(pg_pool, spool, "ams_reading", source="ams_remain", absolute_g=ams, at=NOW)
        _event(
            pg_pool, spool, "weighed", source="scale", absolute_g=500,
            at=NOW + timedelta(hours=1),
        )  # fmt: skip
    found = _store(pg_pool).history(one)
    assert found is not None
    assert found.metrics.ams_error is not None
    assert found.metrics.ams_error.pairs == 1
    assert found.type_ams_error is not None
    assert found.type_ams_error.pairs == 2
    assert found.type_ams_error.bias_g == pytest.approx(3.0)


# -- forecast ---------------------------------------------------------------------------


def test_forecast_type_reorder(pg_pool: PgPool) -> None:
    timed = _type(pg_pool, "PLA-A", lead_time_days=7, safety_margin_days=3)
    plain = _type(pg_pool, "PLA-B")
    for type_id in (timed, plain):
        spool = _spool(pg_pool, type_id=type_id, grams_left=350)
        _sb_end(pg_pool, spool, NOW - timedelta(days=1), 300, f"j-{type_id}")
    found = _store(pg_pool).forecast(NOW)
    rows = {t.type_id: t for t in found.types}
    assert found.window_days == 30
    row = rows[timed]
    assert row.grams_left == 50
    assert row.burn_g_per_day == pytest.approx(10.0)
    assert row.days_of_stock == pytest.approx(5.0)
    assert row.reorder_now is True
    assert row.reorder_on is not None
    assert row.reorder_on < NOW.date()
    assert (row.lead_time_days, row.safety_margin_days) == (7, 3)
    assert (rows[plain].lead_time_days, rows[plain].safety_margin_days) == (7, 3)
    assert rows[plain].label == "PLA-B"
    assert rows[plain].material == "PLA"
    later = _store(pg_pool, lead_time_days=1, safety_margin_days=0).forecast(NOW)
    assert {t.type_id: t for t in later.types}[plain].reorder_now is False
    assert {t.type_id: t for t in later.types}[timed].reorder_now is True


def test_a_type_with_no_spool_in_stock_is_not_low(pg_pool: PgPool) -> None:
    type_id = _type(pg_pool)
    spool = _spool(pg_pool, type_id=type_id, grams_left=500)
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann")
    [row] = _store(pg_pool).forecast(NOW).types
    assert row.spools == 0
    assert row.low_stock is False


def test_a_type_with_no_use_has_no_forecast(pg_pool: PgPool) -> None:
    type_id = _type(pg_pool)
    spool = _spool(pg_pool, type_id=type_id, grams_left=500)
    found = _store(pg_pool).forecast(NOW)
    [row] = found.types
    assert row.burn_g_per_day is None
    assert row.days_of_stock is None
    assert row.empty_on is None
    assert row.reorder_on is None
    assert row.reorder_now is False
    [own] = found.spools
    assert own.spool_id == spool
    assert own.burn_g_per_day is None
    assert own.empty_on is None


def test_type_burn_uses_all_its_spools(pg_pool: PgPool) -> None:
    type_id = _type(pg_pool)
    one = _spool(pg_pool, type_id=type_id, grams_left=900)
    two = _spool(pg_pool, type_id=type_id, grams_left=900)
    _sb_end(pg_pool, one, NOW - timedelta(days=2), 60, "a")
    _sb_end(pg_pool, two, NOW - timedelta(days=3), 90, "b")
    found = _store(pg_pool).forecast(NOW)
    [row] = found.types
    assert row.spools == 2
    assert row.burn_g_per_day == pytest.approx(150 / 30)
    spools = {s.spool_id: s for s in found.spools}
    assert spools[one].burn_g_per_day == pytest.approx(60 / 30)
    assert spools[two].burn_g_per_day == pytest.approx(90 / 30)


# -- inventory --------------------------------------------------------------------------


def test_inventory_stock_by_supplier(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.type_id()
    shop.confirmed(type_id)
    _spool(pg_pool, grams_left=400)
    found = _store(pg_pool).inventory("supplier", "month", date(2026, 1, 1), date(2026, 12, 31))
    rows = {r.key: r for r in found.stock}
    assert rows[str(shop.supplier_id)].label == "Acme"
    assert rows[str(shop.supplier_id)].spools == 2
    assert rows[None].label == "No supplier"
    assert rows[None].spools == 1
    assert rows[None].grams_left == 400
    assert found.dimension == "supplier"


def test_consumption_buckets_by_month(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, grams_left=900)
    _sb_end(pg_pool, spool, datetime(2026, 10, 3, 12, tzinfo=UTC), 10, "a")
    _bb_end(pg_pool, spool, datetime(2026, 11, 5, 12, tzinfo=UTC), 20, "b", status="failed")
    found = _store(pg_pool).inventory("material", "month", date(2026, 9, 1), date(2026, 11, 30))
    assert [(r.bucket, r.label, r.used_g, r.wasted_g) for r in found.consumption] == [
        (date(2026, 10, 1), "PLA", 10.0, 0.0),
        (date(2026, 11, 1), "PLA", 20.0, 20.0),
    ]
    weekly = _store(pg_pool).inventory("material", "week", date(2026, 9, 1), date(2026, 11, 30))
    assert [r.bucket for r in weekly.consumption] == [date(2026, 9, 28), date(2026, 11, 2)]
    narrow = _store(pg_pool).inventory("material", "month", date(2026, 11, 1), date(2026, 11, 30))
    assert [r.bucket for r in narrow.consumption] == [date(2026, 11, 1)]


def test_waste_by_printer(pg_pool: PgPool) -> None:
    printer = uuid4()
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, %s, 'Garage')",
            (printer, f"S{printer.hex[:8]}"),
        )
    spool = _spool(pg_pool, grams_left=900)
    at = datetime(2026, 11, 5, 12, tzinfo=UTC)
    _bb_end(pg_pool, spool, at, 12, "a", status="failed", printer=printer)
    _bb_end(pg_pool, spool, at, 36, "b", printer=printer)
    found = _store(pg_pool).inventory("material", "month", date(2026, 11, 1), date(2026, 11, 30))
    [row] = [w for w in found.waste if w.dimension == "printer"]
    assert row.key == str(printer)
    assert row.label == "Garage"
    assert (row.used_g, row.wasted_g) == (48.0, 12.0)
    assert row.rate == 0.25
    assert {w.dimension for w in found.waste} == {"material", "type", "printer"}


# -- purchasing -------------------------------------------------------------------------


def test_spend_keeps_each_currency(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.type_id()
    line = InvoiceLineIn(
        description="A00", quantity=1, unit_price=Decimal(40), line_subtotal=Decimal(40),
        type_id=type_id,
    )  # fmt: skip
    usd = shop.draft([line], number="R-1")
    assert shop.invoices.confirm(usd.id, actor="ann") is not None
    shop.currency = "CAD"
    fifty = line.model_copy(update={"unit_price": Decimal(50), "line_subtotal": Decimal(50)})
    cad = shop.draft([fifty], number="R-2", total="50.00", subtotal="50", tax="0")
    assert shop.invoices.confirm(cad.id, actor="ann") is not None
    shop.draft([line], number="R-3")
    found = _store(pg_pool).purchasing(date(2026, 1, 1), date(2026, 12, 31))
    assert sorted((r.currency, r.total, r.invoices, r.month) for r in found.spend) == [
        ("CAD", Decimal("50.0000"), 1, date(2026, 11, 1)),
        ("USD", Decimal("42.8000"), 1, date(2026, 11, 1)),
    ]
    assert {r.supplier_name for r in found.spend} == {"Acme"}


def _product(pool: PgPool, pack_size: int = 1) -> tuple[Shop, UUID]:
    shop = Shop(pool)
    made = shop.suppliers.add_product(
        shop.supplier_id, ProductCreate(type_id=shop.type_id(), sku="S1", pack_size=pack_size)
    )
    assert made is not None
    return shop, made.id


def test_price_per_kg_series(pg_pool: PgPool) -> None:
    shop, product = _product(pg_pool, pack_size=2)
    shop.suppliers.add_price(
        product, PricePointCreate(price=Decimal("40.0000"), on_date=date(2026, 11, 2))
    )
    found = _store(pg_pool).purchasing(date(2026, 1, 1), date(2026, 12, 31))
    [series] = found.prices
    assert series.type_label == "PLA-WHT"
    assert series.supplier_name == "Acme"
    assert series.currency == "USD"
    assert [(p.on_date, p.price_per_kg) for p in series.points] == [
        (date(2026, 11, 2), Decimal("20.0000"))
    ]
    outside = _store(pg_pool).purchasing(date(2026, 1, 1), date(2026, 11, 1))
    assert outside.prices == []


def test_price_series_split_by_currency(pg_pool: PgPool) -> None:
    shop, product = _product(pg_pool)
    for currency in ("USD", "CAD"):
        shop.suppliers.add_price(
            product,
            PricePointCreate(price=Decimal(20), currency=currency, on_date=date(2026, 11, 2)),
        )
    found = _store(pg_pool).purchasing(date(2026, 1, 1), date(2026, 12, 31))
    assert sorted(s.currency for s in found.prices) == ["CAD", "USD"]
    assert all(len(s.points) == 1 for s in found.prices)


# -- the summary's consumed grams -------------------------------------------------------


def test_summary_consumed_includes_archived(pg_pool: PgPool) -> None:
    spools = FilamentSpoolStore(pg_pool)
    spool = _spool(pg_pool, grams_left=900)
    _sb_end(pg_pool, spool, NOW, 25, "a")
    spools.archive(spool, True, actor="ann")
    assert spools.summary().consumed_g == 25.0


def test_summary_consumed_counts_a_paired_plate_once(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, grams_left=900)
    _sb_end(pg_pool, spool, NOW, 30, "a")
    _bb_end(pg_pool, spool, NOW, 28, "a")
    assert FilamentSpoolStore(pg_pool).summary().consumed_g == 28.0


# -- the snapshot -----------------------------------------------------------------------


def test_snapshot_reorder_uses_the_spools_effective_material(pg_pool: PgPool) -> None:
    petg = next(
        r.id
        for r in CatalogStore(pg_pool).list("materials")
        if isinstance(r, Material) and r.name == "PETG"
    )
    type_id = _type(pg_pool, lead_time_days=7, safety_margin_days=3)
    spool = _spool(pg_pool, type_id=type_id, grams_left=350, material_id=petg)
    _sb_end(pg_pool, spool, None, 300, "a")
    found = _store(pg_pool).snapshot()
    assert found.low_stock.keys() == found.reorder.keys() == {"PETG"}
    assert found.reorder == {"PETG": 1}


def test_snapshot_reorder_takes_an_archived_spools_material(pg_pool: PgPool) -> None:
    petg = next(
        r.id
        for r in CatalogStore(pg_pool).list("materials")
        if isinstance(r, Material) and r.name == "PETG"
    )
    type_id = _type(pg_pool, lead_time_days=7, safety_margin_days=3)
    spool = _spool(pg_pool, type_id=type_id, grams_left=350, material_id=petg)
    _sb_end(pg_pool, spool, None, 300, "a")
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann")
    found = _store(pg_pool).snapshot()
    assert found.reorder == {"PETG": 1}


def test_snapshot_reorder_takes_the_materials_with_most_grams(pg_pool: PgPool) -> None:
    materials = {
        r.name: r.id for r in CatalogStore(pg_pool).list("materials") if isinstance(r, Material)
    }
    type_id = _type(pg_pool, lead_time_days=7, safety_margin_days=3)
    little = _spool(pg_pool, type_id=type_id, grams_left=100, material_id=materials["PLA"])
    big = _spool(pg_pool, type_id=type_id, grams_left=250, material_id=materials["PETG"])
    _sb_end(pg_pool, little, None, 300, "a")
    _sb_end(pg_pool, big, None, 300, "b")
    found = _store(pg_pool).snapshot()
    assert found.reorder == {"PLA": 0, "PETG": 1}


def test_snapshot_reorder_counts_a_type_under_its_stocked_spools_material(
    pg_pool: PgPool,
) -> None:
    materials = {
        r.name: r.id for r in CatalogStore(pg_pool).list("materials") if isinstance(r, Material)
    }
    type_id = _type(pg_pool, lead_time_days=7, safety_margin_days=3)
    stocked = _spool(pg_pool, type_id=type_id, grams_left=50, material_id=materials["PLA"])
    archived = _spool(pg_pool, type_id=type_id, grams_left=900, material_id=materials["PETG"])
    _sb_end(pg_pool, stocked, None, 300, "a")
    FilamentSpoolStore(pg_pool).archive(archived, True, actor="ann")
    found = _store(pg_pool).snapshot()
    # The archived spool has more grams, but the stocked one's material wins.
    assert found.reorder.get("PLA") == 1
    assert found.reorder.get("PETG", 0) == 0


def test_snapshot_folds_nothing_itself(pg_pool: PgPool) -> None:
    kind = CatalogStore(pg_pool).list("location-kinds")[0]
    assert isinstance(kind, LocationKind)
    with pg_pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_locations (name, kind_id) VALUES ('Shelf 1', %s) RETURNING id",
            (kind.id,),
        ).fetchone()
    assert row is not None
    spool = _spool(pg_pool, grams_left=900)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spools SET location_id = %s WHERE id = %s", (row["id"], spool)
        )
    _sb_end(pg_pool, spool, None, 30, "a")
    _spool(pg_pool, grams_left=100)
    found = _store(pg_pool).snapshot()
    assert sorted(found.stock, key=lambda r: str(r[2])) == sorted(
        [("PLA", None, kind.name, 1, 870.0, None), ("PLA", None, None, 1, 100.0, None)],
        key=lambda r: str(r[2]),
    )
    assert found.used == {("PLA", "completed"): 30.0}
    assert found.burn["PLA"] == pytest.approx(30.0)
    assert found.days_of_stock["PLA"] == pytest.approx(970 / 30.0)
    assert found.low_stock == {"PLA": 0}
    assert found.reorder == {"PLA": 0}
    assert found.currency == "USD"


# -- material numbers (#7152) -----------------------------------------------------------

WINDOW = (date(2026, 11, 1), date(2026, 11, 30))


def _archive_type(pool: PgPool, type_id: UUID, days_ago: int) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spool_types SET archived_at = now() - make_interval(days => %s)"
            " WHERE id = %s",
            (days_ago, type_id),
        )


def test_material_numbers_fold_case_and_archived_types(pg_pool: PgPool) -> None:
    """R-7152-5: one key per ``upper(code)``, archived types of that code in it; the label
    is the live type's code."""
    old = _type(pg_pool, "pla-wht")
    _spool(pg_pool, type_id=old, grams_left=300)
    _archive_type(pg_pool, old, 2)
    live = _type(pg_pool, "PLA-Wht")
    _spool(pg_pool, type_id=live, grams_left=700)
    other = _type(pg_pool, "PETG-BLK")
    _spool(pg_pool, type_id=other, grams_left=100)
    found = _store(pg_pool).material_numbers(*WINDOW)
    assert (found.since, found.until) == WINDOW
    assert [(r.key, r.label, r.spools, r.grams_left) for r in found.rows] == [
        ("PLA-WHT", "PLA-Wht", 2, 1000.0),
        ("PETG-BLK", "PETG-BLK", 1, 100.0),
    ]
    types = {t.id: t for t in found.rows[0].types}
    assert types.keys() == {old, live}
    assert (types[old].archived, types[live].archived) == (True, False)
    assert types[old].code == "pla-wht"


def test_material_number_label_is_the_newest_archived_code_with_no_live_type(
    pg_pool: PgPool,
) -> None:
    older = _type(pg_pool, "abs-red")
    _spool(pg_pool, type_id=older, grams_left=10)
    _archive_type(pg_pool, older, 5)
    newer = _type(pg_pool, "ABS-RED")
    _spool(pg_pool, type_id=newer, grams_left=10)
    _archive_type(pg_pool, newer, 1)
    [row] = _store(pg_pool).material_numbers(*WINDOW).rows
    assert (row.key, row.label) == ("ABS-RED", "ABS-RED")


def test_material_number_used_is_the_metrics_consumption(pg_pool: PgPool) -> None:
    """R-7152-6: plates in the range, every outcome, archived spools too, a paired plate
    once; the failed ones also as waste."""
    type_id = _type(pg_pool, "PLA-WHT")
    one = _spool(pg_pool, type_id=type_id, grams_left=900)
    gone = _spool(pg_pool, type_id=type_id, grams_left=900)
    at = datetime(2026, 11, 5, 12, tzinfo=UTC)
    _sb_end(pg_pool, one, at, 30, "a")
    _bb_end(pg_pool, one, at, 28, "a")
    _bb_end(pg_pool, one, at, 12, "b", status="failed")
    _sb_end(pg_pool, gone, at, 5, "c")
    _sb_end(pg_pool, one, datetime(2026, 10, 31, 23, tzinfo=UTC), 100, "d")
    _sb_end(pg_pool, one, datetime(2026, 12, 1, 0, 30, tzinfo=UTC), 100, "e")
    FilamentSpoolStore(pg_pool).archive(gone, True, actor="ann")
    store = _store(pg_pool)
    [row] = store.material_numbers(*WINDOW).rows
    assert (row.used_g, row.wasted_g) == (45.0, 12.0)
    assert row.spools == 1
    metrics = store.inventory("type", "month", *WINDOW)
    assert sum(r.used_g for r in metrics.consumption) == row.used_g
    assert sum(r.wasted_g for r in metrics.consumption) == row.wasted_g


def test_material_number_value_on_hand(pg_pool: PgPool) -> None:
    """R-7152-7: each spool's cost per kg, else the default's; ``None`` with no cost."""
    priced = _type(pg_pool, "PLA-WHT", cost_per_kg="20")
    _spool(pg_pool, type_id=priced, grams_left=500)
    _spool(pg_pool, type_id=priced, grams_left=250, cost_per_kg="40")
    plain = _type(pg_pool, "PETG-BLK")
    _spool(pg_pool, type_id=plain, grams_left=400)
    rows = {r.key: r for r in _store(pg_pool).material_numbers(*WINDOW).rows}
    assert rows["PLA-WHT"].value == Decimal("20.00")
    assert rows["PETG-BLK"].value is None
    fallback = {
        r.key: r
        for r in _store(pg_pool, default_cost_per_kg=Decimal("10")).material_numbers(*WINDOW).rows
    }
    assert fallback["PETG-BLK"].value == Decimal("4.00")
    assert _store(pg_pool, currency="EUR").material_numbers(*WINDOW).currency == "EUR"


def test_material_numbers_untyped_row_is_last(pg_pool: PgPool) -> None:
    type_id = _type(pg_pool, "PLA-WHT")
    _spool(pg_pool, type_id=type_id, grams_left=10)
    _spool(pg_pool, grams_left=900)
    rows = _store(pg_pool).material_numbers(*WINDOW).rows
    assert [(r.key, r.label, r.types) for r in rows][-1] == (None, "No material number", [])
    assert rows[0].key == "PLA-WHT"


def test_material_numbers_list_only_stock_or_use(pg_pool: PgPool) -> None:
    """R-7152-8: a key with no spool in stock and no plate in the range is left out."""
    idle = _type(pg_pool, "PLA-IDLE")
    spool = _spool(pg_pool, type_id=idle, grams_left=100)
    _sb_end(pg_pool, spool, datetime(2026, 10, 2, tzinfo=UTC), 10, "a")
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann")
    _type(pg_pool, "PLA-NONE")
    used = _type(pg_pool, "PLA-USED")
    gone = _spool(pg_pool, type_id=used, grams_left=100)
    _sb_end(pg_pool, gone, datetime(2026, 11, 2, tzinfo=UTC), 10, "b")
    FilamentSpoolStore(pg_pool).archive(gone, True, actor="ann")
    rows = _store(pg_pool).material_numbers(*WINDOW).rows
    assert [(r.key, r.spools, r.used_g) for r in rows] == [("PLA-USED", 0, 10.0)]


def test_material_numbers_fold_case_as_the_unique_index_does(pg_pool: PgPool) -> None:
    """R-7152-10: the key is Postgres's ``upper(code)``, the unique index's, not Python's
    ``str.upper``, which makes ``ß`` into ``SS`` and so joins codes the index keeps apart."""
    sharp = _type(pg_pool, "PLA-Weiß")
    _spool(pg_pool, type_id=sharp, grams_left=100)
    double = _type(pg_pool, "PLA-WEISS")
    _spool(pg_pool, type_id=double, grams_left=200)
    with pg_pool.connection() as conn:
        found = conn.execute("SELECT upper('PLA-Weiß') AS k").fetchone()
    assert found is not None
    key = found["k"]
    assert key != "PLA-WEISS"
    rows = {r.key: r for r in _store(pg_pool).material_numbers(*WINDOW).rows}
    assert rows.keys() == {key, "PLA-WEISS"}
    assert [t.id for t in rows[key].types] == [sharp]
    assert [t.id for t in rows["PLA-WEISS"].types] == [double]
