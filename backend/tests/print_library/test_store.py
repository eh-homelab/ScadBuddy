from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from scadbuddy.print_library.ids import NATIVE_ID_BASE, is_native_id
from scadbuddy.print_library.store import (
    FolderCycleError,
    NativeFile,
    NativeLibraryStore,
    Unset,
)
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

_keys = count(1)


def file_row(**over: object) -> NativeFile:
    n = next(_keys)
    fields: dict[str, object] = {
        "id": n,
        "folder_id": None,
        "name": f"f{n}.3mf",
        "size": 3,
        "sha256": "0" * 64,
        "blob_key": f"library-file-test-{n}",
        "media_type": "model/3mf",
        "plates": [],
        "has_thumbnail": False,
        "output_id": None,
        "filed": False,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "deleted_at": None,
    }
    fields.update(over)
    return NativeFile.model_validate(fields)


@pytest.fixture
def rows(pg_pool: PgPool) -> NativeLibraryStore:
    return NativeLibraryStore(pg_pool)


def stored(rows: NativeLibraryStore, **over: object) -> NativeFile:
    row = file_row(**over)
    return rows.insert_file(row.model_copy(update={"id": rows.next_file_id()}))


def test_ids_start_at_the_native_base(rows: NativeLibraryStore) -> None:
    folder = rows.create_folder("Dinos", None)
    assert folder.id >= NATIVE_ID_BASE and rows.next_file_id() >= NATIVE_ID_BASE
    assert is_native_id(folder.id) and not is_native_id(42)


def test_file_type_of_a_sliced_file() -> None:
    assert file_row(name="a.GCODE.3mf").file_type == "gcode.3mf"
    assert file_row(name="a.3MF").file_type == "3mf"


def test_trash_hides_and_restore_returns_under_the_same_id(rows: NativeLibraryStore) -> None:
    f = stored(rows)
    rows.trash([f.id])
    assert rows.file(f.id) is None and rows.file(f.id, trashed=True) is not None
    assert rows.restore([f.id]) == [f.id] and rows.file(f.id) is not None


def test_a_trashed_file_of_a_deleted_folder_restores_to_the_top(rows: NativeLibraryStore) -> None:
    d = rows.create_folder("X", None)
    f = stored(rows, folder_id=d.id)
    rows.trash([f.id])
    assert rows.delete_folder(d.id)
    rows.restore([f.id])
    back = rows.file(f.id)
    assert back is not None and back.folder_id is None


def test_a_folder_cannot_move_under_itself(rows: NativeLibraryStore) -> None:
    a = rows.create_folder("a", None)
    b = rows.create_folder("b", a.id)
    with pytest.raises(FolderCycleError):
        rows.update_folder(a.id, name=None, parent_id=b.id)
    with pytest.raises(FolderCycleError):
        rows.update_folder(a.id, name=None, parent_id=a.id)


def test_template_folder_is_found_or_made_once(rows: NativeLibraryStore) -> None:
    assert rows.template_folder("Sign").id == rows.template_folder("Sign").id


def test_names_may_repeat(rows: NativeLibraryStore) -> None:
    stored(rows, name="x.3mf")
    stored(rows, name="x.3mf")
    assert [f.name for f in rows.files(None)] == ["x.3mf", "x.3mf"]


def test_purge_trashed_is_conditional(rows: NativeLibraryStore) -> None:
    f = stored(rows)
    cutoff = datetime.now(UTC) + timedelta(seconds=5)
    assert rows.purge_trashed(f.id, cutoff) is None  # live
    rows.trash([f.id])
    assert [t.id for t in rows.trashed_before(cutoff)] == [f.id]
    rows.restore([f.id])
    assert rows.purge_trashed(f.id, cutoff) is None  # restored before the purge
    rows.trash([f.id])
    purged = rows.purge_trashed(f.id, cutoff)
    assert purged is not None and purged.blob_key == f.blob_key
    assert rows.file(f.id, trashed=True) is None


def test_update_file_distinguishes_top_level_from_not_given(rows: NativeLibraryStore) -> None:
    d = rows.create_folder("d", None)
    f = stored(rows, folder_id=d.id, filed=True)
    kept = rows.update_file(f.id, name="n.3mf", folder_id=Unset, filed=None)
    assert kept is not None and kept.folder_id == d.id and kept.name == "n.3mf" and kept.filed
    moved = rows.update_file(f.id, name=None, folder_id=None, filed=False)
    assert moved is not None and moved.folder_id is None and not moved.filed


def test_filing_and_keys(rows: NativeLibraryStore) -> None:
    a = stored(rows, output_id="o1", filed=True)
    stored(rows, output_id="o2")
    assert [f.id for f in rows.filed_for_output("o1")] == [a.id]
    assert [f.id for f in rows.files_by_ids([a.id])] == [a.id]
    rows.trash([a.id])
    assert a.blob_key in rows.live_file_keys()
    assert rows.files_by_ids([a.id]) == [] and rows.filed_for_output("o1") == []
    assert len(rows.files(None, every_folder=True)) == 1


def _wait_for_a_lock_wait(pool: PgPool) -> None:
    """Until some backend of this database waits on a lock (the delete, here)."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()
        if row is not None and row["n"] > 0:
            return
        time.sleep(0.05)
    raise AssertionError("the delete never waited on a lock")


def test_a_move_inside_a_subtree_being_deleted_does_not_deadlock(
    rows: NativeLibraryStore, pg_pool: PgPool
) -> None:
    # The move locks the file's row, then needs KEY SHARE on its target folder; the
    # delete must take the file rows before the folder rows, or each waits on the other.
    a = rows.create_folder("a", None)
    b = rows.create_folder("b", a.id)
    c = rows.create_folder("c", a.id)
    f = stored(rows, folder_id=b.id)
    with ThreadPoolExecutor(1) as pool:
        with pg_pool.connection() as conn, conn.transaction():
            conn.execute("SELECT id FROM library_files WHERE id = %s FOR UPDATE", (f.id,))
            deleting = pool.submit(rows.delete_folder_tree, a.id)
            _wait_for_a_lock_wait(pg_pool)
            conn.execute(
                "UPDATE library_files SET folder_id = %s, updated_at = now() WHERE id = %s",
                (c.id, f.id),
            )
        done = deleting.result(timeout=30)
    assert done is not None
    trashed, folders = done
    assert [t.id for t in trashed] == [f.id] and set(folders) == {a.id, b.id, c.id}
    assert rows.files(None) == [] and rows.file(f.id, trashed=True) is not None
