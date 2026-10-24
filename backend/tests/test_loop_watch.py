"""The event-loop watch (#3788): a worker whose loop stops running says so, with the
stack that holds it, while it is still held."""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from scadbuddy.core.loop_watch import LoopWatch
from scadbuddy.core.metrics import Metrics


def _sample(metrics: Metrics, name: str) -> float:
    value = metrics.registry.get_sample_value(name)
    return value or 0.0


def _watch_logs(caplog: pytest.LogCaptureFixture) -> str:
    """What the watch itself logged. caplog takes every logger's records, and a pool an
    earlier test left open logs its connect failures from a thread (``unused.invalid``,
    #4653's CI), so "logs nothing" means nothing from the watch."""
    return "\n".join(
        r.getMessage() for r in caplog.records if r.name == "scadbuddy.core.loop_watch"
    )


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
    # A threshold far above any scheduling jitter under `pytest -n`.
    async with LoopWatch(metrics, threshold=1.0, interval=0.1):
        await asyncio.sleep(0.5)
    assert not _watch_logs(caplog)
    assert _sample(metrics, "scadbuddy_event_loop_stalls_total") == 0
    assert _sample(metrics, "scadbuddy_event_loop_lag_seconds_count") >= 3


def _holds_the_executor(seconds: float) -> None:
    time.sleep(seconds)


async def test_a_full_default_executor_is_logged_with_what_holds_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`asyncio.to_thread` work waits for a free default-executor thread: an activity
    whose first await is one never starts while every thread is held (#3788's
    ``project``), with the loop itself running."""
    metrics = Metrics()
    caplog.set_level(logging.WARNING, logger="scadbuddy.core.loop_watch")
    executor = ThreadPoolExecutor(max_workers=1)
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        async with LoopWatch(metrics, threshold=0.2, interval=0.05):
            await asyncio.sleep(0.1)
            await asyncio.to_thread(_holds_the_executor, 0.6)
            await asyncio.sleep(0.2)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    held = [r for r in caplog.records if "default executor" in r.getMessage()]
    assert held, caplog.text
    assert "_holds_the_executor" in held[0].getMessage()
    assert _sample(metrics, "scadbuddy_default_executor_stalls_total") == 1
    assert _sample(metrics, "scadbuddy_event_loop_stalls_total") == 0
    assert _sample(metrics, "scadbuddy_default_executor_wait_seconds_sum") >= 0.3


async def test_leaving_the_watch_does_not_wait_for_a_held_default_executor() -> None:
    """The stall the watch reports must not hold the worker's shutdown too."""
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    watch = LoopWatch(Metrics(), threshold=2.0, interval=0.05)
    try:
        held = loop.run_in_executor(None, _holds_the_executor, 1.0)
        await watch.__aenter__()
        left = time.monotonic()
        await watch.__aexit__(None, None, None)
        assert time.monotonic() - left < 0.5
        await held
    finally:
        watch._stopping.set()
        executor.shutdown(wait=False, cancel_futures=True)


async def test_a_beat_left_unrun_after_leaving_is_no_stall(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A beat posted just before the watch is left, and never run because the loop is
    held or closing, is the shutdown, not a stall to log with every stack."""
    metrics = Metrics()
    caplog.set_level(logging.WARNING, logger="scadbuddy.core.loop_watch")
    watch = LoopWatch(metrics, threshold=0.3, interval=0.05)
    await watch.__aenter__()
    _blocks_the_loop(0.15)  # past `interval`: a beat is posted and left unrun
    await watch.__aexit__(None, None, None)  # sets the stop without yielding
    _blocks_the_loop(0.5)  # past `threshold`, with the stop set
    assert watch._thread is not None
    await asyncio.to_thread(watch._thread.join, 2)
    assert not watch._thread.is_alive()
    assert not _watch_logs(caplog)
    assert _sample(metrics, "scadbuddy_event_loop_stalls_total") == 0


async def test_a_probe_left_waiting_after_leaving_is_no_stall(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The executor's no-op, still queued behind held work when the watch is left, is
    the shutdown too: its beat runs after the stop, and must not report it."""
    metrics = Metrics()
    caplog.set_level(logging.WARNING, logger="scadbuddy.core.loop_watch")
    executor = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(executor)
    watch = LoopWatch(metrics, threshold=0.6, interval=0.05)
    try:
        held = loop.run_in_executor(None, _holds_the_executor, 2.0)
        await watch.__aenter__()
        await asyncio.sleep(0.4)  # the probe is handed, and waits under `threshold`
        _blocks_the_loop(0.2)  # a beat is posted and left unrun
        await watch.__aexit__(None, None, None)
        _blocks_the_loop(0.2)  # the probe passes `threshold`
        await asyncio.sleep(0.1)  # and the beat runs, after the stop
        assert watch._thread is not None
        # Joined on the loop, not through to_thread: the one executor thread is still
        # held, so a to_thread join would wait behind it. The watch needs nothing from
        # the loop once its beat has run.
        watch._thread.join(2)
        assert not watch._thread.is_alive()
        assert not _watch_logs(caplog)
        assert _sample(metrics, "scadbuddy_default_executor_stalls_total") == 0
        await held
    finally:
        watch._stopping.set()
        executor.shutdown(wait=False, cancel_futures=True)
