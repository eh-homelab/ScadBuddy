"""A spool's effective empty weight (#7682)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.empty_weight import EmptyWeight, effective_on
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.conftest import PgPool

pg = pytest.mark.requires_postgres


def _material(pool: PgPool) -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, m.Material) and r.name == "PLA")


def _insert(pool: PgPool, name: str, grams: int, note: str | None = None) -> UUID:
    """An insert as the Bambuddy pull makes one: basis `catalog` by the column default."""
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_insert_types (name, empty_weight_g, note)"
            " VALUES (%s, %s, %s) RETURNING id",
            (name, grams, note),
        ).fetchone()
    assert row is not None
    made: UUID = row["id"]
    return made


def _type(pool: PgPool, insert: UUID | None) -> UUID:
    return (
        SpoolTypeStore(pool)
        .create(
            m.SpoolTypeCreate(
                code=f"PLA-{uuid4().hex[:6]}",
                material_id=_material(pool),
                rgba="#FF0000",
                label_weight_g=1000,
                insert_type_id=insert,
            )
        )
        .id
    )


def _spool(pool: PgPool, insert: UUID | None, **over: Any) -> UUID:
    body = {"material_id": _material(pool), "label_weight_g": 1000, "insert_type_id": insert} | over
    return FilamentSpoolStore(pool).create(m.SpoolCreate.model_validate(body), actor="ann").id


@pg
def test_the_spools_own_insert_wins_over_its_types(pg_pool: PgPool) -> None:
    cardboard = _insert(pg_pool, "Cardboard", 180)
    plastic = _insert(pg_pool, "Plastic", 200)
    spool = _spool(pg_pool, plastic, type_id=_type(pg_pool, cardboard))
    with pg_pool.connection() as conn:
        assert effective_on(conn, spool) == EmptyWeight(200, "insert", "catalog", "Plastic", None)


@pg
def test_a_placeholder_note_makes_the_basis_placeholder(pg_pool: PgPool) -> None:
    insert = _insert(pg_pool, "Plastic", 200, note="Placeholder weight until weighed")
    spool = _spool(pg_pool, insert)
    with pg_pool.connection() as conn:
        found = effective_on(conn, spool)
    assert found is not None and found.basis == "placeholder"
    assert found.note == "Placeholder weight until weighed"


@pg
def test_the_types_insert_is_taken_when_the_spool_sets_none(pg_pool: PgPool) -> None:
    cardboard = _insert(pg_pool, "Cardboard", 180)
    spool = _spool(pg_pool, None, type_id=_type(pg_pool, cardboard))
    with pg_pool.connection() as conn:
        found = effective_on(conn, spool)
    assert found is not None and found.grams == 180
    assert found.name == "Cardboard"


@pg
def test_no_insert_is_none(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, None, type_id=_type(pg_pool, None))
    with pg_pool.connection() as conn:
        assert effective_on(conn, spool) is None
        assert effective_on(conn, uuid4()) is None
