from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest

from scadbuddy.print_library import store as store_module
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
    """Until some backend of this database waits on a lock."""
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
    raise AssertionError("no backend ever waited on a lock")


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


def test_a_bulk_trash_overlapping_a_folder_delete_does_not_deadlock(
    rows: NativeLibraryStore, pg_pool: PgPool
) -> None:
    # The folder delete locks its file rows by id; a trash that locked them in the order
    # its scan met them would hold f2 while waiting on f1, which the delete takes first.
    # Rewriting f1 puts its live tuple after f2's, so a scan meets f2 first.
    a = rows.create_folder("a", None)
    f1 = stored(rows, folder_id=a.id)
    f2 = stored(rows, folder_id=a.id)
    rows.update_file(f1.id, name="f1.3mf", folder_id=Unset, filed=None)
    with pg_pool.connection() as conn:
        conn.execute("ANALYZE library_files")
    with ThreadPoolExecutor(2) as pool:
        with pg_pool.connection() as conn, conn.transaction():
            conn.execute("SELECT id FROM library_files WHERE id = %s FOR UPDATE", (f1.id,))
            deleting = pool.submit(rows.delete_folder_tree, a.id)
            _wait_for_a_lock_wait(pg_pool)
            trashing = pool.submit(rows.trash, [f1.id, f2.id])
            _wait_for_lock_waits(pg_pool, 2)
        done = deleting.result(timeout=30)
        trashed = trashing.result(timeout=30)
    assert done is not None
    assert sorted(f.id for f in done[0]) == [f1.id, f2.id]
    assert trashed == []


def _wait_for_lock_waits(pool: PgPool, n: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()
        if row is not None and row["n"] >= n:
            return
        time.sleep(0.05)
    raise AssertionError(f"fewer than {n} backends ever waited on a lock")


def _paused_after_writing(
    monkeypatch: pytest.MonkeyPatch, file_id: int
) -> tuple[threading.Event, threading.Event]:
    """Pause the write of ``file_id`` (an insert or an update) after its statement, its
    transaction still open: ``written`` once it has written, then waits for ``go``."""
    written, go = threading.Event(), threading.Event()
    insert, update = store_module._insert_file, store_module._update_file

    def hold(target: int) -> None:
        if target == file_id:
            written.set()
            assert go.wait(30)

    def insert_paused(conn: Any, row: NativeFile) -> NativeFile:
        out = insert(conn, row)
        hold(row.id)
        return out

    def update_paused(conn: Any, id: int, sets: list[str], params: list[Any]) -> NativeFile | None:
        out = update(conn, id, sets, params)
        hold(id)
        return out

    monkeypatch.setattr(store_module, "_insert_file", insert_paused)
    monkeypatch.setattr(store_module, "_update_file", update_paused)
    return written, go


@pytest.mark.parametrize("writer", ["upload", "move", "rename"])
def test_a_put_beside_another_write_into_the_folder_takes_another_name(
    rows: NativeLibraryStore, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    """#4512: every write of a name into a folder takes its ``folder-names`` lock, so a
    put whose name pick overlaps an upload, move or rename of ``Sign.3mf`` into the folder
    waits for it, sees the name and takes ``Sign (2).3mf``. Before, only puts took it, and
    the put picked ``Sign.3mf`` beside the other write's uncommitted one."""
    gifts = rows.create_folder("Gifts", None).id
    if writer == "upload":
        row = file_row(folder_id=gifts, name="Sign.3mf").model_copy(
            update={"id": rows.next_file_id()}
        )

        def write() -> object:
            return rows.insert_file(row)

    else:
        row = stored(rows, folder_id=None if writer == "move" else gifts, name="Old.3mf")
        if writer == "move":
            rows.update_file(row.id, name="Sign.3mf", folder_id=Unset, filed=None)

        def write() -> object:
            if writer == "move":
                return rows.update_file(row.id, name=None, folder_id=gifts, filed=None)
            return rows.update_file(row.id, name="Sign.3mf", folder_id=Unset, filed=None)

    written, go = _paused_after_writing(monkeypatch, row.id)
    put = file_row(output_id="c" * 32).model_copy(update={"id": rows.next_file_id()})
    with ThreadPoolExecutor(2) as pool:
        writing = pool.submit(write)
        assert written.wait(30)
        putting = pool.submit(rows.put_for_output, put, gifts, "Sign")
        try:
            _wait_for_a_lock_wait(pg_pool)
            assert not putting.done()
        finally:
            go.set()
        writing.result(timeout=30)
        placed, inserted = putting.result(timeout=30)
    assert inserted and placed.name == "Sign (2).3mf"
    assert sorted(f.name for f in rows.files(gifts)) == ["Sign (2).3mf", "Sign.3mf"]
