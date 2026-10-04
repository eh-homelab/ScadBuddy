"""The API's library worker task (#1054): like the print worker's (review #1061 1a), a
worker that fails while it runs is reported at once and started again."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import ActivityEnvironment

from scadbuddy import main
from scadbuddy.workflows.housekeeping import SWEEPS
from tests.test_print_worker_task import StubWorker


async def test_a_library_worker_that_fails_while_running_is_started_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    second = asyncio.Event()
    built: list[StubWorker] = []

    def build(*args: Any, **kwargs: Any) -> StubWorker:
        built.append(StubWorker(False, second if built else asyncio.Event(), fail_after=not built))
        return built[-1]

    async def no_schedules(*args: Any) -> bool:
        return False

    monkeypatch.setattr(main, "Worker", build)
    monkeypatch.setattr(main, "PRINT_WORKER_RECONNECT", 0.01)
    monkeypatch.setattr(main, "ensure_schedules", no_schedules)
    monkeypatch.setattr(main, "_housekeeping_activities", lambda state: [])
    state = SimpleNamespace(
        settings=SimpleNamespace(temporal_task_queue_library="library"),
        temporal=object(),
        config=SimpleNamespace(asset_sweep_interval=0),
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


async def test_the_schedules_are_set_up_once_temporal_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review I2: a create that fails after the connect (a frontend up before its
    history service) is retried, not left to the next restart."""
    calls: list[float] = []

    async def flaky(client: object, queue: str, interval: float) -> bool:
        calls.append(interval)
        if len(calls) == 1:
            raise RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
        return False

    monkeypatch.setattr(main, "ensure_schedules", flaky)
    monkeypatch.setattr(main, "PRINT_WORKER_RECONNECT", 0.01)
    stop = asyncio.Event()
    await asyncio.wait_for(main._set_up_housekeeping(object(), "library", 600.0, stop), 5)  # type: ignore[arg-type]
    assert calls == [600.0, 600.0]


@pytest.mark.parametrize("paused", [True, False])
async def test_a_paused_schedule_leaves_the_uploads_backfill_to_the_boot(
    paused: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1095 2: the Schedule's sweep is what backfills the uploads; while an
    operator keeps it paused, the start does that instead."""
    set_up = asyncio.Event()
    backfilled: list[object] = []

    async def schedules(client: object, queue: str, interval: float) -> bool:
        set_up.set()
        return paused

    async def backfill(assets: object) -> int:
        backfilled.append(assets)
        return 0

    monkeypatch.setattr(main, "Worker", lambda *a, **k: StubWorker(False, asyncio.Event()))
    monkeypatch.setattr(main, "ensure_schedules", schedules)
    monkeypatch.setattr(main, "_housekeeping_activities", lambda state: [])
    state = SimpleNamespace(
        settings=SimpleNamespace(temporal_task_queue_library="library"),
        temporal=object(),
        config=SimpleNamespace(asset_sweep_interval=600.0),
        store=SimpleNamespace(
            content=object(), remote_assets=SimpleNamespace(backfill=backfill), fonts=None
        ),
        assets=object(),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(main._run_library_worker(state, stop))  # type: ignore[arg-type]
    await asyncio.wait_for(set_up.wait(), 5)
    for _ in range(10):
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert backfilled == ([state.assets] if paused else [])


def _broken(error: Exception) -> Callable[..., Any]:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    return fail


async def _broken_async(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("the projection is gone")


@pytest.mark.parametrize("staging_error", [OSError("read-only"), ValueError("a bad name")])
@pytest.mark.parametrize("sweep", SWEEPS)
async def test_a_failing_sweep_fails_its_activity(
    sweep: str,
    staging_error: Exception,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review #1095 1: a sweep that fails is logged and fails its activity, so the
    workflow reports it and Temporal's UI shows it; whatever the error (review #1095b 4)."""
    monkeypatch.setattr(main, "_remote_assets", lambda state: None)
    monkeypatch.setattr(main, "sweep_assets", _broken(RuntimeError("the volume is gone")))
    monkeypatch.setattr(main, "sweep_blobs", _broken(RuntimeError("the refs are gone")))
    state = SimpleNamespace(
        render=SimpleNamespace(prune=_broken_async),
        store=SimpleNamespace(content=None),
        blobs=object(),
        refs=object(),
        config=SimpleNamespace(job_ttl=60.0),
        catalogue=SimpleNamespace(sweep_duplicate_staging=_broken(staging_error)),
    )
    activities = dict(zip(SWEEPS, main._housekeeping_activities(state), strict=True))  # type: ignore[arg-type]
    with (
        caplog.at_level(logging.ERROR, logger="scadbuddy.main"),
        pytest.raises((RuntimeError, OSError, ValueError)),
    ):
        await ActivityEnvironment().run(activities[sweep])
    assert caplog.records, "the failure is logged too"


async def test_a_long_sweep_heartbeats_while_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review #1095 2: a lost worker is noticed within the heartbeat timeout, not the
    sweep's whole start-to-close timeout."""
    monkeypatch.setattr(main, "HEARTBEAT_EVERY", 0.01)
    beats: list[object] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)

    async def slow() -> None:
        await asyncio.sleep(0.1)

    await env.run(main._heartbeating, slow())
    assert len(beats) >= 3


@pytest.mark.parametrize("name", ["print", "library"])
async def test_a_worker_that_does_not_stop_is_logged_under_its_name(
    name: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review #1095 3: the library worker's shutdown is not the print worker's."""
    monkeypatch.setattr(main, "PRINT_WORKER_STOP_TIMEOUT", 0.01)

    async def forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(forever())
    with caplog.at_level(logging.WARNING, logger="scadbuddy.main"):
        await main._stop_queue_worker(task, name)
    assert f"the {name} worker did not stop in time" in caplog.text
