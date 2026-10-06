"""Issue #89 — normalising the slice-and-queue route into one progress view."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
import respx

from scadbuddy.bambuddy import progress as progress_module
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import QueueItem, SliceJob
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from scadbuddy.bambuddy.progress import (
    QUEUED_THEN_FAILED_FIX,
    SLICE_FIX,
    PrintProgress,
    from_queue,
    progress_for,
)
from scadbuddy.bambuddy.stages import stage_of
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy, SlicedCopy
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


def test_a_failed_slice_job_reports_bambuddys_own_error_detail() -> None:
    """Bambuddy's ``GET /slice-jobs/{id}`` reports a failure as ``error_status`` and
    ``error_detail`` (``backend/app/api/routes/slice_jobs.py``), not ``error``. This is
    that route's whole failed body."""
    job = SliceJob.model_validate(
        {
            "job_id": 9,
            "status": "failed",
            "kind": "library_file",
            "source_id": 12,
            "source_name": "dice.3mf",
            "created_at": "2026-10-06T10:00:00+00:00",
            "started_at": "2026-10-06T10:00:01+00:00",
            "completed_at": "2026-10-06T10:00:09+00:00",
            "progress": None,
            "error_status": 422,
            "error_detail": "object outside the build plate",
        }
    )
    assert job.failure == "object outside the build plate"
    progress = from_queue(None, slice_job=job, slice_job_id=9, bambuddy_url=URL)
    assert progress.error_message == "object outside the build plate"


def test_a_structured_slice_error_detail_still_reads_as_a_message() -> None:
    """``error_detail`` is FastAPI's ``HTTPException.detail``, which may be a dict. It
    must not fail validation of the whole poll."""
    job = SliceJob.model_validate(
        {"job_id": 9, "status": "failed", "error_status": 400, "error_detail": {"msg": "bad plate"}}
    )
    assert job.failure is not None
    assert "bad plate" in job.failure


def test_a_failed_slice_with_only_a_status_code_says_so() -> None:
    job = SliceJob.model_validate({"job_id": 9, "status": "failed", "error_status": 500})
    assert job.failure is not None
    assert "500" in job.failure


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
    slice_job = respx.get(f"{API}/slice-jobs/9").mock(
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
    # With a queue item, its slice job has finished and is not read again (#898).
    assert not slice_job.called


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
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(500, json={"detail": "the database is locked"})
    )
    with pytest.raises(ApiError) as raised:
        await progress_for(
            bambuddy, meta(slice_job_id=9, queue_item_id=51, print_route="slice_queue")
        )
    assert "the database is locked" in raised.value.detail


@respx.mock
async def test_a_bambuddy_that_refuses_the_slice_job_read_is_not_swallowed(
    bambuddy: BambuddyClient,
) -> None:
    """Only a 404 settles a slice job (#898); any other refusal is still an error."""
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(500, json={"detail": "the database is locked"})
    )
    with pytest.raises(ApiError) as raised:
        await progress_for(bambuddy, meta(slice_job_id=9, print_route="slice_queue"))
    assert raised.value.status == 502
    assert "the database is locked" in raised.value.detail


@respx.mock
async def test_an_expired_slice_job_does_not_hide_the_queue_item_it_became(
    bambuddy: BambuddyClient,
) -> None:
    """#898: a queue item is only recorded once its slice job finished, so the slice job
    says nothing more. Bambuddy forgets slice jobs (they expire, and its ids restart),
    and asking for one it has dropped must not fail a read the queue item can answer."""
    expired = respx.get(f"{API}/slice-jobs/21").mock(
        return_value=httpx.Response(404, json={"detail": "Slice job not found or expired"})
    )
    respx.get(f"{API}/queue/114").mock(
        return_value=httpx.Response(200, json={"id": 114, "status": "pending"})
    )
    progress = await progress_for(
        bambuddy, meta(slice_job_id=21, queue_item_id=114, print_route="slice_queue")
    )
    assert progress is not None
    assert progress.stage == "queued"
    assert progress.queue_item_id == 114
    assert not expired.called


@respx.mock
async def test_a_reused_slice_job_id_cannot_lend_its_state_to_an_older_print(
    bambuddy: BambuddyClient,
) -> None:
    """#898: Bambuddy's slice-job ids restart, so the id an old output kept can name
    another output's job. Its failure must not be read as this print's."""
    reused = respx.get(f"{API}/slice-jobs/3").mock(
        return_value=httpx.Response(200, json={"id": 3, "status": "failed", "error": "not ours"})
    )
    respx.get(f"{API}/queue/106").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    progress = await progress_for(
        bambuddy, meta(slice_job_id=3, queue_item_id=106, print_route="slice_queue")
    )
    assert progress is not None
    assert progress.stage == "done"
    assert progress.error_message is None
    assert not reused.called


@respx.mock
async def test_a_slice_job_bambuddy_no_longer_has_settles_the_print(
    bambuddy: BambuddyClient,
) -> None:
    """#898: with no queue item to fall back to, a slice job Bambuddy has forgotten
    will never report again. Polling on would show "waiting" forever."""
    respx.get(f"{API}/slice-jobs/21").mock(
        return_value=httpx.Response(404, json={"detail": "Slice job not found or expired"})
    )
    progress = await progress_for(bambuddy, meta(slice_job_id=21, print_route="slice_queue"))
    assert progress is not None
    assert progress.settled is True
    assert progress.stage == "unknown"
    assert progress.slice_job_id == 21
    assert progress.error_message is not None
    assert "slice job 21" in progress.error_message


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
        client: BambuddyClient,
        slice_job_id: int | None,
        queue_item_id: int | None,
        url: str,
        **_: object,
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


# --- linking archives is best effort (#306, #522 review) --------------------


class FakeLinks(PrintLinkStore):
    """Links in memory; ``fail`` makes every write raise what a lost database would."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(None)
        self.fail = fail
        self.recorded: list[PrintLink] = []

    async def record(self, output_id: str, link: PrintLink) -> None:
        if self.fail:
            raise psycopg.OperationalError("the database went away")
        self.recorded.append(link)

    async def for_output(self, output_id: str) -> list[PrintLink]:
        return list(self.recorded)

    async def linked_queue_items(self, output_id: str) -> set[int]:
        return {link.queue_item_id for link in self.recorded if link.queue_item_id is not None}


class FakeUploads(BambuddyUploadStore):
    """One library copy whose slice's hash is already known."""

    def __init__(self) -> None:
        super().__init__(None)

    async def for_output(self, output_id: str) -> list[LibraryCopy]:
        return [
            LibraryCopy(
                id=11, folder_id=2, target_key="H2C", sliced=[SlicedCopy(id=80, file_hash="ab")]
            )
        ]

    async def sent_between(self, output_id: str) -> tuple[datetime, datetime] | None:
        sent = datetime(2026, 9, 27, tzinfo=UTC)
        return sent, sent


@pytest.fixture
def no_recent_scans() -> Iterator[None]:
    progress_module._last_hash_scan.clear()
    yield
    progress_module._last_hash_scan.clear()


@respx.mock
async def test_a_link_that_cannot_be_recorded_does_not_fail_any_plates_read(
    bambuddy: BambuddyClient,
) -> None:
    sliced()
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json={"id": 51, "status": "printing", "archive_id": 7})
    )
    respx.get(f"{API}/queue/52").mock(
        return_value=httpx.Response(200, json={"id": 52, "status": "completed", "archive_id": 8})
    )
    progress = await progress_for(
        bambuddy, plates_meta(), uploads=FakeUploads(), links=FakeLinks(fail=True)
    )
    assert progress is not None
    assert [(c.plate_id, c.stage) for c in progress.copies_detail] == [
        (1, "running"),
        (2, "done"),
    ]


@respx.mock
@pytest.mark.usefixtures("no_recent_scans")
async def test_a_failing_archive_scan_still_reads_the_gone_item_as_finished(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/queue/51").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    scan = respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(503, json={"detail": "busy"})
    )
    progress = await progress_for(
        bambuddy,
        meta(queue_item_id=51, print_route="slice_queue"),
        uploads=FakeUploads(),
        links=FakeLinks(),
    )
    assert scan.called
    assert progress is not None
    assert (progress.stage, progress.settled) == ("done", True)


@respx.mock
@pytest.mark.usefixtures("no_recent_scans")
async def test_a_gone_slice_job_still_looks_for_the_print_by_hash(
    bambuddy: BambuddyClient,
) -> None:
    """#898 review: a slice job Bambuddy dropped may still have printed, so its archive
    is looked for by hash the way a gone queue item's is."""
    respx.get(f"{API}/slice-jobs/21").mock(
        return_value=httpx.Response(404, json={"detail": "Slice job not found or expired"})
    )
    scan = respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(503, json={"detail": "busy"})
    )
    progress = await progress_for(
        bambuddy,
        meta(slice_job_id=21, print_route="slice_queue"),
        uploads=FakeUploads(),
        links=FakeLinks(),
    )
    assert scan.called
    assert progress is not None
    assert (progress.stage, progress.settled) == ("unknown", True)


@respx.mock
@pytest.mark.usefixtures("no_recent_scans")
async def test_without_a_database_a_gone_item_still_reads_as_finished(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/queue/51").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    progress = await progress_for(
        bambuddy,
        meta(queue_item_id=51, print_route="slice_queue"),
        uploads=BambuddyUploadStore(None),
        links=PrintLinkStore(None),
    )
    assert progress is not None
    assert progress.stage == "done"


@pytest.mark.usefixtures("no_recent_scans")
def test_scans_older_than_the_interval_are_forgotten() -> None:
    interval = progress_module.HASH_SCAN_INTERVAL
    assert progress_module._claim_hash_scan("a", 0.0)
    assert not progress_module._claim_hash_scan("a", interval / 2)
    assert progress_module._claim_hash_scan("b", interval + 1)
    assert set(progress_module._last_hash_scan) == {"b"}


def test_a_stale_rack_pick_shows_bambuddys_own_words() -> None:
    """Spec §5, §10: a pick that no longer fits fails the item at dispatch; the print's
    progress shows Bambuddy's message (the run result was fixed at queue time)."""
    item = QueueItem(
        id=51, status="failed", error_message="Nozzle rack pick no longer fits the printer"
    )
    shown = from_queue(item, bambuddy_url="http://bambuddy.test/queue")
    assert (shown.stage, shown.settled) == ("failed", True)
    assert shown.error_message == "Nozzle rack pick no longer fits the printer"
