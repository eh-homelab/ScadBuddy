"""The copy of the saved outputs' files to rows (#7255) runs once at the API's start and
then as the housekeeping sweep `OUTPUT_COPY_SWEEP`, so the saves and deletes of pods on
an older build during a roll-out reach the rows by the next sweep."""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy import main
from scadbuddy.library.output_copy import CopyResult
from scadbuddy.workflows.housekeeping import OUTPUT_COPY_SWEEP, SWEEPS


def _state(rows: object | None = None) -> Any:
    return SimpleNamespace(
        outputs=SimpleNamespace(rows=object() if rows is None else rows),
        projection=SimpleNamespace(conninfo="postgresql://unused.invalid/scadbuddy"),
    )


def _recording(monkeypatch: pytest.MonkeyPatch, result: CopyResult | None) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def copy(*args: Any) -> CopyResult | None:
        calls.append(args)
        return result

    monkeypatch.setattr(main, "copy_outputs", copy)
    return calls


def _sweep(state: Any) -> Any:
    return dict(zip(SWEEPS, main._housekeeping_activities(state), strict=True))[OUTPUT_COPY_SWEEP]


async def test_the_sweep_runs_the_copy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _recording(monkeypatch, CopyResult(copied=2))
    state = _state()
    with caplog.at_level(logging.INFO, logger="scadbuddy.main"):
        await ActivityEnvironment().run(_sweep(state))
    assert len(calls) == 1
    store, rows, conninfo, stop = calls[0]
    assert (store, rows, conninfo) == (state.outputs, state.outputs.rows, state.projection.conninfo)
    assert isinstance(stop, threading.Event) and not stop.is_set()
    assert "copied the saved outputs to rows" in [r.getMessage() for r in caplog.records]


async def test_a_sweep_that_another_replica_holds_logs_no_counts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The copy's advisory lock: a run that does not get it returns None."""
    calls = _recording(monkeypatch, None)
    with caplog.at_level(logging.INFO, logger="scadbuddy.main"):
        await ActivityEnvironment().run(_sweep(_state()))
    assert len(calls) == 1
    assert "copied the saved outputs to rows" not in [r.getMessage() for r in caplog.records]


async def test_a_failed_sweep_is_logged_and_fails_its_activity(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def copy(*args: object) -> CopyResult:
        raise OSError("the volume went")

    monkeypatch.setattr(main, "copy_outputs", copy)
    with caplog.at_level(logging.ERROR, logger="scadbuddy.main"), pytest.raises(OSError):
        await ActivityEnvironment().run(_sweep(_state()))
    assert [r.getMessage() for r in caplog.records] == [
        "could not copy the saved outputs to rows; the next sweep or start retries"
    ]


async def test_the_sweep_does_nothing_without_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recording(monkeypatch, CopyResult())
    state = _state()
    state.outputs.rows = None
    await ActivityEnvironment().run(_sweep(state))
    assert calls == []


async def test_the_start_copies_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recording(monkeypatch, CopyResult())
    stop = threading.Event()
    await main._copy_outputs_logged(_state(), stop=stop)
    assert len(calls) == 1
    assert calls[0][3] is stop


async def test_a_failed_start_copy_is_logged_and_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def copy(*args: object) -> CopyResult:
        raise OSError("the volume went")

    monkeypatch.setattr(main, "copy_outputs", copy)
    with caplog.at_level(logging.ERROR, logger="scadbuddy.main"):
        await main._copy_outputs_logged(_state(), stop=threading.Event())
    assert [r.getMessage() for r in caplog.records] == [
        "could not copy the saved outputs to rows; the next sweep or start retries"
    ]


async def test_a_cancelled_sweep_stops_its_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sweep cancelled by its timeout, not only by the worker's shutdown, sets its stop:
    the walk's thread ends between folders and frees the advisory lock."""
    started = threading.Event()
    stops: list[threading.Event] = []

    def copy(*args: Any) -> None:
        stops.append(args[3])
        started.set()
        args[3].wait(5)

    monkeypatch.setattr(main, "copy_outputs", copy)
    env = ActivityEnvironment()
    run = asyncio.create_task(env.run(_sweep(_state())))
    await asyncio.to_thread(started.wait, 5)
    env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert stops[0].is_set()
