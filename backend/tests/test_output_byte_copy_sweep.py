"""The byte copy of the saved outputs to the blob store (#7256) runs once at the API's
start, after the copy to rows, and then as the housekeeping sweep `OUTPUT_BYTES_SWEEP`,
so the files that pods on an older build wrote reach the blob store by the next sweep."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy import main
from scadbuddy.library.output_byte_copy import ByteCopyResult
from scadbuddy.workflows.housekeeping import OUTPUT_BYTES_SWEEP, OUTPUT_COPY_SWEEP, SWEEPS

#: The last row copy run of a start whose row copy finished.
FINISHED_RUN = {"finished_at": datetime(2026, 11, 24, tzinfo=UTC), "incomplete": False}


def _rows(last_run: dict[str, Any] | None = FINISHED_RUN) -> Any:
    return SimpleNamespace(last_run=lambda: last_run)


def _state(rows: object | None = None) -> Any:
    return SimpleNamespace(
        outputs=SimpleNamespace(rows=_rows() if rows is None else rows),
        projection=SimpleNamespace(conninfo="postgresql://unused.invalid/scadbuddy"),
        store=SimpleNamespace(content=object()),
    )


def _recording(
    monkeypatch: pytest.MonkeyPatch, result: ByteCopyResult | None
) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def copy(*args: Any) -> ByteCopyResult | None:
        calls.append(args)
        return result

    monkeypatch.setattr(main, "copy_output_bytes", copy)
    return calls


def _sweep(state: Any) -> Any:
    return dict(zip(SWEEPS, main._housekeeping_activities(state), strict=True))[OUTPUT_BYTES_SWEEP]


def test_the_sweep_follows_the_row_copy() -> None:
    assert SWEEPS.index(OUTPUT_BYTES_SWEEP) == SWEEPS.index(OUTPUT_COPY_SWEEP) + 1


async def test_the_sweep_runs_the_copy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _recording(monkeypatch, ByteCopyResult(copied=2))
    state = _state()
    with caplog.at_level(logging.INFO, logger="scadbuddy.main"):
        await ActivityEnvironment().run(_sweep(state))
    assert len(calls) == 1
    store, rows, content, conninfo, stop = calls[0]
    assert (store, rows, content, conninfo) == (
        state.outputs,
        state.outputs.rows,
        state.store.content,
        state.projection.conninfo,
    )
    assert isinstance(stop, threading.Event) and not stop.is_set()
    assert "copied the outputs' bytes to the blob store" in [r.getMessage() for r in caplog.records]


async def test_a_failed_sweep_is_logged_and_fails_its_activity(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def copy(*args: object) -> ByteCopyResult:
        raise OSError("the volume went")

    monkeypatch.setattr(main, "copy_output_bytes", copy)
    with caplog.at_level(logging.ERROR, logger="scadbuddy.main"), pytest.raises(OSError):
        await ActivityEnvironment().run(_sweep(_state()))
    assert [r.getMessage() for r in caplog.records] == [
        "could not copy the outputs' bytes to the blob store; the next sweep or start retries"
    ]


async def test_the_sweep_does_nothing_without_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recording(monkeypatch, ByteCopyResult())
    state = _state()
    state.outputs.rows = None
    await ActivityEnvironment().run(_sweep(state))
    assert calls == []


async def test_the_start_copies_the_rows_then_the_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    async def rows(state: Any, *, stop: threading.Event) -> None:
        order.append("rows")

    async def copy(*args: Any) -> ByteCopyResult:
        order.append("bytes")
        raise OSError("the store went")  # logged only

    monkeypatch.setattr(main, "_copy_outputs_logged", rows)
    monkeypatch.setattr(main, "copy_output_bytes", copy)
    await main._copy_outputs_and_bytes_logged(_state(), stop=threading.Event())
    assert order == ["rows", "bytes"]


async def test_a_stopped_start_skips_the_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recording(monkeypatch, ByteCopyResult())
    stop = threading.Event()

    async def rows(state: Any, *, stop: threading.Event) -> None:
        stop.set()

    monkeypatch.setattr(main, "_copy_outputs_logged", rows)
    await main._copy_outputs_and_bytes_logged(_state(), stop=stop)
    assert calls == []


@pytest.mark.parametrize(
    "last_run",
    [
        None,
        {"finished_at": None, "incomplete": False},
        {"finished_at": None, "incomplete": True},
    ],
    ids=["no run", "not finished", "incomplete"],
)
async def test_the_start_skips_the_bytes_after_a_row_copy_that_did_not_finish(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    last_run: dict[str, Any] | None,
) -> None:
    """The byte copy walks the rows: after a row copy that did not finish (a volume not
    there, a failed listing), the start leaves it to the sweep."""
    calls = _recording(monkeypatch, ByteCopyResult())

    async def rows(state: Any, *, stop: threading.Event) -> None:
        pass

    monkeypatch.setattr(main, "_copy_outputs_logged", rows)
    with caplog.at_level(logging.INFO, logger="scadbuddy.main"):
        await main._copy_outputs_and_bytes_logged(_state(_rows(last_run)), stop=threading.Event())
    assert calls == []
    assert "the row copy did not finish; the start does not copy the outputs' bytes" in [
        r.getMessage() for r in caplog.records
    ]


async def test_a_cancelled_sweep_sets_its_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sweep cancelled by its timeout, not only by the worker's shutdown, sets its stop,
    so the threads the run started end between outputs."""
    started = asyncio.Event()
    stops: list[threading.Event] = []

    async def copy(*args: Any) -> None:
        stops.append(args[4])
        started.set()
        await asyncio.sleep(5)

    monkeypatch.setattr(main, "copy_output_bytes", copy)
    env = ActivityEnvironment()
    run = asyncio.create_task(env.run(_sweep(_state())))
    await asyncio.wait_for(started.wait(), 5)
    env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert stops[0].is_set()
