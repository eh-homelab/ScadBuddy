"""An insert's empty weight from a new spool's weighing (#7280)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.insert_weights import (
    InsertWeightStore,
    NoInsertWeightOfferError,
    checked_by_default,
    is_placeholder,
    mean_weight,
    offer_reason,
    unused,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from tests.conftest import PgPool

pg = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 23, 12, tzinfo=UTC)
_SEQ = itertools.count(1)


def _event(minute: int, kind: str, source: str | None, **over: Any) -> m.SpoolEvent:
    values: dict[str, Any] = {
        "id": uuid4(),
        "seq": next(_SEQ),
        "spool_id": UUID(int=1),
        "at": T0 + timedelta(minutes=minute),
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
    }
    return m.SpoolEvent.model_validate(values | over)


# -- the pure rules ------------------------------------------------------------------


def test_a_placeholder_is_its_basis_or_its_note() -> None:
    assert is_placeholder("placeholder", None) is True
    assert is_placeholder("catalog", "Placeholder weight: weigh an empty spool") is True
    assert is_placeholder("catalog", "  placeholder WEIGHT, weigh it") is True
    assert is_placeholder("catalog", "real") is False
    assert is_placeholder("person", None) is False


def test_a_spool_is_unused_only_with_no_consumption_and_full_grams() -> None:
    full = [_event(0, "created", "person", grams_after=1000)]
    weighed = [*full, _event(1, "weighed", "scale", grams_after=1000)]
    assert unused(weighed, grams_left=1000.0, label=1000) is True
    printed = [*weighed, _event(2, "print_ended", "print_actual", delta_g=-12)]
    assert unused(printed, grams_left=1000.0, label=1000) is False
    lower = [
        *full,
        _event(1, "weighed", "scale", grams_after=950),
        _event(2, "weighed", "scale", grams_after=1000),
    ]
    assert unused(lower, grams_left=1000.0, label=1000) is False
    assert unused(weighed, grams_left=990.0, label=1000) is False
    assert unused(weighed, grams_left=None, label=1000) is False


def test_an_offer_needs_a_reading_above_the_label_and_a_derived_weight_at_most_it() -> None:
    assert offer_reason(1200, 1000) is None
    assert offer_reason(1000, 1000) == "not_above_label"
    assert offer_reason(2600, 1000) == "derived_too_heavy"
    assert offer_reason(2000, 1000) is None


def test_the_weight_is_the_mean_rounded_half_up() -> None:
    assert mean_weight([150.0, 151.0]) == 151
    assert mean_weight([200.0]) == 200


def test_only_a_placeholder_or_a_derived_weight_is_checked_by_default() -> None:
    assert checked_by_default("derived", False) is True
    assert checked_by_default("catalog", True) is True
    assert checked_by_default("person", False) is False
    assert checked_by_default("weighed_empty", False) is False


# -- the store -----------------------------------------------------------------------


def _material(pool: PgPool) -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, m.Material) and r.name == "PLA")


def _insert(pool: PgPool, name: str = "Cardboard", note: str | None = None) -> UUID:
    """An insert as the Bambuddy pull makes one: basis `catalog` by the column default."""
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_insert_types (name, empty_weight_g, note)"
            " VALUES (%s, 200, %s) RETURNING id",
            (name, note),
        ).fetchone()
    assert row is not None
    made: UUID = row["id"]
    return made


def _spool(pool: PgPool, insert: UUID | None, **over: Any) -> UUID:
    body = {
        "material_id": _material(pool),
        "label_weight_g": 1000,
        "insert_type_id": insert,
        "grams_left": 1000,
    } | over
    spools = FilamentSpoolStore(pool)
    return spools.create(m.SpoolCreate.model_validate(body), actor="ann").id


def _store(pool: PgPool) -> InsertWeightStore:
    return InsertWeightStore(FilamentSpoolStore(pool))


def _insert_row(pool: PgPool, insert: UUID) -> m.InsertType:
    row = CatalogStore(pool).get("insert-types", insert)
    assert isinstance(row, m.InsertType)
    return row


@pg
def test_a_new_spool_offers_its_derived_weight_unchecked_for_a_catalog_weight(
    pg_pool: PgPool,
) -> None:
    insert = _insert(pg_pool)
    spool = _spool(pg_pool, insert)
    check = _store(pg_pool).check(spool, 1150)
    assert check is not None
    assert check.reason is None
    assert (check.insert_type_id, check.empty_weight_g, check.filament_g) == (insert, 200, 950)
    offer = check.offer
    assert offer is not None
    assert (offer.derived_g, offer.new_empty_weight_g, offer.sample_count) == (150, 150, 1)
    assert (offer.current_empty_weight_g, offer.current_basis) == (200, "catalog")
    assert (offer.label_weight_g, offer.reading_g, offer.spool_id) == (1000, 1150, spool)
    assert offer.insert_name == "Cardboard"
    assert offer.placeholder is False
    assert offer.checked_by_default is False


@pg
def test_a_placeholder_note_offers_it_checked(pg_pool: PgPool) -> None:
    insert = _insert(pg_pool, note="Placeholder weight, weigh an empty spool")
    check = _store(pg_pool).check(_spool(pg_pool, insert), 1150)
    assert check is not None and check.offer is not None
    assert check.offer.placeholder is True
    assert check.offer.checked_by_default is True


@pg
def test_accepting_sets_the_mean_of_the_readings_and_books_no_event(pg_pool: PgPool) -> None:
    store, spools = _store(pg_pool), FilamentSpoolStore(pg_pool)
    insert = _insert(pg_pool)
    first = _spool(pg_pool, insert)
    before = [e.id for e in spools.events(first)]
    accepted = store.accept(first, 1150)
    assert accepted is not None
    assert (accepted.empty_weight_g, accepted.sample_count, accepted.filament_g) == (150, 1, 1000)
    assert accepted.insert_type.empty_weight_basis == "derived"
    assert accepted.insert_type.derived_count == 1
    row = _insert_row(pg_pool, insert)
    assert (row.empty_weight_g, row.empty_weight_basis, row.derived_count) == (150, "derived", 1)
    assert [e.id for e in spools.events(first)] == before

    second = _spool(pg_pool, insert)
    check = store.check(second, 1151)
    assert check is not None and check.offer is not None
    assert (check.offer.new_empty_weight_g, check.offer.sample_count) == (151, 2)
    assert check.offer.checked_by_default is True
    done = store.accept(second, 1151)
    assert done is not None and done.empty_weight_g == 151

    again = store.accept(first, 1149)
    assert again is not None
    assert (again.sample_count, again.empty_weight_g) == (2, 150)
    assert _insert_row(pg_pool, insert).derived_count == 2


@pg
def test_a_spool_with_no_offer_says_why(pg_pool: PgPool) -> None:
    store, spools = _store(pg_pool), FilamentSpoolStore(pg_pool)
    insert = _insert(pg_pool)

    bare = store.check(_spool(pg_pool, None), 1150)
    assert bare is not None
    assert (bare.reason, bare.offer, bare.filament_g, bare.empty_weight_g) == (
        "no_insert",
        None,
        None,
        None,
    )

    unlabelled = store.check(_spool(pg_pool, insert, label_weight_g=None), 1150)
    assert unlabelled is not None and unlabelled.reason == "no_label_weight"
    assert unlabelled.filament_g == 950

    used = _spool(pg_pool, insert)
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_spool_events (spool_id, kind, source, delta_g)"
            " VALUES (%s, 'print_ended', 'print_actual', -12)",
            (used,),
        )
    spools.add_event(used, m.SpoolEventAdd(kind="weighed", grams=1000), actor="ann")
    printed = store.check(used, 1150)
    assert printed is not None and printed.reason == "used"

    fresh = _spool(pg_pool, insert)
    heavy = store.check(fresh, 2600)
    assert heavy is not None and heavy.reason == "derived_too_heavy"
    light = store.check(fresh, 1000)
    assert light is not None and light.reason == "not_above_label"

    # 900 g left is a reading below the label: use. 1100 g left is no use, but not full.
    part = store.check(_spool(pg_pool, insert, grams_left=900), 1150)
    assert part is not None and part.reason == "used"
    over = store.check(_spool(pg_pool, insert, grams_left=1100), 1150)
    assert over is not None and over.reason == "not_full"

    gone = _spool(pg_pool, insert)
    spools.archive(gone, True, actor="ann")
    archived = store.check(gone, 1150)
    assert archived is not None and archived.reason == "archived"

    assert store.check(uuid4(), 1150) is None
    assert store.accept(uuid4(), 1150) is None
    with pytest.raises(NoInsertWeightOfferError) as refused:
        store.accept(used, 1150)
    assert refused.value.reason == "used"
    assert _insert_row(pg_pool, insert).empty_weight_g == 200


@pg
def test_a_spool_whose_insert_changed_moves_its_reading(pg_pool: PgPool) -> None:
    store, spools = _store(pg_pool), FilamentSpoolStore(pg_pool)
    old, new = _insert(pg_pool, "Old"), _insert(pg_pool, "New")
    spool = _spool(pg_pool, old)
    assert store.accept(spool, 1150) is not None
    spools.update(spool, m.SpoolPatch(insert_type_id=new), actor="ann")
    moved = store.accept(spool, 1160)
    assert moved is not None and moved.insert_type.id == new
    assert (moved.empty_weight_g, moved.sample_count) == (160, 1)
    kept = _insert_row(pg_pool, old)
    assert (kept.empty_weight_g, kept.derived_count) == (150, 0)


@pg
def test_a_person_write_sets_the_basis(pg_pool: PgPool) -> None:
    catalog = CatalogStore(pg_pool)
    made = catalog.create("insert-types", m.InsertTypeCreate(name="Plastic", empty_weight_g=210))
    assert isinstance(made, m.InsertType) and made.empty_weight_basis == "person"
    insert = _insert(pg_pool)
    catalog.update("insert-types", insert, m.InsertTypePatch(note="kept"))
    assert _insert_row(pg_pool, insert).empty_weight_basis == "catalog"
    catalog.update("insert-types", insert, m.InsertTypePatch(empty_weight_g=190))
    assert _insert_row(pg_pool, insert).empty_weight_basis == "person"
    catalog.update(
        "insert-types",
        insert,
        m.InsertTypePatch(empty_weight_g=180, empty_weight_basis="placeholder"),
    )
    row = _insert_row(pg_pool, insert)
    assert (row.empty_weight_g, row.empty_weight_basis, row.placeholder) == (
        180,
        "placeholder",
        True,
    )
