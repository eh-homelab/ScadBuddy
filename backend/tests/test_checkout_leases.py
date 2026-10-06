"""Checkout leases in Postgres (#872): the render worker and the API each build their
own `CheckoutGate`, and a lease one takes must be seen by the other's removal."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from scadbuddy.library.libraries import CheckoutGate, CheckoutLeases
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

JOB = "a" * 32


def _gate(pool: PgPool, root: Path, *, ttl: float = 60.0) -> CheckoutGate:
    return CheckoutGate(CheckoutLeases(pool, root, ttl=ttl))


def _checkout(root: Path, name: str = "BOSL2", commit: str = "c" * 40) -> Path:
    checkout = root / name / commit
    (checkout / name).mkdir(parents=True)
    return checkout


async def test_a_lease_the_worker_holds_is_seen_by_the_apis_removal(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    worker, api = _gate(pg_pool, tmp_path), _gate(pg_pool, tmp_path)
    checkout = _checkout(tmp_path)
    other = _checkout(tmp_path, commit="d" * 40)

    async with worker.rendering(JOB, [checkout]), api.removing():
        assert api.leased(checkout) == [JOB]
        assert api.leased(checkout.parent) == [JOB]
        assert api.leased(other) == []

    async with api.removing():
        assert api.leased(checkout) == []


async def test_a_lease_waits_out_a_removal_in_another_process(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    worker, api = _gate(pg_pool, tmp_path), _gate(pg_pool, tmp_path)
    checkout = _checkout(tmp_path)
    leased = asyncio.Event()

    async def render() -> None:
        async with worker.rendering(JOB, [checkout]):
            leased.set()

    async with api.removing():
        task = asyncio.create_task(render())
        await asyncio.sleep(0.5)
        # Not taken while the removal runs: it would have checked for leases already.
        assert not leased.is_set()
    await asyncio.wait_for(task, 10)
    assert leased.is_set()


async def test_a_crashed_holders_lease_expires(pg_pool: PgPool, tmp_path: Path) -> None:
    worker = CheckoutLeases(pg_pool, tmp_path, ttl=0.5)
    api = _gate(pg_pool, tmp_path)
    checkout = _checkout(tmp_path)

    # Taken and never renewed nor released: the worker died holding it.
    worker.take(JOB, [checkout])
    assert api.leased(checkout) == [JOB]

    time.sleep(1.0)
    async with api.removing():
        assert api.leased(checkout) == []


async def test_a_live_lease_is_renewed_past_its_ttl(pg_pool: PgPool, tmp_path: Path) -> None:
    worker, api = _gate(pg_pool, tmp_path, ttl=0.6), _gate(pg_pool, tmp_path)
    checkout = _checkout(tmp_path)

    async with worker.rendering(JOB, [checkout]):
        await asyncio.sleep(1.5)
        assert await asyncio.to_thread(api.leased, checkout) == [JOB]
