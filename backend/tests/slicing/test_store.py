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


async def test_start_leaves_a_failed_row_to_its_next_execution(store: NativeSliceStore) -> None:
    """The execution owns the running transition (review I2): a start that joins one
    closing never sets the row running behind it."""
    slice_id = await _start(store)
    await store.finish(
        slice_id, status="failed", sliced_blob_key=None, failure="The slicer failed: x", warnings=[]
    )
    again = await store.start(
        workflow_id="slice-1-a", subject="out-1", library_file_id=1, plate_id=1, preset_key="k"
    )
    assert (again.id, again.status) == (slice_id, "failed")

    await store.running(slice_id)
    found = await store.get(slice_id, "out-1")
    assert found is not None
    assert (found.status, found.failure, found.finished_at) == ("running", None, None)


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


def test_native_workflow_id_names_the_copy_and_the_presets() -> None:
    assert native_workflow_id(7, "k") == native_workflow_id(7, "k")
    assert native_workflow_id(7, "k") != native_workflow_id(8, "k")
    assert native_workflow_id(7, "k") != native_workflow_id(7, "j")
    assert native_workflow_id(7, "k").startswith("slice-7-")
    assert len(native_workflow_id(7, "k")) == len("slice-7-") + 32
