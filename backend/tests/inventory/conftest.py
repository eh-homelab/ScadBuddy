"""Fixtures for the inventory stores (#7067): a migrated throwaway schema."""

from __future__ import annotations

import pytest

from scadbuddy.entities.properties import EntityPropertyStore
from scadbuddy.entities.registry import EntityStore
from tests.conftest import PgPool
from tests.support.inventory_rows import ItemRows, KindRows


@pytest.fixture
def kinds(pg_pool: PgPool) -> KindRows:
    return KindRows(pg_pool)


@pytest.fixture
def items(pg_pool: PgPool) -> ItemRows:
    return ItemRows(pg_pool)


@pytest.fixture
def props(pg_pool: PgPool) -> EntityPropertyStore:
    return EntityPropertyStore(pg_pool, EntityStore(pg_pool))
