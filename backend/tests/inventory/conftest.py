"""Fixtures for the inventory stores (#7067): a migrated throwaway schema."""

from __future__ import annotations

import pytest

from scadbuddy.entities.properties import EntityPropertyStore
from scadbuddy.entities.registry import EntityStore
from scadbuddy.inventory.items import ItemStore
from scadbuddy.inventory.kinds import KindStore
from tests.conftest import PgPool


@pytest.fixture
def kinds(pg_pool: PgPool) -> KindStore:
    return KindStore(pg_pool)


@pytest.fixture
def items(pg_pool: PgPool) -> ItemStore:
    return ItemStore(pg_pool, EntityStore(pg_pool))


@pytest.fixture
def props(pg_pool: PgPool) -> EntityPropertyStore:
    return EntityPropertyStore(pg_pool, EntityStore(pg_pool))
