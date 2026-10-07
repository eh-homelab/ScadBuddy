"""`tests.support.deployment.wait_until_build_serves`: the api `client` fixture's wait
for the render build to serve a test's queue."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from temporalio.client import Client

from tests.support import deployment

Clock = list[float]
NO_CLIENT = cast(Client, object())


def _fake(
    monkeypatch: pytest.MonkeyPatch, answers: list[bool], steps: list[float]
) -> tuple[list[float], Clock]:
    """``build_serves`` answers ``answers`` in turn; each sleep moves the loop's clock
    by the next of ``steps`` (negative: the clock stepped back)."""
    slept: list[float] = []
    clock: Clock = [1000.0]

    async def build_serves(*_: Any) -> bool:
        return answers.pop(0)

    async def sleep(delay: float) -> None:
        slept.append(delay)
        clock[0] += steps.pop(0) if steps else delay

    monkeypatch.setattr(deployment, "build_serves", build_serves)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: clock[0])
    return slept, clock


async def test_it_backs_off_between_checks_up_to_a_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev server rate-limits Worker Deployment calls, and `make_current` needs
    them: the checks space out instead of polling at a fixed rate."""
    slept, _ = _fake(monkeypatch, [False] * 6 + [True], [])
    serves = await deployment.wait_until_build_serves(
        NO_CLIENT, "q", "b", timeout=60, poll=0.25, max_poll=2.0
    )
    assert serves
    assert slept == [0.25, 0.5, 1.0, 2.0, 2.0, 2.0]


async def test_a_clock_that_steps_back_does_not_extend_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each step is clamped at zero, so a step back costs nothing and the next forward
    steps still count: the wait ends after its bound of counted time."""
    slept, _ = _fake(monkeypatch, [False] * 10, [-11.7, 1.0, 1.0, 1.0])
    serves = await deployment.wait_until_build_serves(
        NO_CLIENT, "q", "b", timeout=2.5, poll=1.0, max_poll=1.0
    )
    assert not serves
    # -11.7 counts as 0; then 1 + 1 + 1 = 3 >= 2.5 after the fourth sleep.
    assert len(slept) == 4
