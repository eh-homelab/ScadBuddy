"""`copy_leases.hold_lease` over an in-memory `LeaseCalls` (#5298 Task 27): a section
cancelled while it holds the lease releases it, and so does an acquire cancelled or
failed after the API granted it, whose answer never came back."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from scadbuddy.bambuddy.copy_leases import hold_lease

KEY = "native:output:abc:7"


class MemoryLeases:
    """Grants every acquire. With ``then`` set, an acquire grants and then hangs
    (``"hang"``) or fails (``"fail"``), as one whose answer was lost does."""

    def __init__(self, then: str | None = None) -> None:
        self.held: dict[str, str] = {}
        self.then = then
        self.granted = asyncio.Event()

    async def acquire(self, key: str, token: str, ttl: float, wait: float) -> datetime | None:
        self.held[key] = token
        self.granted.set()
        if self.then == "hang":
            await asyncio.Event().wait()
        if self.then == "fail":
            raise ConnectionError("the answer was lost")
        return datetime.now(UTC)

    async def renew(self, key: str, token: str, ttl: float) -> datetime | None:
        return datetime.now(UTC) if self.held.get(key) == token else None

    async def release(self, key: str, token: str) -> None:
        if self.held.get(key) == token:
            del self.held[key]


async def test_a_cancelled_section_releases_its_lease() -> None:
    leases = MemoryLeases()
    entered = asyncio.Event()

    async def section() -> None:
        async with hold_lease(leases, KEY, renew_every=0.05):
            entered.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(section())
    await entered.wait()
    assert KEY in leases.held
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert leases.held == {}


async def test_an_acquire_cancelled_after_its_grant_releases_the_lease() -> None:
    leases = MemoryLeases(then="hang")

    async def section() -> None:
        async with hold_lease(leases, KEY):
            raise AssertionError("never entered")

    task = asyncio.create_task(section())
    await leases.granted.wait()
    assert KEY in leases.held
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert leases.held == {}


async def test_an_acquire_failed_after_its_grant_releases_the_lease() -> None:
    leases = MemoryLeases(then="fail")
    with pytest.raises(ConnectionError):
        async with hold_lease(leases, KEY):
            raise AssertionError("never entered")
    assert leases.held == {}
