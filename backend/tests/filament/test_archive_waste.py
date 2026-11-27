"""Grams left on a spool at archive are booked as non-print waste (#7167).

A person's archive writes one ``non_print_waste`` event, the zero reading, after the final
weighing and before the ``archived`` event. A respooled archive, the bulk archive and the
Bambuddy pull's archive book none."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from scadbuddy.filament import ledger
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.metrics import plates
from scadbuddy.filament.purchasing_models import ShoppingItem
from scadbuddy.filament.shopping import ShoppingStore
from scadbuddy.filament.spools import FilamentSpoolStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def _material(pool: PgPool) -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, m.Material) and r.name == "PLA")


def _spool(pool: PgPool, grams: float | None = 120) -> UUID:
    body = m.SpoolCreate.model_validate(
        {"material_id": _material(pool), "label_weight_g": 1000, "grams_left": grams}
    )
    return FilamentSpoolStore(pool).create(body, actor="ann").id


def _kinds(pool: PgPool, spool: UUID) -> list[str]:
    return [e.kind for e in FilamentSpoolStore(pool).events(spool)]


def _waste(pool: PgPool, spool: UUID) -> list[m.SpoolEvent]:
    return [e for e in FilamentSpoolStore(pool).events(spool) if e.kind == "non_print_waste"]


def _closing(pool: PgPool, spool: UUID) -> dict[str, object]:
    archived = [e for e in FilamentSpoolStore(pool).events(spool) if e.kind == "archived"]
    return archived[-1].data


def _items(pool: PgPool) -> list[ShoppingItem]:
    return ShoppingStore(pool).list()


def test_archive_with_grams_left_books_waste_and_zeroes(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, 120)
    store = FilamentSpoolStore(pg_pool)
    got = store.archive(spool, True, actor="ann", reason="damaged")
    assert got is not None and got.grams_left == 0
    [event] = _waste(pg_pool, spool)
    assert event.data["waste_g"] == 120 and event.data["cause"] == "archive"
    assert (event.grams_before, event.grams_after, event.source) == (120, 0, "person")
    assert _kinds(pg_pool, spool)[-2:] == ["non_print_waste", "archived"]
    assert _closing(pg_pool, spool)["non_print_waste_g"] == 120
    detail = store.detail(spool)
    assert detail is not None and detail.closing is not None
    assert detail.closing.non_print_waste_g == 120


def test_final_weighing_sets_the_waste(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, 120)
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann", reason="damaged", final_grams=35)
    assert _kinds(pg_pool, spool)[-3:] == ["weighed", "non_print_waste", "archived"]
    [event] = _waste(pg_pool, spool)
    assert event.data["waste_g"] == 35
    assert (event.grams_before, event.grams_after) == (35, 0)
    assert _closing(pg_pool, spool)["non_print_waste_g"] == 35


def test_finished_at_zero_books_no_waste(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, 120)
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann", reason="finished", final_grams=0)
    assert _waste(pg_pool, spool) == []
    assert _closing(pg_pool, spool)["non_print_waste_g"] is None


def test_respooled_books_no_waste(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, 120)
    got = FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann", reason="respooled")
    assert got is not None and got.grams_left == 120
    assert _waste(pg_pool, spool) == []
    assert _closing(pg_pool, spool)["non_print_waste_g"] is None


def test_no_reading_no_waste_event_and_no_null_grams(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, None)
    got = FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann", reason="damaged")
    assert got is not None and got.grams_left is None
    assert _waste(pg_pool, spool) == []
    with pg_pool.connection() as conn:
        events = ledger.events(conn, spool)
    assert all(e.grams_after is None for e in events if e.kind == "archived")


def test_waste_is_not_counted_as_a_plate(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, 120)
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann", reason="damaged")
    with pg_pool.connection() as conn:
        assert plates(ledger.events(conn, spool)) == []


def test_bulk_archive_and_pull_archive_book_no_waste(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    bulk, pulled, patched = (_spool(pg_pool, 120) for _ in range(3))
    store.bulk_archive([bulk], True, actor="ann")
    with pg_pool.connection() as conn, conn.transaction():
        store.update_on(
            conn,
            pulled,
            m.SpoolPatch(archived=True),
            actor="bb",
            at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    store.update(patched, m.SpoolPatch(archived=True), actor="ann")
    for spool in (bulk, pulled, patched):
        assert _waste(pg_pool, spool) == []
        got = store.get(spool)
        assert got is not None and got.grams_left == 120


def test_restore_after_waste_keeps_zero_grams(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, 120)
    store = FilamentSpoolStore(pg_pool)
    store.archive(spool, True, actor="ann", reason="damaged")
    got = store.archive(spool, False, actor="ann")
    assert got is not None and got.archived_at is None and got.grams_left == 0


def test_waste_zero_does_not_add_a_shopping_row(pg_pool: PgPool) -> None:
    # The waste event's zero is no run-out. The archive's own row (#6868) stays one row.
    spool = _spool(pg_pool, 120)
    FilamentSpoolStore(pg_pool).archive(spool, True, actor="ann", reason="damaged")
    [item] = _items(pg_pool)
    assert item.empty_spool_id == spool and item.note is not None and "archived" in item.note
    # A spool archived at 0 g after a final weighing of 0 adds none beyond its own.
    ShoppingStore(pg_pool).clear(purchased_only=False)
    again = _spool(pg_pool, 50)
    FilamentSpoolStore(pg_pool).archive(again, True, actor="ann", reason="other", final_grams=0)
    [row] = _items(pg_pool)
    # The one row is the weighing's own run-out; the waste event added none.
    assert row.empty_spool_id == again and row.note is not None and "ran out" in row.note


def test_the_waste_event_alone_adds_nothing(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, 120)
    with pg_pool.connection() as conn, conn.transaction():
        ledger.record(
            conn,
            spool,
            "non_print_waste",
            source="person",
            absolute_g=0,
            actor="ann",
            data={"waste_g": 120, "cause": "archive"},
        )
    assert _items(pg_pool) == []


def test_re_archive_after_restore_keeps_one_waste_event_and_one_shopping_row(
    pg_pool: PgPool,
) -> None:
    spool = _spool(pg_pool, 120)
    store = FilamentSpoolStore(pg_pool)
    store.archive(spool, True, actor="ann", reason="damaged")
    store.archive(spool, False, actor="ann")
    got = store.archive(spool, True, actor="ann", reason="damaged")
    assert got is not None and got.grams_left == 0
    assert len(_waste(pg_pool, spool)) == 1
    [item] = _items(pg_pool)
    assert item.empty_spool_id == spool


def test_a_tiny_raw_remainder_is_waste_with_the_raw_number(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, 120)
    got = FilamentSpoolStore(pg_pool).archive(
        spool, True, actor="ann", reason="damaged", final_grams=0.04
    )
    assert got is not None and got.grams_left == 0
    [event] = _waste(pg_pool, spool)
    assert event.data["waste_g"] == 0.04 and event.grams_before == 0.04


def test_the_archived_spool_with_no_reading_stays_null(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, None)
    store = FilamentSpoolStore(pg_pool)
    store.archive(spool, True, actor="ann", reason="damaged")
    got = store.get(spool)
    assert got is not None and got.grams_left is None


def test_archive_on_in_a_callers_transaction_keeps_its_cause(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, 80)
    store = FilamentSpoolStore(pg_pool)
    with pg_pool.connection() as conn, conn.transaction():
        got = store.archive_on(
            conn,
            spool,
            reason="other",
            final_reading_g=None,
            final_grams=None,
            actor="ann",
            cause="replaced",
        )
    assert got is not None and got.grams_left == 0
    [event] = _waste(pg_pool, spool)
    assert event.data["cause"] == "replaced" and event.data["waste_g"] == 80


def test_archive_on_rolled_back_writes_no_event(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, 80)
    store = FilamentSpoolStore(pg_pool)
    with pytest.raises(RuntimeError), pg_pool.connection() as conn, conn.transaction():
        store.archive_on(
            conn,
            spool,
            reason="other",
            final_reading_g=None,
            final_grams=None,
            actor="ann",
            cause="replaced",
        )
        raise RuntimeError
    assert _kinds(pg_pool, spool) == ["created"]
    got = store.get(spool)
    assert got is not None and got.grams_left == 80 and got.archived_at is None
