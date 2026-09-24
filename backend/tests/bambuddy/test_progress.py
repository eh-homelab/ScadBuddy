"""Issue #89 — normalising both print routes into one progress view.

The load-bearing case is the recorded ``pipeline-run.json``: a real run whose slice
failed, which reports ``status: "in_progress"`` and ``copies_in_progress: 1`` while
carrying ``completed_at`` and a ``Slice failed: …`` message. Every assertion about
``settled`` exists because a poll built on ``status`` would never stop on it.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import PipelineRun, QueueItem, SliceJob
from scadbuddy.bambuddy.progress import (
    NEVER_QUEUED_FIX,
    QUEUED_THEN_FAILED_FIX,
    SLICE_FIX,
    from_queue,
    from_run,
    progress_for,
    stage_of,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.render.glb import BoundingBox
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"
URL = "https://bambuddy.test/queue"


def run(**overrides: object) -> PipelineRun:
    body = dict(recording("pipeline-run.json"))
    body.update(overrides)
    return PipelineRun.model_validate(body)


def meta(**overrides: object) -> OutputMeta:
    body: dict[str, object] = {
        "id": "0" * 32,
        "slug": "demo",
        "job_id": "job",
        "created_at": "2026-09-24T00:00:00Z",
        "bbox_mm": BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    }
    body.update(overrides)
    return OutputMeta.model_validate(body)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("completed", "done"),
        ("COMPLETED", "done"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("pending", "queued"),
        ("in_progress", "running"),
        ("something Bambuddy added later", "unknown"),
        (None, "unknown"),
    ],
)
def test_statuses_map_onto_one_vocabulary(status: str | None, expected: str) -> None:
    """A status nobody has seen before must read as "still going", never as "done" —
    that is what would stop the poll on a print that is still live."""
    assert stage_of(status) == expected


def test_a_failed_run_still_reporting_in_progress_is_settled_and_failed() -> None:
    """The recorded run: ``status: in_progress``, ``copies_in_progress: 1``, and a
    slice failure. Trusting ``status`` here polls forever over an error."""
    recorded = run()
    assert recorded.status == "in_progress"
    assert recorded.copies_in_progress == 1

    progress = from_run(recorded, bambuddy_url=URL)
    assert progress.settled is True
    assert progress.stage == "failed"
    assert progress.error_message is not None
    assert progress.error_message.startswith("Slice failed:")


def test_a_run_whose_slice_never_produced_a_file_points_at_the_slicer() -> None:
    """``slice_job_id`` set and ``sliced_library_file_id`` still null is the structural
    fact; the wording of the message is Bambuddy's and is not parsed."""
    assert from_run(run(), bambuddy_url=URL).fix == SLICE_FIX


def test_a_copy_that_never_reached_the_queue_points_at_eligibility() -> None:
    progress = from_run(
        run(
            status="failed",
            completed_at="2026-09-24T04:13:51",
            slice_job_id=7,
            sliced_library_file_id=52,
            error_message=None,
            jobs=[
                {
                    "id": 1,
                    "pipeline_run_id": 1,
                    "copy_index": 0,
                    "status": "failed",
                    "queue_entry_id": None,
                    "error_message": "no printer matched",
                }
            ],
        ),
        bambuddy_url=URL,
    )
    assert progress.fix == NEVER_QUEUED_FIX
    assert progress.error_message == "no printer matched"


def test_a_copy_that_reached_the_queue_and_then_failed_points_at_the_mapping() -> None:
    progress = from_run(
        run(
            status="failed",
            completed_at="2026-09-24T04:13:51",
            sliced_library_file_id=52,
            error_message=None,
            jobs=[
                {
                    "id": 1,
                    "pipeline_run_id": 1,
                    "copy_index": 0,
                    "status": "failed",
                    "queue_entry_id": 51,
                    "error_message": "the AMS slot is empty",
                }
            ],
        ),
        bambuddy_url=URL,
    )
    assert progress.fix == QUEUED_THEN_FAILED_FIX
    assert progress.copies_detail[0].queue_entry_id == 51


def test_a_run_still_going_is_not_settled() -> None:
    progress = from_run(
        run(completed_at=None, error_message=None, copies=2, copies_completed=1),
        bambuddy_url=URL,
    )
    assert progress.settled is False
    assert progress.fix is None


def test_every_copy_accounted_for_settles_even_without_a_completion_time() -> None:
    progress = from_run(
        run(
            status="completed",
            completed_at=None,
            error_message=None,
            copies=2,
            copies_completed=2,
            copies_in_progress=0,
        ),
        bambuddy_url=URL,
    )
    assert progress.settled is True
    assert progress.stage == "done"


def test_the_queue_route_reports_the_slice_failure_rather_than_an_absent_item() -> None:
    """On this route the queue item only exists once the plate has sliced, so a failed
    slice is the whole state there is."""
    progress = from_queue(
        None,
        slice_job=SliceJob(status="failed", error="object outside the build plate"),
        slice_job_id=9,
        bambuddy_url=URL,
    )
    assert progress.stage == "failed"
    assert progress.settled is True
    assert progress.error_message == "object outside the build plate"
    assert progress.fix == SLICE_FIX


def test_a_queued_item_is_not_settled_and_waiting_is_not_failing() -> None:
    """``waiting_reason`` explains a queued item that is not printing. Showing it as an
    error would turn every normal queue wait into one."""
    item = QueueItem(
        id=51,
        status="pending",
        printer_name="3DP-31B-598",
        waiting_reason="No active H2C printers are idle",
    )
    progress = from_queue(item, slice_job_id=9, bambuddy_url=URL)
    assert progress.stage == "queued"
    assert progress.settled is False
    assert progress.error_message is None
    assert progress.copies_detail[0].waiting_reason == "No active H2C printers are idle"


def test_a_recorded_queue_item_reads_back_as_progress() -> None:
    item = QueueItem.model_validate(recording("queue-item.json"))
    progress = from_queue(item, bambuddy_url=URL)
    assert progress.route == "slice_queue"
    assert progress.queue_item_id == item.id


# --- reading it off a Bambuddy -----------------------------------------------


@respx.mock
async def test_an_output_that_has_never_printed_has_no_progress(
    bambuddy: BambuddyClient,
) -> None:
    """``None`` is an answer, not an error — the send bar shows nothing."""
    assert await progress_for(bambuddy, meta()) is None


@respx.mock
async def test_the_route_is_taken_from_the_record_not_guessed(
    bambuddy: BambuddyClient,
) -> None:
    """An output printed both ways carries a run id *and* a queue item id."""
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json={"id": 51, "status": "completed"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(200, json={"id": 9, "status": "completed"})
    )
    progress = await progress_for(
        bambuddy,
        meta(
            pipeline_run_id=1,
            queue_item_id=51,
            slice_job_id=9,
            print_route="slice_queue",
        ),
    )
    assert progress is not None
    assert progress.route == "slice_queue"
    assert progress.queue_item_id == 51


@respx.mock
async def test_a_record_written_before_this_issue_still_follows_its_run(
    bambuddy: BambuddyClient,
) -> None:
    """The older send bar's only outcome was a run id, so one on its own reads as the
    pipeline route rather than as nothing to show."""
    respx.get(f"{API}/pipeline-runs/1").mock(
        return_value=httpx.Response(200, json=recording("pipeline-run.json"))
    )
    progress = await progress_for(bambuddy, meta(pipeline_run_id=1))
    assert progress is not None
    assert progress.route == "pipeline"
    assert progress.pipeline_run_id == 1


@respx.mock
async def test_a_queue_entry_bambuddy_has_dropped_reads_as_finished(
    bambuddy: BambuddyClient,
) -> None:
    """Bambuddy removes a dispatched entry from the queue. Reporting a 404 there would
    contradict the print the user can watch running."""
    respx.get(f"{API}/queue/51").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    progress = await progress_for(bambuddy, meta(queue_item_id=51, print_route="slice_queue"))
    assert progress is not None
    assert progress.stage == "done"
    assert progress.settled is True


@respx.mock
async def test_a_bambuddy_that_refuses_the_read_is_not_swallowed(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/pipeline-runs/1").mock(
        return_value=httpx.Response(500, json={"detail": "the database is locked"})
    )
    with pytest.raises(ApiError) as raised:
        await progress_for(bambuddy, meta(pipeline_run_id=1, print_route="pipeline"))
    assert "the database is locked" in raised.value.detail
