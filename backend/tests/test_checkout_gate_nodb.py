"""A `CheckoutGate`'s and an `InstallPermits`' renewals of their shared rows, with no
database (#6527, #6546): the wait before each renewal is the injected ``sleep``, so the
test renews on cue and reads no clock."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import uuid
from collections.abc import Callable, Collection, Sequence
from pathlib import Path

import pytest

from scadbuddy.library.libraries import CheckoutGate, InstallPermits
from tests.support.waits import reaches


class _Renewals:
    """What a fake's renewals did, in order, beside the waits before them; and a latch
    that keeps a renewal from returning until the test opens it."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.open = threading.Event()
        self.open.set()

    def renewing(self) -> None:
        self.log.append("renew")
        # The deadline only ends a test that hangs.
        if not self.open.wait(10):
            raise TimeoutError("the test never let the renewal return")
        self.log.append("landed")


class _Rows:
    """The pin holds a `SharedLeases` keeps, in memory."""

    ttl = 0.6

    def __init__(self, renewals: _Renewals | None = None) -> None:
        self.held: set[uuid.UUID] = set()
        self.renewed: list[uuid.UUID] = []
        self.fail_next = False
        self.renewals = renewals or _Renewals()

    def take(self, holder: str, checkouts: Sequence[Path]) -> uuid.UUID:
        raise NotImplementedError

    def renew(self, token: uuid.UUID) -> object:
        raise NotImplementedError

    def drop(self, token: uuid.UUID) -> None:
        raise NotImplementedError

    def take_pin(self) -> uuid.UUID:
        token = uuid.uuid4()
        self.held.add(token)
        return token

    def renew_pin(self, token: uuid.UUID) -> object:
        self.renewed.append(token)
        self.renewals.renewing()
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("the database did not answer")
        return token in self.held

    def drop_pin(self, token: uuid.UUID) -> None:
        self.held.discard(token)


class _Slots:
    """The one ``library_install_slots`` row an `InstallSlots` keeps, in memory."""

    def __init__(self, renewals: _Renewals | None = None) -> None:
        self.held: dict[int, uuid.UUID] = {}
        self.renewed: list[uuid.UUID] = []
        self.renewals = renewals or _Renewals()

    def claim(self, token: uuid.UUID) -> int | None:
        if self.held:
            return None
        self.held[0] = token
        return 0

    def renew(self, slot: int, token: uuid.UUID) -> object:
        self.renewed.append(token)
        self.renewals.renewing()
        return self.held.get(slot) == token

    def drop(self, slot: int, token: uuid.UUID) -> object:
        if self.held.get(slot) == token:
            del self.held[slot]
        return True


class _Cue:
    """An injected ``sleep`` that returns when the test says so."""

    def __init__(self, renewals: _Renewals) -> None:
        self.queue: asyncio.Queue[None] = asyncio.Queue()
        self.asked: list[float] = []
        self.renewals = renewals

    async def __call__(self, delay: float) -> None:
        self.asked.append(delay)
        self.renewals.log.append("wait")
        await self.queue.get()

    def go(self) -> None:
        self.queue.put_nowait(None)


async def test_a_pin_is_renewed_each_third_of_its_ttl_until_it_ends() -> None:
    rows = _Rows()
    cue = _Cue(rows.renewals)
    gate = CheckoutGate(rows, sleep=cue)
    async with gate.pinning():
        (token,) = rows.held
        await reaches(lambda: len(cue.asked), 1)
        assert rows.renewed == []
        # A renewal that fails is logged, and the next one still comes.
        rows.fail_next = True
        for count in (1, 2, 3):
            cue.go()
            await reaches(lambda: len(rows.renewed), count)
            await reaches(lambda: len(cue.asked), count + 1)
        assert rows.renewed == [token] * 3
        assert rows.held == {token}
    assert rows.held == set()
    assert set(cue.asked) == {rows.ttl / 3}


async def test_an_install_slot_is_renewed_each_third_of_its_ttl_until_it_ends() -> None:
    slots = _Slots()
    cue = _Cue(slots.renewals)
    permits = InstallPermits(1, slots=slots, ttl=0.6, sleep=cue)
    async with permits.permit():
        (token,) = slots.held.values()
        await reaches(lambda: len(cue.asked), 1)
        assert slots.renewed == []
        for count in (1, 2, 3):
            cue.go()
            await reaches(lambda: len(slots.renewed), count)
            await reaches(lambda: len(cue.asked), count + 1)
        assert slots.renewed == [token] * 3
        assert slots.held == {0: token}
    assert slots.held == {}
    assert set(cue.asked) == {permits.ttl / 3}


def _holder(
    kind: str, renewals: _Renewals, cue: _Cue
) -> tuple[
    Callable[[], contextlib.AbstractAsyncContextManager[None]], Callable[[], Collection[uuid.UUID]]
]:
    """A pin's hold or an install's slot, and the tokens its fake holds."""
    if kind == "pin":
        rows = _Rows(renewals)
        return CheckoutGate(rows, sleep=cue).pinning, lambda: set(rows.held)
    slots = _Slots(renewals)
    permits = InstallPermits(1, slots=slots, ttl=0.6, sleep=cue)
    return permits.permit, lambda: set(slots.held.values())


@pytest.mark.parametrize("kind", ["pin", "install"])
async def test_a_renewal_that_blocks_past_the_ttl(kind: str) -> None:
    """A renewal that blocks, as a slow connect did (#1851), for longer than the TTL:
    no clock runs here, so "past the TTL" is as long as the test keeps it blocked. All
    that time no second renewal starts beside it (renewals do not pile up threads on a
    database that does not answer). Once it lands, the next wait and renewal come as
    before, and the hold stays held. A block that ends while a renewal blocks drops
    its hold at once and does not wait for that renewal."""
    renewals = _Renewals()
    cue = _Cue(renewals)
    holding, held = _holder(kind, renewals, cue)
    try:
        async with holding():
            (token,) = held()
            await reaches(lambda: renewals.log.count("wait"), 1)
            renewals.open.clear()
            cue.go()
            await reaches(lambda: renewals.log.count("renew"), 1)
            # Blocked: the next wait comes only after it lands.
            renewals.open.set()
            await reaches(lambda: renewals.log.count("wait"), 2)
            assert renewals.log == ["wait", "renew", "landed", "wait"]
            assert held() == {token}
            renewals.open.clear()
            cue.go()
            await reaches(lambda: renewals.log.count("renew"), 2)
        # Left while the second renewal still blocks.
        assert renewals.log == ["wait", "renew", "landed", "wait", "renew"]
        assert held() == set()
    finally:
        renewals.open.set()
    await reaches(lambda: renewals.log.count("landed"), 2)
    assert set(cue.asked) == {0.6 / 3}
