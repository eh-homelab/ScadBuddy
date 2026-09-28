"""Issue #89 — normalising the slice-and-queue route into one progress view."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from scadbuddy.bambuddy import progress as progress_module
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import QueueItem, SliceJob
from scadbuddy.bambuddy.progress import (
    QUEUED_THEN_FAILED_FIX,
    SLICE_FIX,
    PrintProgress,
    from_queue,
    progress_for,
    stage_of,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.render.glb import BoundingBox
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"
URL = "https://bambuddy.test/queue"


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
    """A recorded route is followed as it stands."""
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json={"id": 51, "status": "completed"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(200, json={"id": 9, "status": "completed"})
    )
    progress = await progress_for(
        bambuddy,
        meta(
            queue_item_id=51,
            slice_job_id=9,
            print_route="slice_queue",
        ),
    )
    assert progress is not None
    assert progress.route == "slice_queue"
    assert progress.queue_item_id == 51


@respx.mock
async def test_a_record_whose_last_print_was_a_pipeline_run_has_no_progress(
    bambuddy: BambuddyClient,
) -> None:
    """#312: the pipeline route is gone. The record reads as never printed, and the queue
    item an *earlier* print left beside it is not mistaken for the print now running."""
    record = meta(print_route="pipeline", pipeline_run_id=1, queue_item_id=51, slice_job_id=9)
    assert record.print_route is None
    assert record.queue_item_id is None
    assert await progress_for(bambuddy, record) is None


@respx.mock
async def test_a_record_from_before_routes_with_a_run_id_has_no_progress(
    bambuddy: BambuddyClient,
) -> None:
    """Before #89 a run id meant the pipeline route even beside a queue item id."""
    record = meta(pipeline_run_id=1, queue_item_id=51)
    assert record.queue_item_id is None
    assert await progress_for(bambuddy, record) is None


@respx.mock
async def test_a_record_from_before_routes_with_only_a_queue_item_follows_it(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json={"id": 51, "status": "completed"})
    )
    progress = await progress_for(bambuddy, meta(queue_item_id=51))
    assert progress is not None
    assert progress.route == "slice_queue"
    assert progress.queue_item_id == 51


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
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(500, json={"detail": "the database is locked"})
    )
    with pytest.raises(ApiError) as raised:
        await progress_for(
            bambuddy, meta(slice_job_id=9, queue_item_id=51, print_route="slice_queue")
        )
    assert "the database is locked" in raised.value.detail


# --- every plate of an all-plates print (#200) ------------------------------


def plates_meta() -> OutputMeta:
    return meta(
        print_route="slice_queue",
        queue_item_id=52,
        slice_job_id=10,
        plates=[
            {"plate_id": 1, "queue_item_id": 51, "slice_job_id": 9},
            {"plate_id": 2, "queue_item_id": 52, "slice_job_id": 10},
        ],
    )


def sliced() -> None:
    for job in (9, 10):
        respx.get(f"{API}/slice-jobs/{job}").mock(
            return_value=httpx.Response(200, json={"id": job, "status": "completed"})
        )


@respx.mock
async def test_every_plate_is_polled_not_only_the_last(bambuddy: BambuddyClient) -> None:
    """The single ids are the last plate's. An earlier plate still printing must keep
    the poll going, and each plate's entry is shown."""
    sliced()
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json={"id": 51, "status": "printing"})
    )
    respx.get(f"{API}/queue/52").mock(
        return_value=httpx.Response(200, json={"id": 52, "status": "completed"})
    )
    progress = await progress_for(bambuddy, plates_meta())
    assert progress is not None
    assert progress.settled is False
    assert progress.stage == "running"
    assert progress.copies == 2
    assert progress.copies_completed == 1
    assert progress.copies_in_progress == 1
    assert [(c.plate_id, c.queue_entry_id, c.stage) for c in progress.copies_detail] == [
        (1, 51, "running"),
        (2, 52, "done"),
    ]


@respx.mock
async def test_a_plate_running_outranks_an_earlier_plate_still_queued(
    bambuddy: BambuddyClient,
) -> None:
    """Plates go to different printers and start out of order. The print reads as its
    most advanced plate, not as whichever unsettled plate comes first."""
    sliced()
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json={"id": 51, "status": "pending"})
    )
    respx.get(f"{API}/queue/52").mock(
        return_value=httpx.Response(200, json={"id": 52, "status": "printing"})
    )
    progress = await progress_for(bambuddy, plates_meta())
    assert progress is not None
    assert progress.settled is False
    assert progress.stage == "running"


@respx.mock
async def test_plates_are_polled_together_and_reported_in_plate_order(
    bambuddy: BambuddyClient,
) -> None:
    """Plate 1's read only answers once plate 2's has started, so a one-after-another
    poll would never finish; the result is still in plate order."""
    sliced()
    second_started = asyncio.Event()

    async def first(request: httpx.Request) -> httpx.Response:
        await second_started.wait()
        return httpx.Response(200, json={"id": 51, "status": "printing"})

    def second(request: httpx.Request) -> httpx.Response:
        second_started.set()
        return httpx.Response(200, json={"id": 52, "status": "completed"})

    respx.get(f"{API}/queue/51").mock(side_effect=first)
    respx.get(f"{API}/queue/52").mock(side_effect=second)
    progress = await asyncio.wait_for(progress_for(bambuddy, plates_meta()), timeout=5)
    assert progress is not None
    assert [c.plate_id for c in progress.copies_detail] == [1, 2]


@respx.mock
async def test_a_failing_plate_read_cancels_the_others_and_raises_its_own_error(
    bambuddy: BambuddyClient,
) -> None:
    """Plate 1's read fails once plate 2's is in flight; plate 2's is then cancelled
    rather than left running, and the caller sees the ApiError, not a group."""
    sliced()
    second_started = asyncio.Event()
    second_cancelled = False

    async def first(request: httpx.Request) -> httpx.Response:
        await second_started.wait()
        return httpx.Response(500, json={"detail": "the database is locked"})

    async def second(request: httpx.Request) -> httpx.Response:
        nonlocal second_cancelled
        second_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            second_cancelled = True
            raise
        raise AssertionError("unreachable")

    respx.get(f"{API}/queue/51").mock(side_effect=first)
    respx.get(f"{API}/queue/52").mock(side_effect=second)
    with pytest.raises(ApiError) as raised:
        await asyncio.wait_for(progress_for(bambuddy, plates_meta()), timeout=5)
    assert "the database is locked" in raised.value.detail
    assert second_cancelled


async def test_when_several_plates_fail_the_earliest_plates_error_is_raised(
    bambuddy: BambuddyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plate 2 fails first and plate 1 in the same loop pass, so both are in the group
    with plate 2's first; the error raised is still plate 1's (#244)."""
    released = asyncio.Event()

    async def read(
        client: BambuddyClient, slice_job_id: int | None, queue_item_id: int | None, url: str
    ) -> PrintProgress:
        if queue_item_id == 52:
            released.set()
            raise ApiError(502, "plate 2 broke")
        await released.wait()
        raise ApiError(502, "plate 1 broke")

    monkeypatch.setattr(progress_module, "_queued_progress", read)
    with pytest.raises(ApiError) as raised:
        await progress_for(bambuddy, plates_meta())
    assert raised.value.detail == "plate 1 broke"


@respx.mock
async def test_a_cancelled_plate_beside_a_finished_one_is_a_cancelled_print(
    bambuddy: BambuddyClient,
) -> None:
    """Reporting "done" would say every plate printed when one never did."""
    sliced()
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json={"id": 51, "status": "cancelled"})
    )
    respx.get(f"{API}/queue/52").mock(
        return_value=httpx.Response(200, json={"id": 52, "status": "completed"})
    )
    progress = await progress_for(bambuddy, plates_meta())
    assert progress is not None
    assert progress.settled is True
    assert progress.stage == "cancelled"
    assert progress.copies_cancelled == 1
    assert progress.copies_completed == 1


@respx.mock
async def test_an_earlier_plate_failing_is_the_prints_failure(bambuddy: BambuddyClient) -> None:
    """A failure on the first plate is reported even though the last is fine; a dropped
    entry reads as done, as it does for a single plate."""
    sliced()
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(
            200, json={"id": 51, "status": "failed", "error_message": "AMS slot empty"}
        )
    )
    respx.get(f"{API}/queue/52").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    progress = await progress_for(bambuddy, plates_meta())
    assert progress is not None
    assert progress.settled is True
    assert progress.stage == "failed"
    assert progress.error_message == "AMS slot empty"
    assert progress.fix == QUEUED_THEN_FAILED_FIX
    assert progress.copies_failed == 1
    assert progress.copies_completed == 1
    assert [(c.plate_id, c.queue_entry_id, c.stage) for c in progress.copies_detail] == [
        (1, 51, "failed"),
        (2, 52, "done"),
    ]


@respx.mock
async def test_a_failed_plate_beside_an_unsettled_one_keeps_polling(
    bambuddy: BambuddyClient,
) -> None:
    """The failure is reported at once, but the print is not settled while a sibling
    plate is still printing: that plate can still finish (#260)."""
    sliced()
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(
            200, json={"id": 51, "status": "failed", "error_message": "AMS slot empty"}
        )
    )
    respx.get(f"{API}/queue/52").mock(
        return_value=httpx.Response(200, json={"id": 52, "status": "printing"})
    )
    progress = await progress_for(bambuddy, plates_meta())
    assert progress is not None
    assert progress.stage == "failed"
    assert progress.settled is False
    assert progress.error_message == "AMS slot empty"
    assert progress.copies_failed == 1
    assert progress.copies_in_progress == 1
