"""The print dialog's background slice start (#2169) when it is cancelled (#5445)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.bambuddy import preview

COPY = 41
CREATED = "2026-10-10T23:40:00.123456"


async def test_a_cancelled_start_records_the_job_bambuddy_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#5445: a pod stop between Bambuddy's accept and the record would leave a job no
    row records, which the next attempt cannot reuse, so it would slice again. The start
    runs to its end and only then raises the cancel."""
    reading = asyncio.Event()
    answer = asyncio.Event()
    recorded: list[Any] = []

    async def slice_job(job_id: int) -> Any:
        reading.set()
        await answer.wait()
        return SimpleNamespace(created_at=CREATED)

    async def record_preview(row: Any) -> None:
        recorded.append(row)

    async def slice_(library_file_id: int, request: Any) -> Any:
        return SimpleNamespace(job_id=9)

    async def nothing_to_reuse(library_file_id: int, key: str) -> None:
        return None

    monkeypatch.setattr(preview, "reusable_slice", lambda client, uploads: nothing_to_reuse)
    monkeypatch.setattr(
        preview, "slice_request", lambda plan, plate_id: SimpleNamespace(preset_key="k")
    )
    client = SimpleNamespace(slice=slice_, slice_job=slice_job)
    start = asyncio.create_task(
        preview._start_plate(
            cast(Any, client),
            cast(Any, SimpleNamespace(slicer_backend="bambuddy")),
            cast(Any, SimpleNamespace(record_preview=record_preview)),
            cast(Any, SimpleNamespace(library_file_id=COPY, nozzle_plan=None)),
            cast(Any, SimpleNamespace(plan=object(), plate_id=1)),
            subject="order_item:i-1",
            native=None,
            native_slices=None,
            temporal=None,
            native_reads=None,
        )
    )
    await reading.wait()
    start.cancel()
    await asyncio.sleep(0)
    assert not start.done()
    answer.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert [(row.job_id, row.subject) for row in recorded] == [(9, "order_item:i-1")]
