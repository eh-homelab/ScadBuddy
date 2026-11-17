"""A `CheckoutGate`'s renewals of its shared rows, with no database (#6527): the wait
before each renewal is the gate's injected ``sleep``, so the test renews on cue and
reads no clock."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from scadbuddy.library.libraries import CheckoutGate


class _Rows:
    """The pin holds a `SharedLeases` keeps, in memory."""

    ttl = 0.6

    def __init__(self) -> None:
        self.held: set[uuid.UUID] = set()
        self.renewed: list[uuid.UUID] = []
        self.fail_next = False

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
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("the database did not answer")
        return token in self.held

    def drop_pin(self, token: uuid.UUID) -> None:
        self.held.discard(token)


async def _reaches(count: Callable[[], int], at_least: int) -> None:
    """Wait until ``count()`` is ``at_least``; the deadline only ends a test that hangs."""
    async with asyncio.timeout(10):
        while count() < at_least:
            await asyncio.sleep(0.01)


async def test_a_pin_is_renewed_each_third_of_its_ttl_until_it_ends() -> None:
    rows = _Rows()
    cue: asyncio.Queue[None] = asyncio.Queue()
    asked: list[float] = []

    async def on_cue(delay: float) -> None:
        asked.append(delay)
        await cue.get()

    gate = CheckoutGate(rows, sleep=on_cue)
    async with gate.pinning():
        (token,) = rows.held
        await _reaches(lambda: len(asked), 1)
        assert rows.renewed == []
        # A renewal that fails is logged, and the next one still comes.
        rows.fail_next = True
        for count in (1, 2, 3):
            cue.put_nowait(None)
            await _reaches(lambda: len(rows.renewed), count)
            await _reaches(lambda: len(asked), count + 1)
        assert rows.renewed == [token] * 3
        assert rows.held == {token}
    assert rows.held == set()
    assert set(asked) == {rows.ttl / 3}
