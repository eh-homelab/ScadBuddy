"""The library worker keeps putting the triggers' Schedules right (#2484 review): a route
that changed a row while Temporal was away (503) is not left out of step until the next
start."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.service import RPCError, RPCStatusCode

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


class _Listed:
    def __init__(self, *ids: str) -> None:
        self.triggers = [SimpleNamespace(id=i) for i in ids]

    async def list_triggers(self) -> list[Any]:
        return self.triggers


async def test_one_refused_schedule_never_stops_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2729 review: a spec Temporal refuses is logged and left; every other trigger is
    still put right, and the pass does not ask to be retried soon."""
    ensured: list[str] = []

    async def ensure(_: Any, trigger: Any, __: str) -> None:
        if trigger.id == "bad":
            raise RPCError("bad cron", RPCStatusCode.INVALID_ARGUMENT, b"")
        ensured.append(trigger.id)

    monkeypatch.setattr(fire, "ensure_trigger_schedule", ensure)
    await fire.ensure_trigger_schedules(object(), "library", _Listed("new", "bad", "old"))  # type: ignore[arg-type]
    assert ensured == ["new", "old"]


async def test_temporal_away_tries_every_trigger_then_asks_for_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tried: list[str] = []

    async def ensure(_: Any, trigger: Any, __: str) -> None:
        tried.append(trigger.id)
        if trigger.id == "first":
            raise RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")

    monkeypatch.setattr(fire, "ensure_trigger_schedule", ensure)
    with pytest.raises(RPCError):
        await fire.ensure_trigger_schedules(object(), "library", _Listed("first", "second"))  # type: ignore[arg-type]
    assert tried == ["first", "second"]
