"""Checkout leases in Postgres (#872): the render worker and the API each build their
own `CheckoutGate`, and a lease one takes must be seen by the other's removal."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.library import libraries
from scadbuddy.library.libraries import CheckoutGate, CheckoutLeases, InstallPermits
from tests.conftest import PgPool, open_pg_pool

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


async def test_a_removal_waiting_on_another_process_holds_off_nothing_here(
    pg_pool: PgPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removal waiting for another process's pin (a whole clone, perhaps) does not
    hold off this process's pins and renders meanwhile; it still waits for the pin
    taken here, and runs alone once it goes (#1732 review)."""
    worker, api = _gate(pg_pool, tmp_path), _gate(pg_pool, tmp_path)
    checkout = _checkout(tmp_path)
    removed, pinned, unpin = asyncio.Event(), asyncio.Event(), asyncio.Event()
    waiting = _removal_entered(api, monkeypatch)

    async def remove() -> None:
        async with api.removing():
            removed.set()

    async def render() -> None:
        async with api.rendering(JOB, [checkout]):
            pass

    async def pin() -> None:
        async with api.pinning():
            pinned.set()
            await unpin.wait()

    async with worker.pinning():
        removal = asyncio.create_task(remove())
        await _set(waiting)
        rendered = asyncio.create_task(render())
        pinning = asyncio.create_task(pin())
        await asyncio.wait({rendered, asyncio.create_task(pinned.wait())}, timeout=5)
        meanwhile = (rendered.done(), pinned.is_set())
    await asyncio.sleep(1.0)
    # The other process's pin is gone; this one's holds the removal off.
    waits_for_ours = not removed.is_set()
    unpin.set()
    await asyncio.wait_for(asyncio.gather(removal, rendered, pinning), 20)

    assert meanwhile == (True, True)
    assert waits_for_ours and removed.is_set()


async def test_a_removal_kept_waiting_says_so(
    pg_pool: PgPool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pins that never pause starve a removal; the log says it still waits."""
    monkeypatch.setattr(libraries, "REMOVAL_WAIT_LOG_EVERY", 2)
    worker = _gate(pg_pool, tmp_path)
    api = CheckoutGate(CheckoutLeases(pg_pool, tmp_path, poll=0.05))
    waiting = _removal_entered(api, monkeypatch)

    with caplog.at_level(logging.WARNING, logger=libraries.__name__):
        async with worker.pinning():
            removal = asyncio.create_task(_enter_removal(api))
            await _set(waiting)
            await asyncio.sleep(0.5)
        await asyncio.wait_for(removal, 10)

    assert any(
        record.getMessage() == "a library removal still waits for pins in flight"
        for record in caplog.records
    )


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


async def test_a_live_pin_is_renewed_past_its_ttl(
    pg_pool: PgPool, pg_conninfo: str, tmp_path: Path
) -> None:
    """Each side on its own pool, as the worker and the API are. On one, the waiting
    removal holds the pool's only open connection, so the pin's first renewal waits
    for a new one: a connect slower than the TTL's slack (0.4 s) let the hold lapse
    and the removal in (#1851)."""
    api_pool = open_pg_pool(pg_conninfo)
    try:
        worker, api = _gate(pg_pool, tmp_path, ttl=0.6), _gate(api_pool, tmp_path)
        removed = asyncio.Event()

        async def remove() -> None:
            async with api.removing():
                removed.set()

        async with worker.pinning():
            task = asyncio.create_task(remove())
            await asyncio.sleep(1.5)
            assert not removed.is_set()
        await asyncio.wait_for(task, 10)
    finally:
        api_pool.close()


async def _enter_removal(gate: CheckoutGate) -> None:
    async with gate.removing():
        pass


def _removal_entered(gate: CheckoutGate, monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Set once ``gate``'s removal is in its thread, waiting on Postgres."""
    assert gate.shared is not None
    entered, removing = threading.Event(), gate.shared.removing

    @contextlib.contextmanager
    def watched(stop: threading.Event | None = None) -> Iterator[None]:
        entered.set()
        with removing(stop):
            yield

    monkeypatch.setattr(gate.shared, "removing", watched)
    return entered


async def _set(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 10)


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


async def test_a_removal_cancelled_while_it_waits_leaves_no_lock(
    pg_pool: PgPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removal activity cancelled while it waits on another process's pin must not
    leave its thread to take the lock later and hold it for good (#1131)."""
    worker, api = _gate(pg_pool, tmp_path), _gate(pg_pool, tmp_path)
    checkout = _checkout(tmp_path)
    waiting = _removal_entered(api, monkeypatch)

    async with worker.pinning():
        removal = asyncio.create_task(_enter_removal(api))
        await _set(waiting)
        removal.cancel()
        # Returns only once the thread has stopped (`_taken`).
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(removal, 10)

    # Neither a lease nor another removal waits on a lock nobody will let go.
    async def render() -> None:
        async with worker.rendering(JOB, [checkout]):
            pass

    await asyncio.wait_for(render(), 10)
    await asyncio.wait_for(_enter_removal(worker), 10)


async def test_a_pin_cancelled_while_it_takes_its_hold_leaves_none(
    pg_pool: PgPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold's insert waits out a removal in its thread; cancelled meanwhile, the
    row it inserts afterwards is dropped, not left to block removals for a TTL."""
    worker, api = _gate(pg_pool, tmp_path), _gate(pg_pool, tmp_path)
    assert worker.shared is not None
    taking, take_pin = threading.Event(), worker.shared.take_pin

    def watched() -> Any:
        taking.set()
        return take_pin()

    monkeypatch.setattr(worker.shared, "take_pin", watched)

    async def pin() -> None:
        async with worker.pinning():
            pass

    async with api.removing():
        pinning = asyncio.create_task(pin())
        await _set(taking)
        pinning.cancel()
        # One turn of the loop delivers the cancel while the insert still waits.
        await asyncio.sleep(0)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(pinning, 10)

    # A live hold would keep this waiting for the hold's 60 s TTL.
    await asyncio.wait_for(_enter_removal(api), 10)


async def test_an_install_cancelled_while_it_claims_leaves_its_slot_free(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, api = InstallPermits(1, pg_pool), InstallPermits(1, pg_pool)
    claim, claiming, go = worker.claim, threading.Event(), threading.Event()

    def held_claim() -> Any:
        claiming.set()
        go.wait(10)
        return claim()

    monkeypatch.setattr(worker, "claim", held_claim)

    async def install() -> None:
        async with worker.permit():
            pass

    task = asyncio.create_task(install())
    await _set(claiming)
    task.cancel()
    # One turn of the loop delivers the cancel; then the claim goes on, and claims.
    await asyncio.sleep(0)
    go.set()
    # Returns only once the claim's thread has finished and been undone (`_taken`).
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)

    # A claimed slot left behind would keep this one waiting for its 60 s TTL.
    async def other() -> None:
        async with api.permit():
            pass

    await asyncio.wait_for(other(), 10)
