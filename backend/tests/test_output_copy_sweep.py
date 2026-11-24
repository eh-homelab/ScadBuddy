"""The API's copy of the saved outputs' files to rows (#7255) runs at start and then
once each `SCADBUDDY_OUTPUT_COPY_INTERVAL_SECONDS`, so saves and deletes of old pods
during a roll-out reach the rows; 0 is the start's run only, and the shutdown's stop ends
the loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy import main
from scadbuddy.core.settings import BOOTSTRAP_FIELDS, Settings
from scadbuddy.library.output_copy import CopyResult
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS


def _state(interval: float) -> Any:
    return SimpleNamespace(
        outputs=SimpleNamespace(rows=object()),
        projection=SimpleNamespace(pool=object()),
        settings=SimpleNamespace(output_copy_interval_seconds=interval),
    )


def _counting(monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], asyncio.Event]:
    runs: list[int] = []
    second = asyncio.Event()
    loop = asyncio.get_running_loop()

    def copy(*args: object) -> CopyResult:
        runs.append(1)
        if len(runs) >= 2:
            loop.call_soon_threadsafe(second.set)
        return CopyResult()

    monkeypatch.setattr(main, "copy_outputs", copy)
    monkeypatch.setattr(main, "COPY_STOP_POLL", 0.01)
    return runs, second


async def test_the_copy_runs_again_after_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    runs, second = _counting(monkeypatch)
    stop = threading.Event()
    task = asyncio.create_task(main._copy_outputs_logged(_state(0.02), stop=stop))
    await asyncio.wait_for(second.wait(), 5)
    assert len(runs) >= 2
    await main._join_background(stop, task)
    assert task.done() and not task.cancelled()


async def test_stop_ends_the_wait_between_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    runs, _ = _counting(monkeypatch)
    stop = threading.Event()
    task = asyncio.create_task(main._copy_outputs_logged(_state(3600), stop=stop))
    while not runs:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert runs == [1]


async def test_interval_zero_is_the_start_only(monkeypatch: pytest.MonkeyPatch) -> None:
    runs, _ = _counting(monkeypatch)
    await asyncio.wait_for(main._copy_outputs_logged(_state(0), stop=threading.Event()), 5)
    assert runs == [1]


async def test_a_failed_run_is_tried_again_at_the_next_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    second = asyncio.Event()
    loop = asyncio.get_running_loop()

    def copy(*args: object) -> CopyResult:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the volume went")
        loop.call_soon_threadsafe(second.set)
        return CopyResult()

    monkeypatch.setattr(main, "copy_outputs", copy)
    monkeypatch.setattr(main, "COPY_STOP_POLL", 0.01)
    stop = threading.Event()
    task = asyncio.create_task(main._copy_outputs_logged(_state(0.02), stop=stop))
    await asyncio.wait_for(second.wait(), 5)
    await main._join_background(stop, task)
    assert len(calls) >= 2


def test_the_interval_setting_defaults_to_ten_minutes() -> None:
    settings = Settings(database_url=UNUSED_DATABASE_URL, temporal_address=UNUSED_TEMPORAL_ADDRESS)
    assert settings.output_copy_interval_seconds == 600
    assert "output_copy_interval_seconds" in BOOTSTRAP_FIELDS


def test_the_interval_setting_is_never_negative() -> None:
    with pytest.raises(ValueError, match="output_copy_interval_seconds"):
        Settings(
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            output_copy_interval_seconds=-1,
        )
