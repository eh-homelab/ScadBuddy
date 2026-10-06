"""The API's print worker task (#1052): a worker that fails is reported when it fails
and started again, so print runs do not wait on a dead queue until the next restart."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy import main
from scadbuddy.operations.component import OPERATIONS


class StubWorker:
    """A worker that fails at once (``fail``), fails once it has run (``fail_after``), or
    runs until it is shut down."""

    def __init__(self, fail: bool, entered: asyncio.Event, *, fail_after: bool = False) -> None:
        self.fail = fail
        self.fail_after = fail_after
        self.entered = entered
        self.stopped = asyncio.Event()

    async def run(self) -> None:
        if self.fail:
            raise RuntimeError("the worker could not poll")
        self.entered.set()
        if self.fail_after:
            await asyncio.sleep(0)
            raise RuntimeError("the worker's poller died")
        await self.stopped.wait()

    async def shutdown(self) -> None:
        self.stopped.set()


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
    monkeypatch.setattr(main, "reconcile_lost_runs", _no_lost_runs)
    monkeypatch.setattr(main, "reconcile_lost_operations", _no_lost_runs)
    state = _state()
    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR, logger="scadbuddy.main"):
        task = asyncio.create_task(main._run_print_worker(state, stop))  # type: ignore[arg-type]
        await asyncio.wait_for(entered.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 5)
    assert len(built) == 2
    assert "the print worker failed" in caplog.text


async def _no_lost_runs(*args: Any, **kwargs: Any) -> int:
    return 0


#: The operations component's value, as the worker task reads it.
OPS = SimpleNamespace(store="operations", kinds={})


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(temporal_task_queue_bambuddy="bambuddy"),
        temporal=object(),
        settings_store=None,
        outputs=None,
        uploads=None,
        catalogue=None,
        print_runs=SimpleNamespace(store=None),
        print_progress=None,
        print_watcher=None,
        print_links=None,
        components=SimpleNamespace(get=lambda key: OPS if key is OPERATIONS else None),
    )


async def test_the_worker_task_ends_lost_runs_at_start_and_on_its_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #1061 F1: a run whose execution closed without ending it is ended."""
    passes = asyncio.Event()
    calls: list[object] = []

    async def reconcile(client: object, store: object, **kwargs: Any) -> int:
        calls.append(store)
        if len(calls) >= 2:
            passes.set()
        return 0

    monkeypatch.setattr(main, "bambuddy_worker", lambda *a, **k: StubWorker(False, asyncio.Event()))
    monkeypatch.setattr(main, "reconcile_lost_runs", reconcile)
    monkeypatch.setattr(main, "reconcile_lost_operations", _no_lost_runs)
    monkeypatch.setattr(main, "LOST_RUN_INTERVAL", 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(main._run_print_worker(_state(), stop))  # type: ignore[arg-type]
    await asyncio.wait_for(passes.wait(), 5)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert len(calls) >= 2


async def test_a_worker_that_fails_while_running_is_started_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review #1061 1a: a failure after the worker started, not only one entering it."""
    second = asyncio.Event()
    built: list[StubWorker] = []

    def build(*args: Any, **kwargs: Any) -> StubWorker:
        built.append(StubWorker(False, second if built else asyncio.Event(), fail_after=not built))
        return built[-1]

    monkeypatch.setattr(main, "bambuddy_worker", build)
    monkeypatch.setattr(main, "PRINT_WORKER_RECONNECT", 0.01)
    monkeypatch.setattr(main, "reconcile_lost_runs", _no_lost_runs)
    monkeypatch.setattr(main, "reconcile_lost_operations", _no_lost_runs)
    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR, logger="scadbuddy.main"):
        task = asyncio.create_task(main._run_print_worker(_state(), stop))  # type: ignore[arg-type]
        await asyncio.wait_for(second.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 5)
    assert len(built) == 2
    assert built[1].stopped.is_set()  # shut down on stop, not abandoned
    assert "the print worker failed" in caplog.text


async def test_a_worker_still_connecting_stops_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connect to a Temporal that never answers retries for a long time; the app's
    shutdown must not wait it out (each test's teardown did, 40 s)."""
    connecting = asyncio.Event()

    async def never(*args: Any, **kwargs: Any) -> Any:
        connecting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "connect", never)
    state = _state()
    state.temporal = None
    state.settings.temporal_address = "unused:7233"
    state.settings.temporal_namespace = "default"
    stop = asyncio.Event()
    task = asyncio.create_task(main._run_print_worker(state, stop))  # type: ignore[arg-type]
    await asyncio.wait_for(connecting.wait(), 5)
    stop.set()
    await asyncio.wait_for(task, 1)


async def test_the_worker_task_ends_lost_operations_on_its_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #1063 1: an operation whose execution closed after ``op_insert`` is ended
    beside the print runs, by the same loop."""
    passes = asyncio.Event()
    calls: list[object] = []

    async def reconcile(client: object, store: object, **kwargs: Any) -> int:
        calls.append(store)
        if len(calls) >= 2:
            passes.set()
        return 0

    monkeypatch.setattr(main, "bambuddy_worker", lambda *a, **k: StubWorker(False, asyncio.Event()))
    monkeypatch.setattr(main, "reconcile_lost_runs", _no_lost_runs)
    monkeypatch.setattr(main, "reconcile_lost_operations", reconcile)
    monkeypatch.setattr(main, "LOST_RUN_INTERVAL", 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(main._run_print_worker(_state(), stop))  # type: ignore[arg-type]
    await asyncio.wait_for(passes.wait(), 5)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert calls[:2] == ["operations", "operations"]
