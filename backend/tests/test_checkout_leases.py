"""Checkout leases in Postgres (#872): the render worker and the API each build their
own `CheckoutGate`, and a lease one takes must be seen by the other's removal."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from scadbuddy.library.libraries import CheckoutGate, CheckoutLeases, InstallPermits
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


async def test_a_pin_in_another_process_holds_off_a_removal(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    """A pin clones and THEN records; a removal in another process must not delete
    the checkout in between (#1131)."""
    worker, api = _gate(pg_pool, tmp_path), _gate(pg_pool, tmp_path)
    removed = asyncio.Event()

    async def remove() -> None:
        async with api.removing():
            removed.set()

    async with worker.pinning():
        task = asyncio.create_task(remove())
        await asyncio.sleep(1.0)
        assert not removed.is_set()
    await asyncio.wait_for(task, 10)
    assert removed.is_set()


async def test_a_pin_nested_in_one_a_removal_waits_on_completes(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    """A create holds a pin and its fetcher pins again inside it. A removal in another
    process waiting on the outer pin must not block the inner one: that would wait on
    each other for good."""
    worker, api = _gate(pg_pool, tmp_path), _gate(pg_pool, tmp_path)

    async with worker.pinning():
        removal = asyncio.create_task(_enter_removal(api))
        await asyncio.sleep(1.0)
        assert not removal.done()

        async def nested() -> None:
            async with worker.pinning():
                pass

        await asyncio.wait_for(nested(), 10)
    await asyncio.wait_for(removal, 10)


async def test_a_pin_waits_out_a_removal_in_another_process(
    pg_pool: PgPool, tmp_path: Path
) -> None:
    worker, api = _gate(pg_pool, tmp_path), _gate(pg_pool, tmp_path)
    pinned = asyncio.Event()

    async def pin() -> None:
        async with worker.pinning():
            pinned.set()

    async with api.removing():
        task = asyncio.create_task(pin())
        await asyncio.sleep(0.5)
        assert not pinned.is_set()
    await asyncio.wait_for(task, 10)
    assert pinned.is_set()


async def test_a_crashed_pinners_hold_expires(pg_pool: PgPool, tmp_path: Path) -> None:
    worker = CheckoutLeases(pg_pool, tmp_path, ttl=0.5)
    api = _gate(pg_pool, tmp_path)

    # Taken and never renewed nor released: the process died mid-pin.
    worker.take_pin()

    await asyncio.wait_for(_enter_removal(api), 10)


async def test_a_live_pin_is_renewed_past_its_ttl(pg_pool: PgPool, tmp_path: Path) -> None:
    worker, api = _gate(pg_pool, tmp_path, ttl=0.6), _gate(pg_pool, tmp_path)
    removed = asyncio.Event()

    async def remove() -> None:
        async with api.removing():
            removed.set()

    async with worker.pinning():
        task = asyncio.create_task(remove())
        await asyncio.sleep(1.5)
        assert not removed.is_set()
    await asyncio.wait_for(task, 10)


async def _enter_removal(gate: CheckoutGate) -> None:
    async with gate.removing():
        pass


async def test_installs_are_capped_across_processes(pg_pool: PgPool) -> None:
    """Each process builds its own permits; together they still clone at most
    ``limit`` at once (#1131)."""
    worker, api = InstallPermits(1, pg_pool), InstallPermits(1, pg_pool)
    entered = asyncio.Event()

    async def install() -> None:
        async with api.permit():
            entered.set()

    async with worker.permit():
        task = asyncio.create_task(install())
        await asyncio.sleep(1.0)
        assert not entered.is_set()
    await asyncio.wait_for(task, 10)
    assert entered.is_set()


async def test_a_live_install_is_renewed_past_its_ttl(pg_pool: PgPool) -> None:
    worker, api = InstallPermits(1, pg_pool, ttl=0.6), InstallPermits(1, pg_pool)
    entered = asyncio.Event()

    async def install() -> None:
        async with api.permit():
            entered.set()

    async with worker.permit():
        task = asyncio.create_task(install())
        await asyncio.sleep(1.5)
        assert not entered.is_set()
    await asyncio.wait_for(task, 10)


async def test_a_crashed_installers_permit_expires(pg_pool: PgPool) -> None:
    worker, api = InstallPermits(1, pg_pool, ttl=0.5), InstallPermits(1, pg_pool)

    # Claimed and never renewed nor released: the process died mid-clone.
    assert worker.claim() is not None

    async def install() -> None:
        async with api.permit():
            pass

    await asyncio.wait_for(install(), 10)


async def test_installs_in_one_process_are_capped_without_a_database() -> None:
    permits = InstallPermits(2)
    running, most = 0, 0

    async def install() -> None:
        nonlocal running, most
        async with permits.permit():
            running += 1
            most = max(most, running)
            await asyncio.sleep(0.05)
            running -= 1

    await asyncio.gather(*(install() for _ in range(5)))
    assert most == 2
