"""#2715: what a print run's lost slice start left in Bambuddy's library is removed.

A start whose POST got no answer is started again (#2081); Bambuddy may have taken
the first one, whose job then slices to a file nothing queues. Every start is
recorded, and the housekeeping sweep reads the jobs numbered beside the first one
recorded after a lost start (`bambuddy/lost_slices.py`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import SliceLostError, SliceStarted
from scadbuddy.bambuddy.lost_slices import GIVE_UP_AFTER, STARTS_KEPT, sweep_lost_slices
from scadbuddy.bambuddy.models import LibraryFile, SliceJob, SliceResult
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LostStart, PreviewSlice
from scadbuddy.core.problems import ApiError
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows import print_activities
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.print_models import SLICE_LOST, SliceStartInput
from tests.bambuddy.test_dispatch import PLAN

COPY = 41
T0 = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)


@dataclass
class Starts:
    """The store's lost starts and recorded jobs, in memory."""

    lost: list[LostStart] = field(default_factory=list)
    #: Job id -> when it was recorded (a run's start or a background slice).
    jobs: dict[int, datetime] = field(default_factory=dict)
    finished: list[int] = field(default_factory=list)
    pruned: list[datetime] = field(default_factory=list)

    async def prune_slice_starts(self, before: datetime) -> int:
        self.pruned.append(before)
        return 0

    async def lost_starts(self) -> list[LostStart]:
        return [row for row in self.lost if row.id not in self.finished]

    async def finish_lost_start(self, row_id: int) -> None:
        self.finished.append(row_id)

    async def slice_jobs_after(self, after: datetime, within: timedelta) -> list[int]:
        return sorted(job for job, at in self.jobs.items() if after < at <= after + within)

    async def slice_jobs_recorded(self, job_ids: Iterable[int], since: datetime) -> set[int]:
        return {job for job in job_ids if job in self.jobs and self.jobs[job] >= since}


@dataclass
class Jobs:
    """Bambuddy's slice jobs and library, as far as the sweep reads and deletes them."""

    jobs: dict[int, SliceJob] = field(default_factory=dict)
    files: dict[int, str] = field(default_factory=dict)
    deleted: list[int] = field(default_factory=list)
    read: list[int] = field(default_factory=list)

    async def slice_job(self, job_id: int) -> SliceJob:
        self.read.append(job_id)
        if job_id not in self.jobs:
            raise ApiError(404, "Slice job not found or expired")
        return self.jobs[job_id]

    async def library_file(self, file_id: int) -> LibraryFile:
        if file_id not in self.files or file_id in self.deleted:
            raise ApiError(404, "gone")
        return LibraryFile(id=file_id, filename=self.files[file_id])

    async def delete_library_file(self, file_id: int) -> bool:
        self.deleted.append(file_id)
        return True


def job(
    job_id: int,
    *,
    source: int = COPY,
    at: datetime = T0 + timedelta(seconds=5),
    status: str = "completed",
    sliced: int | None = None,
) -> SliceJob:
    result = (
        SliceResult(library_file_id=sliced, name=f"sliced-{sliced}.gcode.3mf")
        if sliced is not None and status == "completed"
        else None
    )
    return SliceJob(
        id=job_id,
        status=status,
        kind="library_file",
        source_id=source,
        created_at=at.replace(tzinfo=None).isoformat(),
        result=result,
    )


def world(*jobs: SliceJob) -> Jobs:
    bambuddy = Jobs(jobs={j.id or 0: j for j in jobs})
    for j in jobs:
        if j.result is not None and j.result.library_file_id is not None:
            bambuddy.files[j.result.library_file_id] = j.result.name or ""
    return bambuddy


def lost_at(when: datetime = T0) -> Starts:
    return Starts(lost=[LostStart(id=1, library_file_id=COPY, created_at=when)])


async def sweep(bambuddy: Jobs, store: Starts, now: datetime = T0 + timedelta(minutes=5)) -> int:
    return await sweep_lost_slices(
        cast(BambuddyClient, bambuddy), cast(BambuddyUploadStore, store), now=now
    )


async def test_the_job_a_lost_start_left_beside_the_runs_own_is_removed() -> None:
    """The run's retry is job 11; job 10, of the same copy and created at the lost
    start, nobody recorded: it is the lost start's, and its file goes. Job 9 slices
    another file, and job 11 is the run's: both stay."""
    store = lost_at()
    store.jobs[11] = T0 + timedelta(minutes=1)
    bambuddy = world(
        job(9, source=99, sliced=400),
        job(10, sliced=501),
        job(11, at=T0 + timedelta(minutes=1), sliced=502),
    )

    assert await sweep(bambuddy, store) == 1

    assert bambuddy.deleted == [501]
    assert 11 not in bambuddy.read
    assert store.finished == [1]


async def test_a_job_a_run_or_background_slice_recorded_is_never_removed() -> None:
    store = lost_at()
    store.jobs[11] = T0 + timedelta(minutes=1)
    # Another run's start, or the print dialog's, of the same copy at the same time.
    store.jobs[10] = T0 + timedelta(seconds=30)
    bambuddy = world(job(10, sliced=501), job(11, at=T0 + timedelta(minutes=1), sliced=502))

    assert await sweep(bambuddy, store) == 0
    assert bambuddy.deleted == []


async def test_a_job_from_before_the_lost_start_or_long_after_it_stays() -> None:
    store = lost_at()
    store.jobs[11] = T0 + timedelta(minutes=1)
    bambuddy = world(
        job(9, at=T0 - timedelta(minutes=10), sliced=400),
        job(12, at=T0 + timedelta(hours=1), sliced=503),
    )

    assert await sweep(bambuddy, store) == 0
    assert bambuddy.deleted == []


async def test_a_job_still_slicing_is_looked_at_again_next_sweep() -> None:
    store = lost_at()
    store.jobs[11] = T0 + timedelta(minutes=1)
    bambuddy = world(job(10, status="running"))

    assert await sweep(bambuddy, store) == 0
    assert store.finished == []

    bambuddy.jobs[10] = job(10, sliced=501)
    bambuddy.files[501] = "sliced-501.gcode.3mf"
    assert await sweep(bambuddy, store, now=T0 + timedelta(minutes=10)) == 1
    assert bambuddy.deleted == [501]
    assert store.finished == [1]


async def test_a_file_that_no_longer_carries_its_jobs_name_stays() -> None:
    """The id may be another file's by now (as the background-slice sweep checks)."""
    store = lost_at()
    store.jobs[11] = T0 + timedelta(minutes=1)
    bambuddy = world(job(10, sliced=501))
    bambuddy.files[501] = "someone else's.3mf"

    assert await sweep(bambuddy, store) == 0
    assert bambuddy.deleted == []


async def test_with_nothing_started_since_it_waits_then_gives_up() -> None:
    store = lost_at()
    bambuddy = world()

    await sweep(bambuddy, store)
    assert store.finished == []
    await sweep(bambuddy, store, now=T0 + GIVE_UP_AFTER + timedelta(minutes=1))
    assert store.finished == [1]


async def test_bambuddy_unreachable_keeps_the_lost_start_for_the_next_sweep() -> None:
    class Down(Jobs):
        async def slice_job(self, job_id: int) -> SliceJob:
            raise ApiError(503, "Bambuddy is down")

    store = lost_at()
    store.jobs[11] = T0 + timedelta(minutes=1)

    assert await sweep(Down(), store) == 0
    assert store.finished == []


async def test_a_failed_delete_keeps_the_files_already_removed_counted() -> None:
    """#2911: jobs 9 and 10 are both the lost start's; 10's file cannot be deleted. 9's
    removal still counts, and the lost start is looked at again next sweep."""

    class Refuses(Jobs):
        async def delete_library_file(self, file_id: int) -> bool:
            if file_id == 502:
                raise ApiError(503, "Bambuddy is busy")
            return await super().delete_library_file(file_id)

    store = lost_at()
    store.jobs[11] = T0 + timedelta(minutes=1)
    bambuddy = Refuses()
    for j in (job(9, sliced=501), job(10, sliced=502)):
        bambuddy.jobs[j.id or 0] = j
        assert j.result is not None and j.result.library_file_id is not None
        bambuddy.files[j.result.library_file_id] = j.result.name or ""

    assert await sweep(bambuddy, store) == 1
    assert bambuddy.deleted == [501]
    assert store.finished == []


async def test_each_sweep_forgets_slice_starts_past_their_use() -> None:
    """#2911: answered and swept starts would otherwise only grow."""
    store = Starts()
    now = T0 + timedelta(days=3)

    await sweep(world(), store, now=now)

    assert store.pruned == [now - STARTS_KEPT]
    assert GIVE_UP_AFTER + timedelta(minutes=30) < STARTS_KEPT


# The activity records every start, and a lost one.


@dataclass
class Recorder:
    starts: list[tuple[int, int | None]] = field(default_factory=list)
    fail: bool = False

    async def record_slice_start(self, library_file_id: int, job_id: int | None) -> None:
        if self.fail:
            raise RuntimeError("the database is down")
        self.starts.append((library_file_id, job_id))

    async def previews_for(self, library_file_id: int, preset_key: str) -> list[PreviewSlice]:
        return []


def activities(monkeypatch: pytest.MonkeyPatch, recorder: Recorder, start: Any) -> PrintActivities:
    @asynccontextmanager
    async def client_for(settings: object) -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(print_activities, "client_for", client_for)
    monkeypatch.setattr(print_activities, "start_slice", start)

    class Settings:
        def load(self) -> object:
            return object()

    deps = PrintDeps(
        settings_store=cast(Any, Settings()),
        outputs=cast(Any, None),
        prints=None,
        uploads=cast(BambuddyUploadStore, recorder),
        store=cast(Any, None),
        observer=cast(Any, None),
    )
    return PrintActivities(deps)


START = SliceStartInput(library_file_id=COPY, plan=PLAN, plate_id=1)


async def test_every_slice_a_run_starts_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def answered(*_: object, **__: object) -> SliceStarted:
        return SliceStarted(job_id=11, preset_key="k")

    recorder = Recorder()
    started = await activities(monkeypatch, recorder, answered).slice_start(START)

    assert started.job_id == 11
    assert recorder.starts == [(COPY, 11)]


async def test_a_start_that_got_no_answer_is_recorded_as_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lost(*_: object, **__: object) -> SliceStarted:
        raise SliceLostError(503, "Bambuddy could not be reached")

    recorder = Recorder()
    with pytest.raises(ApplicationError) as raised:
        await activities(monkeypatch, recorder, lost).slice_start(START)

    assert raised.value.type == SLICE_LOST
    assert recorder.starts == [(COPY, None)]


async def test_a_start_it_cannot_record_is_not_handed_to_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrecorded, the run's own job would look like a lost start's to the sweep, which
    would remove the file the run queues."""

    async def answered(*_: object, **__: object) -> SliceStarted:
        return SliceStarted(job_id=11, preset_key="k")

    with pytest.raises(RuntimeError):
        await activities(monkeypatch, Recorder(fail=True), answered).slice_start(START)


async def test_a_lost_start_it_cannot_record_still_reports_the_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lost(*_: object, **__: object) -> SliceStarted:
        raise SliceLostError(503, "Bambuddy could not be reached")

    with pytest.raises(ApplicationError) as raised:
        await activities(monkeypatch, Recorder(fail=True), lost).slice_start(START)
    assert raised.value.type == SLICE_LOST


# The store, on Postgres.


@pytest.fixture
def uploads(pg_conninfo: str) -> Iterator[BambuddyUploadStore]:
    jobs = JobProjection(pg_conninfo, pool_size=2)
    jobs.open()
    try:
        yield BambuddyUploadStore(jobs.pool)
    finally:
        jobs.close()


@pytest.mark.requires_postgres
async def test_the_store_keeps_lost_starts_and_the_jobs_beside_them(
    uploads: BambuddyUploadStore,
) -> None:
    await uploads.record_slice_start(COPY, None)
    [lost] = await uploads.lost_starts()
    assert lost.library_file_id == COPY
    await uploads.record_slice_start(COPY, 11)
    await uploads.record_preview(
        PreviewSlice(job_id=12, subject="o", library_file_id=COPY, preset_key="k", plate_id=1)
    )

    assert await uploads.slice_jobs_after(lost.created_at, timedelta(minutes=30)) == [11, 12]
    assert (
        await uploads.slice_jobs_after(lost.created_at + timedelta(hours=1), timedelta(minutes=30))
        == []
    )
    since = lost.created_at - timedelta(minutes=30)
    assert await uploads.slice_jobs_recorded([10, 11, 12], since) == {11, 12}
    assert await uploads.slice_jobs_recorded([11], lost.created_at + timedelta(hours=1)) == set()

    await uploads.finish_lost_start(lost.id)
    assert await uploads.lost_starts() == []


@pytest.mark.requires_postgres
async def test_the_store_prunes_only_slice_starts_older_than_asked(
    uploads: BambuddyUploadStore,
) -> None:
    """#2911: old starts, answered or lost, go; recent ones stay for the sweep."""
    await uploads.record_slice_start(COPY, 21)
    await uploads.record_slice_start(COPY, None)
    with uploads._pool.connection() as conn:
        conn.execute(
            "UPDATE print_slice_starts SET created_at = created_at - interval '2 days'"
            " WHERE job_id = 21 OR job_id IS NULL"
        )
    await uploads.record_slice_start(COPY, 22)
    [old_lost] = await uploads.lost_starts()

    pruned = await uploads.prune_slice_starts(old_lost.created_at + timedelta(days=1))

    assert pruned == 2
    assert await uploads.lost_starts() == []
    assert await uploads.slice_jobs_recorded([21, 22], old_lost.created_at) == {22}
