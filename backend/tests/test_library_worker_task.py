"""The API's library worker task (#1054): like the print worker's (review #1061 1a), a
worker that fails while it runs is reported at once and started again."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy import main
from tests.test_print_worker_task import StubWorker


async def test_a_library_worker_that_fails_while_running_is_started_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    second = asyncio.Event()
    built: list[StubWorker] = []

    def build(*args: Any, **kwargs: Any) -> StubWorker:
        built.append(StubWorker(False, second if built else asyncio.Event(), fail_after=not built))
        return built[-1]

    async def no_schedules(*args: Any) -> None:
        return None

    monkeypatch.setattr(main, "Worker", build)
    monkeypatch.setattr(main, "PRINT_WORKER_RECONNECT", 0.01)
    monkeypatch.setattr(main, "ensure_schedules", no_schedules)
    monkeypatch.setattr(main, "_housekeeping_activities", lambda state: [])
    state = SimpleNamespace(
        settings=SimpleNamespace(temporal_task_queue_library="library"),
        temporal=object(),
        config=SimpleNamespace(asset_sweep_interval=0),
        operations=SimpleNamespace(store=None, kinds={}),
        settings_store=None,
    )
    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR, logger="scadbuddy.main"):
        task = asyncio.create_task(main._run_library_worker(state, stop))  # type: ignore[arg-type]
        await asyncio.wait_for(second.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 5)
    assert len(built) == 2
    assert built[1].stopped.is_set()
    assert "the library worker failed" in caplog.text


async def test_a_library_worker_still_connecting_stops_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connecting = asyncio.Event()

    async def never(*args: Any, **kwargs: Any) -> Any:
        connecting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "connect", never)
    state = SimpleNamespace(
        settings=SimpleNamespace(
            temporal_task_queue_library="library",
            temporal_address="unused:7233",
            temporal_namespace="default",
        ),
        temporal=None,
        config=SimpleNamespace(asset_sweep_interval=0),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(main._run_library_worker(state, stop))  # type: ignore[arg-type]
    await asyncio.wait_for(connecting.wait(), 5)
    stop.set()
    await asyncio.wait_for(task, 1)
