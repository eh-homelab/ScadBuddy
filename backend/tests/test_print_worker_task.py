"""The API's print worker task (#1052): a worker that fails is reported when it fails
and started again, so print runs do not wait on a dead queue until the next restart."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest

from scadbuddy import main


class StubWorker:
    def __init__(self, fail: bool, entered: asyncio.Event) -> None:
        self.fail = fail
        self.entered = entered

    async def __aenter__(self) -> StubWorker:
        if self.fail:
            raise RuntimeError("the worker could not poll")
        self.entered.set()
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


async def test_a_failed_worker_is_logged_at_once_and_started_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    entered = asyncio.Event()
    built: list[StubWorker] = []

    def build(*args: Any, **kwargs: Any) -> StubWorker:
        built.append(StubWorker(fail=not built, entered=entered))
        return built[-1]

    monkeypatch.setattr(main, "bambuddy_worker", build)
    monkeypatch.setattr(main, "PRINT_WORKER_RECONNECT", 0.01)
    state = SimpleNamespace(
        settings=SimpleNamespace(temporal_task_queue_bambuddy="bambuddy"),
        temporal=object(),
        settings_store=None,
        outputs=None,
        uploads=None,
        catalogue=None,
        print_runs=SimpleNamespace(store=None),
        operations=SimpleNamespace(store=None, kinds={}),
        print_progress=None,
        print_watcher=None,
    )
    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR, logger="scadbuddy.main"):
        task = asyncio.create_task(main._run_print_worker(state, stop))  # type: ignore[arg-type]
        await asyncio.wait_for(entered.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 5)
    assert len(built) == 2
    assert "the print worker failed" in caplog.text
