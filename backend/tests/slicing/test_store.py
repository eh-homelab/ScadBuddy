"""`native_slices`: one row per `SliceWorkflow` execution (plan
2026-10-16-native-slicer-rust-worker Task 11)."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

import pytest

from scadbuddy.slicing.store import (
    HOLDER,
    NativeSliceStore,
    SliceSubjectError,
    native_workflow_id,
)
from scadbuddy.store.index import Pool
from scadbuddy.store.refs import BlobRefs
from tests.support.store import pool  # noqa: F401

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def store(pool: Pool) -> NativeSliceStore:  # noqa: F811
    return NativeSliceStore(pool)


async def _start(store: NativeSliceStore, workflow_id: str = "slice-1-a", **extra: object) -> int:
    fields: dict[str, object] = {
        "workflow_id": workflow_id,
        "subject": "out-1",
        "library_file_id": 1,
        "plate_id": 1,
        "preset_key": "k",
    }
    fields.update(extra)
    row = await store.start(**fields)  # type: ignore[arg-type]
    return row.id


async def test_start_twice_with_one_workflow_id_is_one_row(store: NativeSliceStore) -> None:
    first = await store.start(
        workflow_id="slice-1-a", subject="out-1", library_file_id=1, plate_id=1, preset_key="k"
    )
    again = await store.start(
        workflow_id="slice-1-a", subject="out-1", library_file_id=1, plate_id=1, preset_key="k"
    )
    assert again.id == first.id
    assert first.status == "running" and first.warnings == [] and first.finished_at is None


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_start_on_an_unfinished_row_reads_running_at_once(
    store: NativeSliceStore, status: Literal["failed", "cancelled"]
) -> None:
    """Task 12 review I1: the dialog reads the row the moment the start answers, before
    the new execution's first activity; it must not read the old failure and stop. A
    start that no execution follows is the lost-row stop's (`started_at` refreshed)."""
    slice_id = await _start(store)
    await store.finish(
        slice_id, status=status, sliced_blob_key=None, failure="The slicer failed: x", warnings=[]
    )
    before = await store.get(slice_id, "out-1")
    assert before is not None
    again = await store.start(
        workflow_id="slice-1-a", subject="out-1", library_file_id=1, plate_id=1, preset_key="k"
    )
    assert (again.id, again.status, again.failure, again.finished_at) == (
        slice_id,
        "running",
        None,
        None,
    )
    assert again.started_at > before.started_at
    found = await store.get(slice_id, "out-1")
    assert found is not None and found.status == "running"


async def test_a_start_on_a_running_row_leaves_its_start(store: NativeSliceStore) -> None:
    """A start joining a running execution keeps the start the lost-row stop compares."""
    slice_id = await _start(store)
    started = await store.running(slice_id)
    again = await _start(store)
    found = await store.get(again, "out-1")
    assert found is not None and found.started_at == started


async def test_start_under_another_subject_is_refused(store: NativeSliceStore) -> None:
    await _start(store)
    with pytest.raises(SliceSubjectError, match="out-1"):
        await store.start(
            workflow_id="slice-1-a", subject="out-2", library_file_id=1, plate_id=1, preset_key="k"
        )


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_an_unfinished_slice_drops_its_holds(
    store: NativeSliceStore,
    pool: Pool,  # noqa: F811
    status: Literal["failed", "cancelled"],
) -> None:
    kept = await _start(store, "slice-1-a")
    ended = await _start(store, "slice-2-a")
    refs = BlobRefs(pool)
    for slice_id in (kept, ended):
        refs.add(f"slice-{slice_id}", HOLDER, str(slice_id))
    await store.finish(ended, status=status, sliced_blob_key=None, failure="no", warnings=[])
    found = await store.get(ended, "out-1")
    assert found is not None and found.status == status
    assert refs.referenced() == {f"slice-{kept}"}


async def test_lost_lists_old_running_rows(store: NativeSliceStore) -> None:
    running = await _start(store, "slice-1-a")
    done = await _start(store, "slice-2-a")
    await store.finish(
        done, status="completed", sliced_blob_key="slice-x", failure=None, warnings=[]
    )
    assert await store.lost(timedelta(hours=1)) == []
    assert [row.id for row in await store.lost(timedelta(0))] == [running]


async def test_start_on_a_completed_row_returns_it(store: NativeSliceStore) -> None:
    slice_id = await _start(store)
    await store.finish(
        slice_id, status="completed", sliced_blob_key="slice-abc", failure=None, warnings=["w"]
    )
    again = await store.start(
        workflow_id="slice-1-a", subject="out-1", library_file_id=1, plate_id=1, preset_key="k"
    )
    assert (again.id, again.status, again.sliced_blob_key) == (slice_id, "completed", "slice-abc")
    assert again.warnings == ["w"] and again.finished_at is not None


async def test_a_completed_row_answered_again_is_kept_from_the_ttl_sweep(
    store: NativeSliceStore,
) -> None:
    """Task 12 review M5: the grace runs from its last use, so a dialog reopened on it
    past `PREVIEW_GRACE` does not lose it to the next sweep."""
    slice_id = await _start(store)
    await store.finish(
        slice_id, status="completed", sliced_blob_key="slice-abc", failure=None, warnings=[]
    )
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE native_slices SET finished_at = now() - interval '2 days' WHERE id = %s",
            (slice_id,),
        )
    again = await store.start(
        workflow_id="slice-1-a", subject="out-1", library_file_id=1, plate_id=1, preset_key="k"
    )
    assert again.status == "completed"
    assert await store.sweep(timedelta(days=1)) == []


async def test_reusable_finds_a_completed_row_not_a_failed_one(store: NativeSliceStore) -> None:
    done = await _start(store, "slice-1-a", library_file_id=1)
    failed = await _start(store, "slice-2-a", library_file_id=2)
    for slice_id in (done, failed):
        await store.prepared(slice_id, "sha")
    await store.finish(
        done, status="completed", sliced_blob_key="slice-out", failure=None, warnings=[]
    )
    await store.finish(failed, status="failed", sliced_blob_key=None, failure="no", warnings=[])

    found = await store.reusable("sha", "k")
    assert found is not None and found.id == done and found.input_sha256 == "sha"
    assert await store.reusable("sha", "other") is None
    assert await store.reusable("other", "k") is None

    await store.finish(done, status="failed", sliced_blob_key=None, failure="no", warnings=[])
    assert await store.reusable("sha", "k") is None


async def test_reusable_is_the_newest_completed(store: NativeSliceStore) -> None:
    older = await _start(store, "slice-1-a", library_file_id=1)
    newer = await _start(store, "slice-2-a", library_file_id=2)
    for slice_id, key in ((older, "slice-old"), (newer, "slice-new")):
        await store.prepared(slice_id, "sha")
        await store.finish(
            slice_id, status="completed", sliced_blob_key=key, failure=None, warnings=[]
        )
    found = await store.reusable("sha", "k")
    assert found is not None and found.id == newer


async def test_get_under_another_subject_is_none(store: NativeSliceStore) -> None:
    slice_id = await _start(store)
    found = await store.get(slice_id, "out-1")
    assert found is not None and found.workflow_id == "slice-1-a"
    assert await store.get(slice_id, "out-2") is None
    assert await store.get(slice_id + 1000, "out-1") is None


async def test_sweep_deletes_finished_rows_and_their_holds(
    store: NativeSliceStore,
    pool: Pool,  # noqa: F811
) -> None:
    running = await _start(store, "slice-1-a")
    done = await _start(store, "slice-2-a")
    failed = await _start(store, "slice-3-a")
    await store.finish(
        done, status="completed", sliced_blob_key="slice-x", failure=None, warnings=[]
    )
    await store.finish(failed, status="failed", sliced_blob_key=None, failure="no", warnings=[])
    refs = BlobRefs(pool)
    for slice_id in (running, done, failed):
        refs.add(f"slice-{slice_id}", HOLDER, str(slice_id))

    assert await store.sweep(timedelta(hours=1)) == []
    assert sorted(await store.sweep(timedelta(0))) == sorted([done, failed])

    assert await store.get(running, "out-1") is not None
    assert await store.get(done, "out-1") is None
    assert refs.referenced() == {f"slice-{running}"}


def test_native_workflow_id_names_the_copy_its_bytes_its_subject_and_the_presets() -> None:
    """Task 12 review M6: Bambuddy's SQLite library reuses the id of a deleted newest
    file, so the id alone does not name the copy; its sha256 and subject do."""

    def named(
        file_id: int = 7, key: str = "k", *, sha: str | None = "a" * 64, subject: str = "out-1"
    ) -> str:
        return native_workflow_id(file_id, key, subject=subject, copy_sha256=sha)

    assert named() == named()
    assert (
        len({named(), named(8), named(key="j"), named(sha="b" * 64), named(subject="out-2")}) == 5
    )
    assert named(sha=None) != named()
    assert named().startswith("slice-7-")
    assert len(named()) == len("slice-7-") + 32


async def test_a_stop_of_a_lost_row_spares_a_newer_execution(
    store: NativeSliceStore,
    pool: Pool,  # noqa: F811
) -> None:
    """Re-review N1: the sweep listed the row, then a new execution started it again;
    its stop must not write over the new run or drop its holds."""
    slice_id = await _start(store)
    assert await store.running(slice_id) is not None
    (listed,) = await store.lost(timedelta(0))
    restarted = await store.running(slice_id)
    assert restarted is not None and restarted > listed.started_at
    refs = BlobRefs(pool)
    refs.add("slice-new-run", HOLDER, str(slice_id))

    assert not await store.stop_lost(listed, "The slice stopped.")

    found = await store.get(slice_id, "out-1")
    assert found is not None and (found.status, found.failure) == ("running", None)
    assert refs.referenced() == {"slice-new-run"}

    (again,) = await store.lost(timedelta(0))
    assert await store.stop_lost(again, "The slice stopped.")
    found = await store.get(slice_id, "out-1")
    assert found is not None and (found.status, found.failure) == ("failed", "The slice stopped.")
    assert refs.referenced() == set()


async def test_lost_reads_the_executions_start_not_the_rows(store: NativeSliceStore) -> None:
    slice_id = await _start(store)
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE native_slices SET created_at = now() - interval '1 day' WHERE id = %s",
            (slice_id,),
        )
    await store.running(slice_id)
    assert await store.lost(timedelta(hours=1)) == []


async def test_running_a_row_swept_away_says_so(store: NativeSliceStore) -> None:
    """Re-review N2: an execution whose row the TTL sweep deleted takes no hold."""
    assert await store.running(12345) is None


async def test_a_restart_of_a_finished_row_keeps_it_from_the_ttl_sweep(
    store: NativeSliceStore,
) -> None:
    slice_id = await _start(store)
    await store.finish(slice_id, status="failed", sliced_blob_key=None, failure="x", warnings=[])
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE native_slices SET finished_at = now() - interval '2 days' WHERE id = %s",
            (slice_id,),
        )
    again = await _start(store)
    assert again == slice_id
    assert await store.sweep(timedelta(days=1)) == []
