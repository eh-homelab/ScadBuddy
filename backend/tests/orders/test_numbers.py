"""Order numbers (#4249, plan Ruling 5): ``ORD-<yyyy>-<n>`` from a per-year counter row
under a row lock, so a rolled-back insert never skips one and two replicas never
repeat one."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from scadbuddy.orders.numbers import next_number
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def test_numbers_count_per_year(pool: PgPool) -> None:
    with pool.connection() as c, c.transaction():
        assert next_number(c, 2026) == "ORD-2026-1"
        assert next_number(c, 2026) == "ORD-2026-2"
        assert next_number(c, 2027) == "ORD-2027-1"


def test_numbers_need_a_transaction(pool: PgPool) -> None:
    # The lock lives for the transaction: outside one it would be released at once.
    with pool.connection() as c, pytest.raises(RuntimeError, match="transaction"):
        next_number(c, 2026)


def test_numbers_are_distinct_under_concurrent_creates(pool: PgPool) -> None:
    # Two threads, two connections, 50 numbers each: 100 distinct, none skipped.
    def take(count: int) -> list[str]:
        taken: list[str] = []
        with pool.connection() as c:
            for _ in range(count):
                with c.transaction():
                    taken.append(next_number(c, 2026))
        return taken

    with ThreadPoolExecutor(max_workers=2) as threads:
        first, second = threads.map(take, [50, 50])
    numbers = first + second
    assert len(set(numbers)) == 100
    assert set(numbers) == {f"ORD-2026-{n}" for n in range(1, 101)}


def test_a_rolled_back_insert_does_not_skip(pool: PgPool) -> None:
    class AbandonError(Exception):
        pass

    with pool.connection() as c:
        with c.transaction():
            assert next_number(c, 2026) == "ORD-2026-1"
        with pytest.raises(AbandonError), c.transaction():
            assert next_number(c, 2026) == "ORD-2026-2"
            raise AbandonError
        with c.transaction():
            assert next_number(c, 2026) == "ORD-2026-2"


def test_numbers_continue_across_committed_transactions(pool: PgPool) -> None:
    with pool.connection() as c, c.transaction():
        assert next_number(c, 2026) == "ORD-2026-1"
    with pool.connection() as c, c.transaction():
        assert next_number(c, 2026) == "ORD-2026-2"
