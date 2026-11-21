"""A `CheckoutGate`'s and an `InstallPermits`' renewals of their shared rows, with no
database (#6527, #6546): the wait before each renewal is the injected ``sleep``, so the
test renews on cue and reads no clock. A renewal that finds its row gone ends the block
(#6560)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from collections.abc import Callable, Collection, Iterator, Sequence
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.libraries import install_library_handlers
from scadbuddy.core.problems import ApiError
from scadbuddy.library import libraries
from scadbuddy.library.libraries import CheckoutGate, HoldLostError, InstallPermits
from scadbuddy.library.operations import answered_as_routes
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
        self.taken: list[uuid.UUID] = []
        self.renewed: list[uuid.UUID] = []
        self.fail_next = False
        self.renewals = renewals or _Renewals()

    def take(self, holder: str, checkouts: Sequence[Path]) -> uuid.UUID:
        raise NotImplementedError

    def renew(self, token: uuid.UUID) -> bool:
        raise NotImplementedError

    def drop(self, token: uuid.UUID) -> None:
        raise NotImplementedError

    def take_pin(self) -> uuid.UUID:
        token = uuid.uuid4()
        self.held.add(token)
        self.taken.append(token)
        return token

    def renew_pin(self, token: uuid.UUID) -> bool:
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
        self.claimed: list[uuid.UUID] = []
        self.renewed: list[uuid.UUID] = []
        self.renewals = renewals or _Renewals()

    def claim(self, token: uuid.UUID) -> int | None:
        self.claimed.append(token)
        if self.held:
            return None
        self.held[0] = token
        return 0

    def renew(self, slot: int, token: uuid.UUID) -> bool:
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
            # Blocked: no second wait or renewal starts while it blocks (#6566).
            assert renewals.log == ["wait", "renew"]
            # The next wait comes only after it lands.
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


#: What `_renewed` logs when a renewal finds its row gone (`libraries.py`).
LOST = "a renewal found its row gone; the block that held it ends"


def _lost(records: Sequence[logging.LogRecord]) -> list[tuple[str, str]]:
    """The row and holder of each renewal logged as having found its row gone."""
    return [
        (record.__dict__["row"], record.__dict__["holder"])
        for record in records
        if record.levelno == logging.ERROR and record.getMessage() == LOST
    ]


@pytest.mark.parametrize("finishes", [False, True], ids=["waiting", "finishing"])
async def test_a_pin_whose_hold_lapsed_ends_with_an_error(
    finishes: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """The hold lapsed (a long pause) and is gone when the renewal comes (#6560): the
    pin must not go on, as a removal may now delete the checkout it reads. The block is
    cancelled where it waits, and ends with `HoldLostError` even when it takes that
    cancel and finishes; the loss is logged with the row and the holder, and the hold is
    not taken again."""
    rows = _Rows()
    cue = _Cue(rows.renewals)
    gate = CheckoutGate(rows, sleep=cue)
    ran_on = False
    with (
        caplog.at_level(logging.ERROR, logger=libraries.__name__),
        pytest.raises(HoldLostError) as raised,
    ):
        async with gate.pinning():
            (token,) = rows.held
            await reaches(lambda: len(cue.asked), 1)
            rows.held.clear()
            cue.go()
            # The deadline only ends a test whose block is never ended.
            async with asyncio.timeout(10):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if not finishes:
                        raise
            ran_on = True
    assert ran_on is finishes
    # Ended by the lost hold's cancel, not by the test's deadline.
    cause = raised.value.__cause__
    assert cause is None if finishes else isinstance(cause, asyncio.CancelledError)
    assert raised.value.row == "pin hold"
    assert raised.value.holder == str(token)
    assert _lost(caplog.records) == [("pin hold", str(token))]
    assert rows.taken == [token]
    assert rows.renewed == [token]
    assert rows.held == set()
    # The task that ran the block is not left cancelling.
    current = asyncio.current_task()
    assert current is not None
    assert current.cancelling() == 0


@pytest.mark.parametrize("finishes", [False, True], ids=["waiting", "finishing"])
async def test_an_install_whose_slot_was_taken_ends_with_an_error(
    finishes: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """The slot lapsed and another install claimed it before the renewal came (#6560):
    two clones would now share one slot. The block ends with `HoldLostError`, the loss is
    logged with the row and the holder, the slot is not claimed again, and the release
    leaves the other install's slot alone."""
    slots = _Slots()
    cue = _Cue(slots.renewals)
    permits = InstallPermits(1, slots=slots, ttl=0.6, sleep=cue)
    other = uuid.uuid4()
    ran_on = False
    with (
        caplog.at_level(logging.ERROR, logger=libraries.__name__),
        pytest.raises(HoldLostError) as raised,
    ):
        async with permits.permit():
            (token,) = slots.held.values()
            await reaches(lambda: len(cue.asked), 1)
            slots.held[0] = other
            cue.go()
            async with asyncio.timeout(10):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if not finishes:
                        raise
            ran_on = True
    assert ran_on is finishes
    # Ended by the lost hold's cancel, not by the test's deadline.
    cause = raised.value.__cause__
    assert cause is None if finishes else isinstance(cause, asyncio.CancelledError)
    assert raised.value.row == "library install slot"
    assert raised.value.holder == str(token)
    assert _lost(caplog.records) == [("library install slot", str(token))]
    assert slots.claimed == [token]
    assert slots.renewed == [token]
    assert slots.held == {0: other}
    current = asyncio.current_task()
    assert current is not None
    assert current.cancelling() == 0


async def test_a_cancel_from_outside_is_not_taken_for_a_lost_hold() -> None:
    """A block cancelled from outside after its hold was lost still ends cancelled: the
    lost hold takes back only its own cancel (#6560)."""
    rows = _Rows()
    cue = _Cue(rows.renewals)
    gate = CheckoutGate(rows, sleep=cue)
    inside, caught = asyncio.Event(), asyncio.Event()

    async def pin() -> None:
        async with gate.pinning():
            await reaches(lambda: len(cue.asked), 1)
            inside.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Taken, so the block waits on to receive the outside cancel. The
                # deadline only ends a test whose outside cancel never comes.
                caught.set()
                async with asyncio.timeout(10):
                    await asyncio.Event().wait()

    task = asyncio.create_task(pin())
    async with asyncio.timeout(10):
        await inside.wait()
        rows.held.clear()
        cue.go()
        await caught.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert rows.held == set()


async def test_a_pin_and_a_slot_lost_in_one_tick_end_with_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fetch holds an install slot inside a pin (`CheckoutFetcher.fetch`), and both are
    lost at once (#6560). The two renewals share one cue and run in threads, so the
    order is not fixed: the slot's loss can land first, the pin's can, or both cancels
    can arrive before the block runs and count as one. On every path the fetch ends with
    `HoldLostError`, each loss a renewal saw is logged once, the pin is released and the
    task is not left cancelling. The order where the pin is lost while the inner block
    already ends is fixed in
    `test_a_pin_lost_while_the_inner_slot_is_released_ends_with_an_error`."""
    renewals = _Renewals()
    cue = _Cue(renewals)
    rows, slots = _Rows(renewals), _Slots(renewals)
    gate = CheckoutGate(rows, sleep=cue)
    permits = InstallPermits(1, slots=slots, ttl=0.6, sleep=cue)
    with (
        caplog.at_level(logging.ERROR, logger=libraries.__name__),
        pytest.raises(HoldLostError),
    ):
        async with gate.pinning(), permits.permit():
            await reaches(lambda: len(cue.asked), 2)
            rows.held.clear()
            slots.held[0] = uuid.uuid4()
            cue.go()
            cue.go()
            # The deadline only ends a test whose block is never ended.
            async with asyncio.timeout(10):
                await asyncio.Event().wait()
    # Each loss that a renewal saw before the blocks ended is logged once.
    lost = [row for row, _ in _lost(caplog.records)]
    assert lost and set(lost) <= {"pin hold", "library install slot"}
    assert len(lost) == len(set(lost))
    assert rows.held == set()
    current = asyncio.current_task()
    assert current is not None
    assert current.cancelling() == 0


@contextlib.contextmanager
def _removed(handler: logging.Handler) -> Iterator[None]:
    """Remove ``handler`` from the library's logger at the end."""
    try:
        yield
    finally:
        logging.getLogger(libraries.__name__).removeHandler(handler)


async def test_a_pin_lost_while_the_inner_slot_is_released_ends_with_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The slot inside a pin is lost first; the pin is lost while the inner block is
    already ending, during the slot's release (#6585 review). The inner block takes back
    its own cancel before it awaits anything, so the pin's cancel is the only one left,
    and the outer block takes it back: the fetch ends with `HoldLostError`, not a plain
    cancel, and the task is not left cancelling."""
    renewals = _Renewals()
    pin_cue, slot_cue = _Cue(renewals), _Cue(renewals)
    rows = _Rows(renewals)
    loop = asyncio.get_running_loop()
    pin_lost = threading.Event()

    class _PinLoss(logging.Handler):
        """Set when the pin's renewal logs its loss: it cancels the block in the same
        step, so the cancel is sent before the release below returns. A flag set in the
        renew's thread came too early: the release could return, and the inner block's
        error reach the outer one, before the renewal's coroutine sent the cancel."""

        def emit(self, record: logging.LogRecord) -> None:
            if record.__dict__.get("row") == "pin hold" and record.getMessage() == LOST:
                pin_lost.set()

    class _LateSlots(_Slots):
        def drop(self, slot: int, token: uuid.UUID) -> object:
            # Lose the pin now, and let its renewal cancel the block before the release
            # returns.
            rows.held.clear()
            loop.call_soon_threadsafe(pin_cue.go)
            if not pin_lost.wait(10):
                raise TimeoutError("the pin's renewal never came")
            return super().drop(slot, token)

    slots = _LateSlots(renewals)
    gate = CheckoutGate(rows, sleep=pin_cue)
    permits = InstallPermits(1, slots=slots, ttl=0.6, sleep=slot_cue)

    handler = _PinLoss(logging.ERROR)
    logging.getLogger(libraries.__name__).addHandler(handler)
    with (
        contextlib.closing(handler),
        _removed(handler),
        caplog.at_level(logging.ERROR, logger=libraries.__name__),
        pytest.raises(HoldLostError) as raised,
    ):
        async with gate.pinning(), permits.permit():
            await reaches(lambda: len(pin_cue.asked) + len(slot_cue.asked), 2)
            slots.held[0] = uuid.uuid4()
            # Only the slot's renewal is cued: the pin's waits for the release.
            slot_cue.go()
            async with asyncio.timeout(10):
                await asyncio.Event().wait()
    assert raised.value.row == "pin hold"
    assert [row for row, _ in _lost(caplog.records)] == ["library install slot", "pin hold"]
    assert rows.held == set()
    current = asyncio.current_task()
    assert current is not None
    assert current.cancelling() == 0


async def test_a_lost_hold_is_a_503_to_try_again() -> None:
    """A route or an operation whose hold was lost answers a 503 that says so, not the
    500 of an unexpected error (#6560)."""
    lost = HoldLostError("pin hold", "a-token")

    async def run() -> None:
        raise lost

    with pytest.raises(ApiError) as answered:
        await answered_as_routes(run)()
    assert answered.value.status == 503
    assert answered.value.detail == str(lost)

    app = FastAPI()
    install_library_handlers(app)

    @app.get("/pin")
    async def pin() -> None:
        raise lost

    response = await asyncio.to_thread(
        lambda: TestClient(app, raise_server_exceptions=False).get("/pin")
    )
    assert response.status_code == 503
    assert response.json()["detail"] == str(lost)
    assert "try again" in str(lost)
