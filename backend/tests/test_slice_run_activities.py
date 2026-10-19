"""`RunActivities` and the run's native-slice mapping, without Temporal (plan
2026-10-16 Task 13 review M1, M2, M3, M5): stubs for the slicer, the store and the
libraries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import pytest
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    RetryState,
    TimeoutError,
    TimeoutType,
)
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.print_run import PlannedRun
from scadbuddy.print_library.ids import NATIVE_ID_BASE
from scadbuddy.workflows import print_activities, printing
from scadbuddy.workflows.print_models import FAILED, NativeSliced, SlicedUploadInput, SliceRunInput
from scadbuddy.workflows.slicing import SLICE_WORST_CASE, failure_status
from scadbuddy.workflows.slicing_activities import RunActivities, _refused
from scadbuddy.workflows.slicing_models import SLICE_REFUSED

COPY = NATIVE_ID_BASE + 7
#: Any starter: these tests never reach the start.
STARTER = object()
PLAN = SlicePlan(
    printer_preset=PresetRef(source="standard", id="Test Printer 0.4 nozzle"),
    process_preset=PresetRef(source="standard", id="leaf"),
    filament_presets=[PresetRef(source="standard", id="Test PETG")],
    filament_colours=["#FF0000"],
    bed_type="Textured PEI Plate",
)


class _Content:
    def __init__(self, spool: Path, payload: bytes) -> None:
        self.spool = spool
        self.payload = payload

    async def stat(self, key: str) -> Any:
        return SimpleNamespace(ref=SimpleNamespace(sha256="0" * 64, size=len(self.payload)))

    async def get(self, ref: Any) -> AsyncIterator[bytes]:
        yield self.payload


class _Native:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes, int | None]] = []

    async def library_file(self, file_id: int) -> Any:
        assert file_id == COPY
        return SimpleNamespace(id=COPY, filename="demo-elan.3mf", file_hash=None)

    async def upload(self, filename: str, data: BinaryIO, folder_id: int | None) -> Any:
        self.uploads.append((filename, data.read(), folder_id))
        return SimpleNamespace(id=COPY + 1)


def _activities(tmp_path: Path, *, native: Any = None, serving: Any = None) -> RunActivities:
    slicing: Any = SimpleNamespace(
        starter=STARTER,
        store=None,
        core=SimpleNamespace(store=SimpleNamespace(content=_Content(tmp_path, b"sliced"))),
    )

    async def serving_native() -> Any:
        if serving is not None:
            return await serving()
        return native

    library: Any = SimpleNamespace(serving_native=serving_native)
    settings: Any = SimpleNamespace(load=lambda: SimpleNamespace(bambuddy_url=None))
    return RunActivities(slicing, library, lambda: settings)


async def test_a_native_copys_sliced_file_goes_to_the_native_library(tmp_path: Path) -> None:
    """Review M5: the native-library branch, named and filed beside the copy."""
    native = _Native()
    run = _activities(tmp_path, native=native)
    planned = PlannedRun.model_construct(library_file_id=COPY, folder_id=NATIVE_ID_BASE + 3)
    sliced = NativeSliced(slice_id=1, preset_key="k", sliced_blob_key="native-slice-1")

    uploaded = await ActivityEnvironment().run(
        run.print_sliced_upload, SlicedUploadInput(planned=planned, plate_id=2, sliced=sliced)
    )

    assert uploaded == COPY + 1
    assert native.uploads == [("demo-elan.plate2.sliced.3mf", b"sliced", NATIVE_ID_BASE + 3)]


async def test_slice_run_beats_before_its_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M1: what runs before the wait (settings, the copy's read, the presets, the
    start) beats too, so a slow Bambuddy does not time the attempt out."""
    monkeypatch.setattr(print_activities, "HEARTBEAT_EVERY", 0.05)

    async def slow() -> Any:
        await asyncio.sleep(0.3)
        raise RuntimeError("stop here")

    run = _activities(tmp_path, serving=slow)
    beats: list[Any] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    with pytest.raises(RuntimeError, match="stop here"):
        await env.run(
            run.slice_run,
            SliceRunInput(library_file_id=COPY, plan=PLAN, plate_id=1, subject="out-1"),
        )
    assert beats


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_refused("plate 99 is empty"), 422),
        (_refused("The slice's record is gone: slice again.", status=409), 409),
        (_refused("the native slicer needs Bambu Studio's profiles", status=503), 503),
        (
            ApplicationError(
                "x", {"exit_code": 4, "stderr_tail": "bad"}, type="SlicerError", non_retryable=True
            ),
            502,
        ),
        (ApplicationError("x", type="SomethingElse"), 502),
    ],
)
def test_failure_status(error: ApplicationError, expected: int) -> None:
    """Review M3: only the plan's refusals are 422; a slice to start again is 409, the
    server's own lack 503, any other failure 502."""
    assert error.type in (SLICE_REFUSED, "SlicerError", "SomethingElse")
    assert failure_status(error) == expected


def _timed_out(kind: TimeoutType) -> ActivityError:
    error = ActivityError(
        "activity timed out",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="slice_run",
        activity_id="1",
        retry_state=RetryState.TIMEOUT,
    )
    error.__cause__ = TimeoutError("timed out", type=kind, last_heartbeat_details=[])
    return error


def test_a_slice_past_the_runs_bound_is_named_and_retryable() -> None:
    """Review M2: a slice that outlives every ``slice_run`` attempt fails the run with a
    504 that says to print again (which joins the slice), never the unexpected 500."""
    problem = printing.outlived_slice(_timed_out(TimeoutType.SCHEDULE_TO_CLOSE))
    assert problem is not None
    from scadbuddy.workflows.problems import problem_of

    found = problem_of(problem)
    assert found.status == 504
    assert "print again" in found.detail
    assert printing.outlived_slice(_timed_out(TimeoutType.HEARTBEAT)) is None


def test_slice_run_retries_until_the_slices_own_worst_case() -> None:
    """Review M2: rejoining is idempotent, so timeouts retry until the bound."""
    assert printing.SLICE_RUN_RETRY.maximum_attempts == 0
    assert FAILED in (printing.SLICE_RUN_RETRY.non_retryable_error_types or [])
    assert printing.SLICE_RUN_BOUND >= SLICE_WORST_CASE
