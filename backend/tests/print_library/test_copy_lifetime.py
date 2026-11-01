"""The lifetime of a native run's per-print copies and slices (#4337, #3818 tier A Task 14):
`library_files.print_subject` marks what a run put in the native library, and
`sweep_print_copies` trashes what nothing keeps, as Bambuddy's inbox keeps its copies."""

from __future__ import annotations

import io
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.bambuddy.send import Printable, Target
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, SlicedCopy
from scadbuddy.print_library.copies import ensure_native_copy, release_moved
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.sweep import COPY_ORPHAN_GRACE, CopySweep, sweep_print_copies
from scadbuddy.render.pg_store import MIGRATIONS_DIR
from scadbuddy.render.plate import plate_for
from scadbuddy.store.index import Pool

pytestmark = pytest.mark.requires_postgres

KEY = "out-life-1"
OTHER = "out-life-2"
H2D = Target(plate_for("Bambu Lab H2D"), "0.4")
X1C = Target(plate_for("Bambu Lab X1 Carbon"), "0.4")
#: Past every grace the sweep gives a file nothing records.
LATER = timedelta(days=2)


@pytest.fixture
def uploads(pool: Pool) -> BambuddyUploadStore:
    return BambuddyUploadStore(pool)


@pytest.fixture
def payload(three_mf: Path) -> bytes:
    return three_mf.read_bytes()


def printable(payload: bytes, key: str = KEY) -> Printable:
    async def fetch() -> bytes:
        return payload

    return Printable(key=key, colours=("#FF6AC1", "#1F6FEB"), inbox_name="bracket.3mf", fetch=fetch)


async def copy_of(
    native: NativeLibrary,
    uploads: BambuddyUploadStore,
    payload: bytes,
    *,
    target: Target = H2D,
    folder_id: int | None = None,
    key: str = KEY,
) -> int:
    return await ensure_native_copy(
        native, uploads, printable(payload, key), target=target, folder_id=folder_id
    )


async def slice_of(
    native: NativeLibrary, uploads: BambuddyUploadStore, copy_id: int, *, key: str = KEY
) -> int:
    """A sliced file beside ``copy_id``, as `print_sliced_upload` and `print_record` leave
    one: marked with the copy's subject, recorded on the copy."""
    copy = native.rows.file(copy_id)
    assert copy is not None
    sliced = await native.upload(
        "bracket.plate1.gcode.3mf",
        io.BytesIO(b"gcode"),
        copy.folder_id,
        print_subject=copy.print_subject,
    )
    await uploads.record_sliced(key, copy_id, SlicedCopy(id=sliced.id, preset_key="k"))
    return sliced.id


async def sweep(
    native: NativeLibrary,
    uploads: BambuddyUploadStore,
    *,
    after: timedelta = timedelta(0),
    in_use: Iterable[str] = (),
) -> CopySweep:
    busy = set(in_use)

    async def subjects_in_use() -> set[str]:
        return busy

    return await sweep_print_copies(native, uploads, subjects_in_use, now=datetime.now(UTC) + after)


def live(native: NativeLibrary, file_id: int) -> bool:
    return native.rows.file(file_id) is not None


def trashed(native: NativeLibrary, file_id: int) -> bool:
    return native.rows.file(file_id) is None and native.rows.file(file_id, trashed=True) is not None


async def test_a_runs_copy_and_slice_carry_its_subject(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    copy_id = await copy_of(native, uploads, payload)
    sliced = await slice_of(native, uploads, copy_id)

    for file_id in (copy_id, sliced):
        row = native.rows.file(file_id)
        assert row is not None and row.print_subject == KEY


async def test_a_superseded_copy_goes_to_the_trash_with_its_slices(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    """Case 3: as Bambuddy's inbox holds one copy per output, a folder holds the subject's
    newest copy; an older one, for another target, goes with its slices. Into the trash:
    its row, and so its blob, stays until the trash's own purge."""
    folder = (await native.create_folder("Bracket", None)).id
    old = await copy_of(native, uploads, payload, folder_id=folder)
    old_slice = await slice_of(native, uploads, old)
    new = await copy_of(native, uploads, payload, target=X1C, folder_id=folder)
    new_slice = await slice_of(native, uploads, new)

    done = await sweep(native, uploads)

    assert done.superseded == 2
    assert trashed(native, old) and trashed(native, old_slice)
    assert live(native, new) and live(native, new_slice)
    assert [c.id for c in await uploads.for_output(KEY, native=True)] == [new]
    row = native.rows.file(old, trashed=True)
    assert row is not None
    assert row.blob_key in native.rows.live_file_keys()
    assert await native.content.stat(row.blob_key) is not None


async def test_each_folder_and_subject_keeps_its_own_newest(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    folder = (await native.create_folder("Elsewhere", None)).id
    top = await copy_of(native, uploads, payload)
    there = await copy_of(native, uploads, payload, target=X1C, folder_id=folder)
    others = await copy_of(native, uploads, payload, target=X1C, key=OTHER)

    done = await sweep(native, uploads)

    assert done == CopySweep(superseded=0, orphans=0, released=0)
    assert all(live(native, i) for i in (top, there, others))


async def test_an_orphaned_copy_and_its_slice_go_to_the_trash_after_a_grace(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    """Cases 1 and 2: an output deleted without its copies, or with its model, forgets
    their records (`delete_outputs`). What a run put in the library and nothing records
    goes to the trash once it is older than `COPY_ORPHAN_GRACE`."""
    copy_id = await copy_of(native, uploads, payload)
    sliced = await slice_of(native, uploads, copy_id)
    await uploads.delete_outputs([KEY])

    assert (await sweep(native, uploads)).orphans == 0
    assert live(native, copy_id) and live(native, sliced)

    done = await sweep(native, uploads, after=COPY_ORPHAN_GRACE + timedelta(minutes=1))

    assert done.orphans == 2
    assert trashed(native, copy_id) and trashed(native, sliced)


async def test_a_slice_never_recorded_goes_too(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    """A slice of a library file printed as it is (no copy to record it on) is recorded
    nowhere: litter once its run has ended."""
    stray = await native.upload(
        "bracket.plate1.gcode.3mf", io.BytesIO(b"g"), None, print_subject="library:1000000001"
    )

    assert (await sweep(native, uploads, after=LATER)).orphans == 1
    assert trashed(native, stray.id)


async def test_a_moved_copy_and_its_slices_are_the_persons(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    """Case 4: a copy a person moved is theirs, with its slices, as Bambuddy's inbox copy
    once moved is (#2670): never superseded, never trashed as an orphan."""
    elsewhere = (await native.create_folder("Mine", None)).id
    moved = await copy_of(native, uploads, payload)
    moved_slice = await slice_of(native, uploads, moved)
    await native.update_file(moved, folder_id=elsewhere)
    await release_moved(native, uploads, [moved])
    newer = await copy_of(native, uploads, payload, target=X1C)
    await uploads.delete_outputs([KEY])

    done = await sweep(native, uploads, after=LATER)

    assert done.orphans == 1  # the newer copy only
    assert live(native, moved) and live(native, moved_slice)
    assert trashed(native, newer)
    for file_id in (moved, moved_slice):
        row = native.rows.file(file_id)
        assert row is not None and row.print_subject is None


async def test_a_copy_moved_before_the_release_is_released_by_the_sweep(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    """A recorded copy found out of its folder (moved before moves released it) is the
    person's too, with its slices: released, never trashed."""
    elsewhere = (await native.create_folder("Mine", None)).id
    moved = await copy_of(native, uploads, payload)
    moved_slice = await slice_of(native, uploads, moved)
    with native.rows._pool.connection() as conn:
        conn.execute("UPDATE library_files SET folder_id = %s WHERE id = %s", (elsewhere, moved))

    done = await sweep(native, uploads, after=LATER)

    assert done.released == 1
    assert live(native, moved) and live(native, moved_slice)
    assert await uploads.for_output(KEY, native=True) == []
    for file_id in (moved, moved_slice):
        row = native.rows.file(file_id)
        assert row is not None and row.print_subject is None


@pytest.mark.parametrize("how", ["moved", "restored"])
async def test_a_recorded_copy_made_the_persons_is_released_with_its_slices(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes, how: str
) -> None:
    """A recorded copy that no longer carries its mark is a person's: one a move cleared
    whose release never ran (`release_moved` after the move), or one restored from the
    trash. Its slices are released with it, never left marked under it for good."""
    copy_id = await copy_of(native, uploads, payload)
    sliced = await slice_of(native, uploads, copy_id)
    if how == "moved":
        elsewhere = (await native.create_folder("Mine", None)).id
        await native.update_file(copy_id, folder_id=elsewhere)
    else:
        await native.trash([copy_id])
        await native.restore([copy_id])

    done = await sweep(native, uploads, after=LATER)

    assert done.released == 1
    assert live(native, copy_id) and live(native, sliced)
    row = native.rows.file(sliced)
    assert row is not None and row.print_subject is None
    assert await uploads.for_output(KEY, native=True) == []


async def test_a_purged_copys_slices_go_as_orphans(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    """A recorded copy whose row is gone (a person trashed it, and the trash was purged)
    keeps nothing: its record goes, and then its slices as orphans."""
    copy_id = await copy_of(native, uploads, payload)
    sliced = await slice_of(native, uploads, copy_id)
    await native.trash([copy_id])
    trashed_row = native.rows.file(copy_id, trashed=True)
    assert trashed_row is not None

    # Trashed: a restore may still bring it back, so its record stays.
    await sweep(native, uploads, after=LATER)
    assert [c.id for c in await uploads.for_output(KEY, native=True)] == [copy_id]

    assert await native.purge(copy_id, datetime.now(UTC) + LATER)
    await sweep(native, uploads, after=LATER)
    assert await uploads.for_output(KEY, native=True) == []
    assert (await sweep(native, uploads, after=LATER)).orphans == 1
    assert trashed(native, sliced)


async def test_a_folder_keeps_the_copy_its_subject_used_last(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    """A run that reuses an older copy (its target again) makes it the one kept: the
    copy just printed never goes for one uploaded later and not used since."""
    reused = await copy_of(native, uploads, payload)
    later = await copy_of(native, uploads, payload, target=X1C)
    assert await copy_of(native, uploads, payload) == reused

    done = await sweep(native, uploads)

    assert done.superseded == 1
    assert live(native, reused) and trashed(native, later)
    assert [c.id for c in await uploads.for_output(KEY, native=True)] == [reused]


async def test_a_subject_a_run_takes_up_during_the_sweep_keeps_its_files(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    """The subjects in use are read again just before the trash: a run that starts
    between the reads (and may reuse any of its copies) keeps every file of its subject."""
    old = await copy_of(native, uploads, payload)
    await copy_of(native, uploads, payload, target=X1C)
    reads: list[set[str]] = [set(), {KEY}]

    async def subjects_in_use() -> set[str]:
        return reads.pop(0)

    done = await sweep_print_copies(native, uploads, subjects_in_use, now=datetime.now(UTC))

    assert done.superseded == 0
    assert live(native, old)
    assert len(await uploads.for_output(KEY, native=True)) == 2


async def test_a_persons_file_of_the_same_name_and_bytes_is_never_swept(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    """Case 5: only what a run uploaded carries a subject; a person's own file, however
    like a copy, carries none."""
    theirs = await native.upload("bracket.3mf", io.BytesIO(payload), None)
    sliced = await native.upload("bracket.plate1.gcode.3mf", io.BytesIO(b"gcode"), None)

    assert await sweep(native, uploads, after=LATER) == CopySweep(
        superseded=0, orphans=0, released=0
    )
    assert live(native, theirs.id) and live(native, sliced.id)


async def test_a_subject_a_run_still_uses_is_left_alone(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    old = await copy_of(native, uploads, payload)
    await copy_of(native, uploads, payload, target=X1C)
    orphan = await copy_of(native, uploads, payload, key=OTHER)
    await uploads.delete_outputs([OTHER])

    done = await sweep(native, uploads, after=LATER, in_use={KEY, OTHER})

    assert done == CopySweep(superseded=0, orphans=0, released=0)
    assert live(native, old) and live(native, orphan)


async def test_a_restored_or_copied_per_print_file_is_the_persons(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes
) -> None:
    """A person who restores a trashed copy, or copies one, wants that file: theirs."""
    first = await copy_of(native, uploads, payload)
    await native.trash([first])
    await native.restore([first])
    duplicate = await native.copy(first, None)

    for file_id in (first, duplicate.id):
        row = native.rows.file(file_id)
        assert row is not None and row.print_subject is None


async def test_the_migration_marks_what_runs_put_in_the_library(
    native: NativeLibrary, uploads: BambuddyUploadStore, payload: bytes, pool: Pool
) -> None:
    """How the files cases 1 and 2 orphaned before #4337 are reached: a recorded copy
    still where it was recorded and the slices on it, and a copy or slice a finished
    run's result names, still in the run's folder and recorded by nothing. Never a
    moved copy, a person's file, or the library file a library print printed as it is."""
    elsewhere = (await native.create_folder("Mine", None)).id
    recorded = await copy_of(native, uploads, payload)
    recorded_slice = await slice_of(native, uploads, recorded)
    moved = await copy_of(native, uploads, payload, target=X1C)
    orphan = await copy_of(native, uploads, payload, key=OTHER)
    orphan_slice = await native.upload("o.plate1.gcode.3mf", io.BytesIO(b"o"), None)
    own = await native.upload("own.gcode.3mf", io.BytesIO(b"own"), None)
    own_slice = await native.upload("own.plate1.gcode.3mf", io.BytesIO(b"s"), None)
    theirs = await native.upload("bracket.3mf", io.BytesIO(payload), None)
    await uploads.delete_outputs([OTHER])
    with pool.connection() as conn:
        conn.execute("UPDATE library_files SET folder_id = %s WHERE id = %s", (elsewhere, moved))
        conn.execute("UPDATE library_files SET print_subject = NULL")
        for run_id, subject, result in [
            (
                "r-orphan",
                OTHER,
                {"library_file_id": orphan, "sliced_library_file_id": orphan_slice.id},
            ),
            (
                "r-own",
                f"library:{own.id}",
                {"library_file_id": own.id, "sliced_library_file_id": own_slice.id},
            ),
        ]:
            conn.execute(
                "INSERT INTO print_runs (id, output_id, idempotency_key, status, result)"
                " VALUES (%s, %s, %s, 'succeeded', %s::jsonb)",
                (run_id, subject, run_id, json.dumps({**result, "copies": 1, "folder_id": None})),
            )
        conn.execute((MIGRATIONS_DIR / MIGRATION).read_text())

    marks = {f.id: f.print_subject for f in native.rows.files(None, every_folder=True)}
    assert marks[recorded] == KEY and marks[recorded_slice] == KEY
    assert marks[orphan] == OTHER and marks[orphan_slice.id] == OTHER
    assert marks[own_slice.id] == f"library:{own.id}"
    assert marks[moved] is None and marks[own.id] is None and marks[theirs.id] is None


MIGRATION = "20261101T1528Z_library_files_print_subject.sql"
