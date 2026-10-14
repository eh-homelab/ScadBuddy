"""The library worker keeps putting the triggers' Schedules right (#2484 review): a route
that changed a row while Temporal was away (503) is not left out of step until the next
start."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from scadbuddy.flow_triggers import fire


async def test_the_schedules_are_reconciled_again_after_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passes = 0
    stop = asyncio.Event()

    async def ensure(*_: Any) -> None:
        nonlocal passes
        passes += 1
        if passes == 1:
            raise RuntimeError("Temporal is away")
        if passes == 3:
            stop.set()

    monkeypatch.setattr(fire, "ensure_trigger_schedules", ensure)
    monkeypatch.setattr(fire, "SCHEDULES_RETRY", 0.01)
    monkeypatch.setattr(fire, "RECONCILE_INTERVAL", 0.01)
    await asyncio.wait_for(fire.reconcile_trigger_schedules(object(), "library", object(), stop), 5)  # type: ignore[arg-type]
    assert passes == 3
