"""Fixtures for the entity registry: a migrated throwaway schema."""

from __future__ import annotations

import pytest

from scadbuddy.entities.registry import EntityStore
from tests.conftest import PgPool


@pytest.fixture
def store(pg_pool: PgPool) -> EntityStore:
    return EntityStore(pg_pool)
