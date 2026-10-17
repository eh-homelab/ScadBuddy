"""The event-loop watch (#3788): a worker whose loop stops running says so, with the
stack that holds it, while it is still held."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from scadbuddy.core.loop_watch import LoopWatch
from scadbuddy.core.metrics import Metrics


def _sample(metrics: Metrics, name: str) -> float:
    value = metrics.registry.get_sample_value(name)
    return value or 0.0


def _blocks_the_loop(seconds: float) -> None:
    time.sleep(seconds)


async def test_a_blocked_loop_is_logged_with_the_stack_that_blocks_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = Metrics()
    caplog.set_level(logging.WARNING, logger="scadbuddy.core.loop_watch")
    async with LoopWatch(metrics, threshold=0.2, interval=0.05):
        await asyncio.sleep(0.1)
        _blocks_the_loop(0.6)
        await asyncio.sleep(0.1)
    stalled = [r for r in caplog.records if "event loop" in r.getMessage()]
    assert stalled, caplog.text
    # Logged while the loop was still held, so the stack is the culprit's.
    assert "_blocks_the_loop" in stalled[0].getMessage()
    assert _sample(metrics, "scadbuddy_event_loop_stalls_total") == 1
    assert _sample(metrics, "scadbuddy_event_loop_lag_seconds_sum") >= 0.4


async def test_a_loop_that_keeps_running_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    metrics = Metrics()
    caplog.set_level(logging.WARNING, logger="scadbuddy.core.loop_watch")
    async with LoopWatch(metrics, threshold=0.2, interval=0.05):
        await asyncio.sleep(0.4)
    assert not caplog.records
    assert _sample(metrics, "scadbuddy_event_loop_stalls_total") == 0
    assert _sample(metrics, "scadbuddy_event_loop_lag_seconds_count") >= 3
