"""Fixtures for the orders feature (#4249): the row store over a migrated throwaway
schema. Every test here needs Postgres, as the rows live nowhere else."""

from __future__ import annotations

import pytest

from scadbuddy.orders.store import OrderStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def pool(pg_pool: PgPool) -> PgPool:
    return pg_pool


@pytest.fixture
def rows(pool: PgPool) -> OrderStore:
    return OrderStore(pool)
