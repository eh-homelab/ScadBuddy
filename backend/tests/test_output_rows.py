"""`OutputRows` (#7255, outputs-in-Postgres spec 2026-11-23 §4.2): the saved outputs as
rows. The tables come first; the row store's tests follow."""

from __future__ import annotations

import dataclasses
import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from scadbuddy.core.paths import DataPaths
from scadbuddy.library import output_rows
from scadbuddy.library.output_index import OutputIndex
from scadbuddy.library.output_query import AfterNotListedError, OutputQuery, params_text
from scadbuddy.library.output_rows import (
    BYTES_LOCK,
    COPY_LOCK,
    ByteWrite,
    OutputBlobs,
    OutputFileBlob,
    OutputRow,
    OutputRows,
    output_blob_key,
    output_file_key,
)
from scadbuddy.library.outputs import STOREFRONT, OutputMeta, OutputSource
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.projection import JobProjection

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    jobs = JobProjection(pg_conninfo, pool_size=2)
    jobs.open()
    try:
        yield jobs
    finally:
        jobs.close()


def test_outputs_tables_exist(projection: JobProjection) -> None:
    with projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO outputs (output_id, slug, meta, created_at)"
            " VALUES ('o1', 'cube', '{\"id\": \"o1\"}', now())"
        )
        run = conn.execute("INSERT INTO output_copy_runs DEFAULT VALUES RETURNING id").fetchone()
        row = conn.execute("SELECT * FROM outputs WHERE output_id = 'o1'").fetchone()
        assert run is not None and row is not None
        assert row["slug"] == "cube"
        assert row["params"] == {}
        assert row["inputs"] is None
        assert row["has_plate_cover"] is False
        assert row["copied_at"] is not None
        copy_run = conn.execute(
            "SELECT * FROM output_copy_runs WHERE id = %s", (run["id"],)
        ).fetchone()
        assert copy_run is not None
        assert copy_run["finished_at"] is None
        assert copy_run["not_copied"] == []
        assert copy_run["copied"] == 0


def test_output_bytes_tables_exist(projection: JobProjection) -> None:
    """Phase 2 (#7256, spec §4.2, §4.3): an output's blob keys, its files and its derived
    data; a deleted output takes its child rows with it."""
    with projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO outputs (output_id, slug, meta, created_at, model_blob)"
            " VALUES ('o1', 'cube', '{\"id\": \"o1\"}', now(), 'output-o1-model')"
        )
        conn.execute(
            "INSERT INTO output_files (output_id, name, blob_key, size)"
            " VALUES ('o1', 'a.stl', 'output-o1-file-0', 3)"
        )
        conn.execute(
            "INSERT INTO output_derived (output_id, kind, plate, version, data)"
            " VALUES ('o1', 'geometry', 1, 'v1', '{}')"
        )
        row = conn.execute("SELECT * FROM outputs WHERE output_id = 'o1'").fetchone()
        assert row is not None
        assert row["model_blob"] == "output-o1-model"
        assert row["bytes_at"] is None and row["damaged"] == []
        run = conn.execute("INSERT INTO output_byte_runs DEFAULT VALUES RETURNING *").fetchone()
        assert run is not None and run["failed"] == [] and run["finished_at"] is None
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                "INSERT INTO output_derived (output_id, kind, plate, version, data)"
                " VALUES ('o1', 'other', 1, 'v1', '{}')"
            )
        conn.execute("DELETE FROM outputs WHERE output_id = 'o1'")
        for table in ("output_files", "output_derived"):
            left = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            assert left is not None and left["n"] == 0


@pytest.fixture
def rows(projection: JobProjection) -> OutputRows:
    return OutputRows(projection.pool)


def make_row(
    output_id: str,
    slug: str = "cube",
    age: timedelta = timedelta(0),
    *,
    name: str | None = None,
    has_plate_cover: bool = False,
) -> OutputRow:
    meta = OutputMeta(
        id=output_id,
        slug=slug,
        name=name,
        job_id="j",
        created_at=NOW - age,
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )
    return OutputRow(
        output_id=output_id,
        slug=slug,
        meta=meta,
        params={"width": 20},
        inputs=None,
        record=None,
        manifest=None,
        bom=None,
        arranged_from=None,
        backfill=None,
        has_plate_cover=has_plate_cover,
        copied_at=NOW,
    )


def test_insert_then_get_round_trips_meta_and_jsons(rows: OutputRows) -> None:
    row = make_row("o1", name="Cube")
    full = OutputRow(
        **{
            **row.__dict__,
            "inputs": {"params": {"width": 20}},
            "record": {"plate_key": "k"},
            "manifest": [{"part": "p"}],
            "bom": [{"piece": "a"}],
            "arranged_from": ["x", "y"],
            "backfill": {"job_id": "j"},
        }
    )
    rows.insert(full)
    got = rows.get("o1")
    assert got is not None
    assert got.meta == full.meta
    assert got.params == {"width": 20}
    assert got.inputs == {"params": {"width": 20}}
    assert got.record == {"plate_key": "k"}
    assert got.manifest == [{"part": "p"}]
    assert got.bom == [{"piece": "a"}]
    assert got.arranged_from == ["x", "y"]
    assert got.backfill == {"job_id": "j"}
    assert rows.get("missing") is None
    assert rows.slug_of("o1") == "cube"
    assert rows.slug_of("missing") is None


def test_existing_returns_only_known_ids(rows: OutputRows) -> None:
    rows.insert(make_row("a"))
    rows.insert(make_row("b"))
    assert rows.existing(["a", "c", "b"]) == {"a", "b"}
    assert rows.existing([]) == set()


def test_for_slug_is_newest_first(rows: OutputRows) -> None:
    rows.insert(make_row("old", age=timedelta(hours=2)))
    rows.insert(make_row("new", age=timedelta(hours=1)))
    rows.insert(make_row("other", slug="ball"))
    assert [r.output_id for r in rows.for_slug("cube")] == ["new", "old"]


def test_insert_conflict_raises(rows: OutputRows) -> None:
    rows.insert(make_row("o1"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        rows.insert(make_row("o1"))


def test_update_meta_changes_name(rows: OutputRows) -> None:
    rows.insert(make_row("o1", name="Old"))
    rows.update_meta("o1", make_row("o1", name="New").meta)
    got = rows.get("o1")
    assert got is not None
    assert got.meta.name == "New"


def test_every_live_writer_moves_copied_at(rows: OutputRows) -> None:
    """A row a live writer touched after the copy's mark is one the copy leaves alone."""
    rows.insert(make_row("o1"))
    writes: list[Callable[[], None]] = [
        lambda: rows.update_meta("o1", make_row("o1", name="New").meta),
        lambda: rows.set_backfill("o1", {"job_id": "j"}),
        lambda: rows.attach_backfill("o1", {"plate_key": "p"}, []),
    ]
    for write in writes:
        mark = rows.clock()
        write()
        got = rows.get("o1")
        assert got is not None and got.copied_at > mark


def test_set_backfill_and_pending_backfills(rows: OutputRows) -> None:
    rows.insert(make_row("o1"))
    rows.insert(make_row("o2"))
    assert rows.pending_backfills() == []
    rows.set_backfill("o1", {"job_id": "j1"})
    assert rows.pending_backfills() == [("o1", {"job_id": "j1"})]
    rows.set_backfill("o1", None)
    assert rows.pending_backfills() == []


def test_delete_slug_drops_every_row_of_slug(rows: OutputRows) -> None:
    rows.insert(make_row("a"))
    rows.insert(make_row("b"))
    rows.insert(make_row("c", slug="ball"))
    rows.delete_slug("cube")
    assert rows.existing(["a", "b", "c"]) == {"c"}
    rows.delete("c")
    assert rows.existing(["c"]) == set()


def test_oldest_with_plate_cover_picks_oldest_flagged(rows: OutputRows) -> None:
    rows.insert(make_row("oldest", age=timedelta(hours=3)))
    rows.insert(make_row("mid", age=timedelta(hours=2), has_plate_cover=True))
    rows.insert(make_row("new", age=timedelta(hours=1), has_plate_cover=True))
    assert rows.oldest_with_plate_cover("cube") == "mid"
    assert rows.oldest_with_plate_cover("ball") is None


def test_write_copy_sets_copied_at_and_ids_copied_before_finds_the_rest(
    rows: OutputRows,
) -> None:
    rows.write_copy([make_row("a")], [], rows.clock())
    # The mark is the database's clock, as the copy's is (`run_started_at`).
    mark = rows.run_started_at(rows.start_run())
    assert rows.write_copy([make_row("b")], [make_row("a", name="again")], rows.clock()) == (
        {"b"},
        {"a"},
    )
    assert rows.ids_copied_before(mark) == set()
    rows.write_copy([make_row("c")], [], rows.clock())
    later = rows.run_started_at(rows.start_run())
    assert rows.ids_copied_before(later) == {"a", "b", "c"}
    got = rows.get("a")
    assert got is not None
    assert got.meta.name == "again"


def test_write_copy_skips_rows_written_after_its_mark(rows: OutputRows) -> None:
    """A row a writer touched after the mark, or deleted, stays as that writer left it."""
    rows.insert(make_row("renamed"))
    rows.insert(make_row("deleted"))
    mark = rows.clock()
    rows.update_meta("renamed", make_row("renamed", name="live").meta)
    rows.delete("deleted")
    rows.insert(make_row("saved", name="live"))
    stale = [make_row("renamed", name="old"), make_row("deleted", name="old")]
    assert rows.write_copy([make_row("saved", name="old")], stale, mark) == (set(), set())
    for output_id in ("renamed", "saved"):
        got = rows.get(output_id)
        assert got is not None and got.meta.name == "live"
    assert rows.get("deleted") is None


def _copied(output_id: str, slug: str = "cube") -> OutputRow:
    """A row as the copy writes it: with a stamp of the files it read."""
    return dataclasses.replace(make_row(output_id, slug), files_stamp="stamp")


def test_a_save_under_another_slug_does_not_replace_the_copys_row(rows: OutputRows) -> None:
    """Ids can repeat across models (`orders/quote.py` makes them with uuid5): a save
    replaces the copy's row of its own folder only, never one of another model."""
    rows.write_copy([_copied("o1", "cube")], [], rows.clock())
    with pytest.raises(psycopg.errors.UniqueViolation):
        rows.insert(make_row("o1", "ball"))
    assert rows.slug_of("o1") == "cube"
    rows.insert(make_row("o1", "cube", name="saved"))
    got = rows.get("o1")
    assert got is not None and got.meta.name == "saved" and got.files_stamp is None


def test_undo_copy_keeps_a_row_a_writer_touched_after_the_mark(rows: OutputRows) -> None:
    mark = rows.clock()
    rows.write_copy([_copied("touched"), _copied("left")], [], mark)
    rows.update_meta("touched", make_row("touched", name="live").meta)
    assert rows.undo_copy(["touched", "left"], mark) == {"left"}
    got = rows.get("touched")
    assert got is not None and got.meta.name == "live"


def test_drop_unseen_keeps_a_row_a_writer_touched_after_the_start(rows: OutputRows) -> None:
    rows.insert(make_row("touched"))
    rows.insert(make_row("left"))
    started = rows.clock()
    rows.update_meta("touched", make_row("touched", name="live").meta)
    assert rows.drop_unseen(["touched", "left"], started) == 1
    assert rows.existing(["touched", "left"]) == {"touched"}


def test_drop_copied_deletes_only_the_copys_row_of_that_slug(rows: OutputRows) -> None:
    rows.write_copy([_copied("copied"), _copied("elsewhere", "ball")], [], rows.clock())
    rows.insert(make_row("saved"))
    for output_id in ("copied", "elsewhere", "saved"):
        rows.drop_copied(output_id, "cube")
    assert rows.existing(["copied", "elsewhere", "saved"]) == {"elsewhere", "saved"}


def test_attach_backfill_keeps_a_record_and_clears_the_backfill(rows: OutputRows) -> None:
    rows.insert(make_row("o1"))
    rows.set_backfill("o1", {"job_id": "j"})
    rows.attach_backfill("o1", {"plate_key": "first"}, [{"part": "p"}])
    got = rows.get("o1")
    assert got is not None
    assert got.record == {"plate_key": "first"}
    assert got.manifest == [{"part": "p"}]
    assert got.backfill is None
    rows.attach_backfill("o1", {"plate_key": "second"}, [{"part": "q"}])
    got = rows.get("o1")
    assert got is not None
    assert got.record == {"plate_key": "first"}
    assert got.manifest == [{"part": "q"}]


def test_copied_false_until_a_run_finishes_then_stays_true(rows: OutputRows) -> None:
    assert rows.copied() is False
    assert rows.last_run() is None
    run_id = rows.start_run()
    assert rows.copied() is False
    rows.finish_run(
        run_id,
        copied=2,
        refreshed=1,
        dropped=0,
        not_copied=["x"],
        damaged=[{"id": "y", "why": "bad"}],
    )
    assert rows.copied() is True
    last = rows.last_run()
    assert last is not None
    assert last["copied"] == 2
    assert last["not_copied"] == ["x"]
    assert last["finished_at"] is not None


# Phase 2 (#7256, spec §4.2, §4.3): the row's blob keys, files, derived data and holds.


def _holds(projection: JobProjection, output_id: str) -> set[str]:
    with projection.pool.connection() as conn:
        found = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = 'output' AND holder_id = %s",
            (output_id,),
        ).fetchall()
    return {str(item["key"]) for item in found}


def _hold(projection: JobProjection, output_id: str, key: str) -> None:
    with projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, 'output', %s)",
            (key, output_id),
        )


def _blobs(output_id: str, *names: str) -> OutputBlobs:
    return OutputBlobs(
        model=output_blob_key(output_id, "model"),
        preview=output_blob_key(output_id, "preview"),
        thumbnail=None,
        files=tuple(
            OutputFileBlob(name=name, blob_key=output_file_key(output_id, name), size=n + 1)
            for n, name in enumerate(names)
        ),
    )


def test_blob_keys_follow_the_spec() -> None:
    assert output_blob_key("o1", "thumbnail") == "output-o1-thumbnail"
    assert output_file_key("o1", "a.stl") == (
        "output-o1-file-" + hashlib.sha256(b"a.stl").hexdigest()[:32]
    )
    assert output_file_key("o1", "b.stl") != output_file_key("o1", "a.stl")
    blobs = _blobs("o1", "a.stl")
    assert blobs.keys() == ["output-o1-model", "output-o1-preview", output_file_key("o1", "a.stl")]


def test_insert_with_blobs_writes_columns_files_and_holds(
    rows: OutputRows, projection: JobProjection
) -> None:
    blobs = _blobs("o1", "a.stl", "b.stl")
    rows.insert(dataclasses.replace(make_row("o1"), blobs=blobs))
    got = rows.get("o1")
    assert got is not None
    assert got.blobs == dataclasses.replace(blobs, files=())
    assert got.bytes_at is not None
    assert rows.files("o1") == list(blobs.files)
    assert _holds(projection, "o1") == set(blobs.keys())
    # A row with no model blob has no bytes yet: the files still hold them.
    rows.insert(make_row("o2"))
    plain = rows.get("o2")
    assert plain is not None and plain.bytes_at is None and plain.blobs == OutputBlobs()
    assert _holds(projection, "o2") == set()


def test_insert_failure_writes_no_hold(rows: OutputRows, projection: JobProjection) -> None:
    """A save that fails after it dropped the copy's row undoes the drop: the copy's row
    and its holds stay, and the save writes no file row and no hold."""
    rows.write_copy([_copied("o1")], [], rows.clock())
    copied = _blobs("o1", "a.stl")
    rows.write_byte_copy([ByteWrite("o1", copied, "stamp", [])])
    # Two `files/` entries of one name: `_write_files` fails on the key, after the drop.
    clash = dataclasses.replace(
        _blobs("o1", "b.stl"),
        files=(
            OutputFileBlob(name="b.stl", blob_key=output_file_key("o1", "b.stl"), size=1),
            OutputFileBlob(name="b.stl", blob_key=output_file_key("o1", "b.stl"), size=1),
        ),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        rows.insert(dataclasses.replace(make_row("o1"), blobs=clash))
    got = rows.get("o1")
    assert got is not None and got.files_stamp == "stamp"
    assert _holds(projection, "o1") == set(copied.keys())
    assert [f.name for f in rows.files("o1")] == ["a.stl"]


def test_set_blob_moves_bytes_at_only_on_a_row_in_blobs(rows: OutputRows) -> None:
    rows.insert(dataclasses.replace(make_row("blobbed"), blobs=_blobs("blobbed")))
    before = rows.get("blobbed")
    assert before is not None and before.bytes_at is not None
    assert rows.set_blob("blobbed", "thumbnail", output_blob_key("blobbed", "thumbnail"))
    after = rows.get("blobbed")
    assert after is not None and after.bytes_at is not None
    assert after.bytes_at > before.bytes_at
    # A plain row's bytes are still in its files: NULL keeps meaning that.
    rows.insert(make_row("plain"))
    assert rows.set_blob("plain", "thumbnail", output_blob_key("plain", "thumbnail"))
    plain = rows.get("plain")
    assert plain is not None and plain.bytes_at is None


def test_set_blob_on_deleted_row_writes_no_hold(
    rows: OutputRows, projection: JobProjection
) -> None:
    rows.insert(make_row("o1"))
    key = output_blob_key("o1", "thumbnail")
    assert rows.set_blob("o1", "thumbnail", key) is True
    got = rows.get("o1")
    assert got is not None and got.blobs.thumbnail == key
    assert _holds(projection, "o1") == {key}
    assert rows.set_blob("gone", "thumbnail", output_blob_key("gone", "thumbnail")) is False
    assert _holds(projection, "gone") == set()
    with pytest.raises(ValueError):
        rows.set_blob("o1", "meta", key)  # type: ignore[arg-type]


def test_files_are_sorted_by_name(rows: OutputRows) -> None:
    files = (
        OutputFileBlob(name="c.stl", blob_key="output-o1-file-2", size=3),
        OutputFileBlob(name="a.stl", blob_key="output-o1-file-0", size=1),
        OutputFileBlob(name="b.stl", blob_key="output-o1-file-1", size=2),
    )
    rows.insert(dataclasses.replace(make_row("o1"), blobs=OutputBlobs(files=files)))
    assert [f.name for f in rows.files("o1")] == ["a.stl", "b.stl", "c.stl"]
    assert rows.file("o1", "b.stl") == files[2]
    assert rows.file("o1", "missing.stl") is None


def test_derived_round_trip_and_version(rows: OutputRows) -> None:
    rows.insert(make_row("o1"))
    assert rows.derived("o1", "geometry", 1) is None
    rows.write_derived("o1", "geometry", 1, "v1", {"faces": 12}, model_sha=None)
    assert rows.derived("o1", "geometry", 1) == ("v1", {"faces": 12})
    rows.write_derived("o1", "geometry", 1, "v2", {"faces": 14}, model_sha=None)
    assert rows.derived("o1", "geometry", 1) == ("v2", {"faces": 14})
    assert rows.derived("o1", "geometry", 2) is None
    assert rows.derived("o1", "attributed", 1) is None
    # Nothing is written for a row that is gone.
    rows.write_derived("gone", "geometry", 1, "v1", {}, model_sha=None)
    assert rows.derived("gone", "geometry", 1) is None


def test_write_bytes_skips_deleted_row_and_its_holds(
    rows: OutputRows, projection: JobProjection
) -> None:
    rows.insert(make_row("kept"))
    rows.insert(make_row("deleted"))
    _hold(projection, "kept", output_file_key("kept", "f.stl"))
    rows.delete("deleted")
    damaged = [{"role": "thumbnail", "error": "missing"}]
    written, _ = rows.write_byte_copy(
        [
            ByteWrite("kept", _blobs("kept", "a.stl"), "stamp-1", damaged),
            ByteWrite("deleted", _blobs("deleted", "a.stl"), "stamp-1", []),
        ]
    )
    assert written == {"kept"}
    assert rows.get("deleted") is None
    assert _holds(projection, "deleted") == set()
    assert rows.files("deleted") == []
    # The kept row's old file hold, which the new files no longer name, is released.
    assert _holds(projection, "kept") == set(_blobs("kept", "a.stl").keys())
    slug, stamp, at = rows.byte_states()["kept"]
    assert (slug, stamp) == ("cube", "stamp-1") and at is not None
    assert [f.name for f in rows.files("kept")] == ["a.stl"]
    with projection.pool.connection() as conn:
        row = conn.execute("SELECT damaged FROM outputs WHERE output_id = 'kept'").fetchone()
    assert row is not None and row["damaged"] == damaged
    # A second write replaces the files.
    rows.write_byte_copy([ByteWrite("kept", _blobs("kept", "x.stl", "y.stl"), "stamp-2", [])])
    assert [f.name for f in rows.files("kept")] == ["x.stl", "y.stl"]
    assert rows.write_byte_copy([]) == (set(), [])


def test_delete_releases_byte_holds_not_part_holds(
    rows: OutputRows, projection: JobProjection
) -> None:
    part = "a" * 64
    # A render job's output key has the same prefix; it is not one of this output's.
    job_output = "output-" + "b" * 32 + "-0"
    rows.insert(dataclasses.replace(make_row("o1"), blobs=_blobs("o1", "a.stl")))
    _hold(projection, "o1", part)
    _hold(projection, "o1", job_output)
    rows.delete("o1")
    assert _holds(projection, "o1") == {part, job_output}


def test_delete_slug_releases_every_byte_hold(rows: OutputRows, projection: JobProjection) -> None:
    rows.insert(dataclasses.replace(make_row("a"), blobs=_blobs("a")))
    rows.insert(dataclasses.replace(make_row("b"), blobs=_blobs("b", "f.stl")))
    rows.insert(dataclasses.replace(make_row("c", slug="ball"), blobs=_blobs("c")))
    rows.delete_slug("cube")
    assert _holds(projection, "a") == set()
    assert _holds(projection, "b") == set()
    assert _holds(projection, "c") == set(_blobs("c").keys())


def test_undo_copy_and_drop_unseen_release_byte_holds(
    rows: OutputRows, projection: JobProjection
) -> None:
    mark = rows.clock()
    rows.write_copy([_copied("undone"), _copied("unseen"), _copied("replaced")], [], mark)
    for output_id in ("undone", "unseen", "replaced"):
        rows.write_byte_copy([ByteWrite(output_id, _blobs(output_id), "s", [])])
    assert rows.undo_copy(["undone"], mark) == {"undone"}
    assert _holds(projection, "undone") == set()
    assert rows.drop_unseen(["unseen"], rows.clock()) == 1
    assert _holds(projection, "unseen") == set()
    rows.drop_copied("replaced", "cube")
    assert _holds(projection, "replaced") == set()


def test_byte_run_round_trip(rows: OutputRows) -> None:
    assert rows.last_byte_run() is None
    run_id = rows.start_byte_run()
    last = rows.last_byte_run()
    assert last is not None and last["id"] == run_id and last["finished_at"] is None
    rows.end_byte_run(
        run_id,
        finished=False,
        incomplete=True,
        refused="store full",
        copied=1,
        verified=2,
        skipped=3,
        needed_bytes=400,
        needed_count=5,
        failed=[{"id": "x", "error": "boom"}],
        damaged=[{"id": "y", "role": "model", "error": "missing"}],
    )
    last = rows.last_byte_run()
    assert last is not None
    assert last["finished_at"] is None and last["incomplete"] is True
    assert last["refused"] == "store full"
    assert (last["copied"], last["verified"], last["skipped"]) == (1, 2, 3)
    assert (last["needed_bytes"], last["needed_count"]) == (400, 5)
    assert last["failed"] == [{"id": "x", "error": "boom"}]
    assert last["damaged"] == [{"id": "y", "role": "model", "error": "missing"}]
    second = rows.start_byte_run()
    rows.end_byte_run(
        second,
        finished=True,
        incomplete=False,
        refused=None,
        copied=0,
        verified=0,
        skipped=0,
        needed_bytes=0,
        needed_count=0,
        failed=[],
        damaged=[],
    )
    last = rows.last_byte_run()
    assert last is not None and last["id"] == second and last["finished_at"] is not None


def test_pending_bytes_counts_rows_without_bytes_at(rows: OutputRows) -> None:
    rows.insert(make_row("files"))
    rows.insert(dataclasses.replace(make_row("saved"), blobs=_blobs("saved")))
    rows.insert(make_row("copied"))
    assert rows.pending_bytes() == 2
    rows.write_byte_copy([ByteWrite("copied", _blobs("copied"), "s", [])])
    assert rows.pending_bytes() == 1


# Phase 3 release A (#7257, spec §4.2, §4.5): the list's columns on `outputs`, the
# cut-over mark and the id claims.


def _column(projection: JobProjection, output_id: str, column: str) -> object:
    with projection.pool.connection() as conn:
        found = conn.execute(
            f"SELECT {column} AS value FROM outputs WHERE output_id = %s", (output_id,)
        ).fetchone()
    assert found is not None
    return found["value"]


def test_name_and_source_follow_meta(rows: OutputRows, projection: JobProjection) -> None:
    rows.insert(make_row("o1", name="Old"))
    assert _column(projection, "o1", "name") == "Old"
    assert _column(projection, "o1", "source") == "back_office"
    renamed = make_row("o1", name="New").meta.model_copy(update={"source": STOREFRONT})
    rows.update_meta("o1", renamed)
    assert _column(projection, "o1", "name") == "New"
    assert _column(projection, "o1", "source") == "storefront"
    # A meta from before `source` is the back office's.
    with projection.pool.connection() as conn:
        conn.execute("UPDATE outputs SET meta = meta - 'source' - 'name' WHERE output_id = 'o1'")
    assert _column(projection, "o1", "name") is None
    assert _column(projection, "o1", "source") == "back_office"


def test_insert_writes_params_text_and_drops_claim(rows: OutputRows) -> None:
    assert rows.claim("o1", "cube") is True
    rows.insert(dataclasses.replace(make_row("o1"), params_text="width=20"))
    got = rows.get("o1")
    assert got is not None and got.params_text == "width=20"
    # The save's transaction took the claim: once the row goes, the id is free again.
    rows.delete("o1")
    assert rows.claim("o1", "cube") is True


def _end_byte_run(rows: OutputRows, **changes: Any) -> int:
    run = rows.start_byte_run()
    values: dict[str, Any] = {
        "finished": True,
        "incomplete": False,
        "refused": None,
        "copied": 0,
        "verified": 0,
        "skipped": 0,
        "needed_bytes": 0,
        "needed_count": 0,
        "failed": [],
        "damaged": [],
    }
    rows.end_byte_run(run, **{**values, **changes})
    return run


def _finish_row_copy(rows: OutputRows) -> None:
    rows.finish_run(rows.start_run(), copied=0, refreshed=0, dropped=0, not_copied=[], damaged=[])


def test_try_cut_over_needs_every_condition(rows: OutputRows, projection: JobProjection) -> None:
    def fresh() -> OutputRows:
        # A new object each time: `cut_over` stays True once it read True.
        return OutputRows(projection.pool)

    rows.insert(dataclasses.replace(make_row("saved"), blobs=_blobs("saved")))
    # No byte run and no row copy run.
    assert fresh().try_cut_over() is False
    _end_byte_run(rows)
    # No row copy run has finished.
    assert fresh().try_cut_over() is False
    _finish_row_copy(rows)
    # The last byte run was refused.
    _end_byte_run(rows, finished=False, refused="a bambuddy store")
    assert fresh().try_cut_over() is False
    # The last byte run names a failure.
    _end_byte_run(rows, failed=[{"output_id": "x", "error": "boom"}])
    assert fresh().try_cut_over() is False
    # The last byte run did not finish.
    _end_byte_run(rows, finished=False, incomplete=True)
    assert fresh().try_cut_over() is False
    # A row whose bytes the files still hold.
    last = _end_byte_run(rows)
    rows.insert(make_row("files"))
    assert fresh().try_cut_over() is False
    assert fresh().cut_over() is False and fresh().cut_over_at() is None
    # Every condition holds.
    rows.write_byte_copy([ByteWrite("files", _blobs("files"), "s", [])])
    assert fresh().try_cut_over() is True
    assert fresh().cut_over() is True
    assert fresh().cut_over_at() is not None
    with projection.pool.connection() as conn:
        mark = conn.execute("SELECT byte_run_id FROM output_cutover").fetchone()
    assert mark is not None and mark["byte_run_id"] == last


@pytest.mark.parametrize("lock", [COPY_LOCK, BYTES_LOCK], ids=["row copy", "byte copy"])
def test_try_cut_over_waits_for_a_running_copy(
    rows: OutputRows, projection: JobProjection, pg_conninfo: str, lock: str
) -> None:
    """A2 review: a copy that holds its lock may still write a row with ``bytes_at``
    NULL, so no mark is written while it runs."""
    _finish_row_copy(rows)
    _end_byte_run(rows)
    with psycopg.connect(pg_conninfo, autocommit=True) as copy:
        copy.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock,))
        assert rows.try_cut_over() is False
        assert rows.cut_over_at() is None
        copy.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock,))
    assert rows.try_cut_over() is True


def test_try_cut_over_needs_the_last_row_copy_run_finished(rows: OutputRows) -> None:
    """A2 review: an earlier finished row copy run is not enough; the last one must be."""
    _finish_row_copy(rows)
    rows.start_run()
    _end_byte_run(rows)
    assert rows.try_cut_over() is False
    _finish_row_copy(rows)
    assert rows.try_cut_over() is True


def test_try_cut_over_is_written_once(rows: OutputRows, projection: JobProjection) -> None:
    _finish_row_copy(rows)
    first = _end_byte_run(rows)
    assert rows.try_cut_over() is True
    at = rows.cut_over_at()
    _end_byte_run(rows)
    # The mark exists: this call wrote nothing, so it answers False; `cut_over` is the state.
    other = OutputRows(projection.pool)
    assert other.try_cut_over() is False
    assert other.cut_over() is True
    assert rows.cut_over_at() == at
    with projection.pool.connection() as conn:
        marks = conn.execute("SELECT byte_run_id FROM output_cutover").fetchall()
    assert [mark["byte_run_id"] for mark in marks] == [first]
    # Once True, it stays True for the object, as `copied` does.
    with projection.pool.connection() as conn:
        conn.execute("DELETE FROM output_cutover")
    assert rows.cut_over() is True


def _listed(
    output_id: str,
    slug: str,
    *,
    age: timedelta,
    name: str | None = None,
    params: dict[str, Any] | None = None,
    source: OutputSource = "back_office",
) -> OutputRow:
    row = make_row(output_id, slug, age, name=name)
    return dataclasses.replace(
        row,
        meta=row.meta.model_copy(update={"source": source}),
        params=params or {},
        params_text=params_text(params or {}, {}),
    )


def test_query_on_outputs_matches_index_query(
    rows: OutputRows, projection: JobProjection, tmp_path: Path
) -> None:
    """The rows of `tests/test_output_index.py`'s query tests: `OutputRows.query` lists
    what `OutputIndex.query` lists, in the same order, with the same totals."""
    index = OutputIndex(projection.pool, DataPaths(root=tmp_path))
    listed = [
        _listed("1" * 32, "sign", age=timedelta(days=3), name="Door", params={"w": 30}),
        _listed("2" * 32, "sign", age=timedelta(hours=5), params={"label": "Ada"}),
        _listed("3" * 32, "tag", age=timedelta(minutes=5), name="Gift", params={"w": 10}),
        _listed("e" * 32, "sign", age=timedelta(hours=1)),
        _listed(
            "f" * 32, "sign", age=timedelta(hours=6), name="Storefront: Sign", source=STOREFRONT
        ),
    ]
    for row in listed:
        rows.insert(row)
        index.record(row.meta, row.params, {})
    # A row a phase 2 pod saved has no `params_text`: it is listed, and `q` passes it
    # by, as the index's empty text does.
    # Its index row was written before its row, so no `record` filled its text.
    old = _listed("4" * 32, "sign", age=timedelta(days=4), params={"w": 40})
    index.record(old.meta, {}, {})
    rows.insert(dataclasses.replace(old, params_text=None))
    got = rows.get("4" * 32)
    assert got is not None and got.params_text is None
    cases: list[tuple[OutputQuery, tuple[list[str], int]]] = [
        (OutputQuery(since=NOW - timedelta(days=1)), (["3" * 32, "e" * 32, "2" * 32], 3)),
        (OutputQuery(until=NOW - timedelta(days=1)), (["1" * 32, "4" * 32], 2)),
        (OutputQuery(slugs=["sign"]), (["e" * 32, "2" * 32, "1" * 32, "4" * 32], 4)),
        (OutputQuery(slugs=[]), ([], 0)),
        (OutputQuery(q="door"), (["1" * 32], 1)),
        (OutputQuery(q="ada"), (["2" * 32], 1)),
        (OutputQuery(q="w="), (["3" * 32, "1" * 32], 2)),
        (OutputQuery(q="%"), ([], 0)),
        (OutputQuery(limit=2), (["3" * 32, "e" * 32], 5)),
        (OutputQuery(limit=2, after="e" * 32), (["2" * 32, "1" * 32], 5)),
        (
            OutputQuery(include_storefront=True),
            (["3" * 32, "e" * 32, "2" * 32, "f" * 32, "1" * 32, "4" * 32], 6),
        ),
        (OutputQuery(ordered=True), ([], 0)),
        (OutputQuery(ordered=False, limit=1), (["3" * 32], 5)),
    ]
    for query, expected in cases:
        assert rows.query(query) == expected, query
        assert index.query(query) == expected, query
    # `after` must be an id the filters list: an unknown one, or the storefront's
    # output in the back office's list.
    for missing in ("9" * 32, "f" * 32):
        with pytest.raises(AfterNotListedError):
            rows.query(OutputQuery(after=missing))
        with pytest.raises(AfterNotListedError):
            index.query(OutputQuery(after=missing))


def test_index_record_fills_params_text(
    rows: OutputRows, projection: JobProjection, tmp_path: Path
) -> None:
    rows.insert(make_row("o1"))
    index = OutputIndex(projection.pool, DataPaths(root=tmp_path))
    index.record(make_row("o1").meta, {"width": 20, "depth": 5}, {"depth": 5})
    got = rows.get("o1")
    assert got is not None and got.params_text == "width=20"
    # An output with no row yet (no copy read it): the index row alone.
    index.record(make_row("o2").meta, {"width": 1}, {})
    assert rows.get("o2") is None
    assert index.query(OutputQuery(q="width=1")) == (["o2"], 1)


def test_claim_refused_while_claimed_or_saved(rows: OutputRows) -> None:
    """Review Focus 4: one claim wins; a released claim can be taken again; an id that
    has a row cannot be claimed."""
    assert rows.claim("o1", "cube") is True
    assert rows.claim("o1", "cube") is False
    assert rows.claim("o1", "ball") is False
    rows.release_claim("o1")
    assert rows.claim("o1", "cube") is True
    rows.insert(make_row("o2"))
    assert rows.claim("o2", "cube") is False
    rows.release_claim("missing")


def test_claim_refused_while_a_save_commits(
    rows: OutputRows, projection: JobProjection, pg_conninfo: str
) -> None:
    """A save that holds the claim of an id inserts its row and deletes its claim, and
    then commits (1-2 s on prod). A claim of that id in this window waits on the deleted
    claim row; after the commit it is refused, and it leaves no claim row."""
    assert rows.claim("o1", "cube") is True
    answers: list[bool] = []
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as save:
        with save.transaction():
            output_rows._insert(save, projection.pool, make_row("o1"))
            claimer = threading.Thread(target=lambda: answers.append(rows.claim("o1", "ball")))
            claimer.start()
            deadline = time.monotonic() + 10
            with projection.pool.connection() as conn:
                while not conn.execute(
                    "SELECT 1 FROM pg_locks WHERE NOT granted AND locktype = 'transactionid'"
                ).fetchone():
                    assert time.monotonic() < deadline, "the claim never waited on the save"
                    time.sleep(0.02)
        claimer.join(timeout=10)
    assert answers == [False]
    with projection.pool.connection() as conn:
        left = conn.execute("SELECT count(*) AS n FROM output_claims").fetchone()
    assert left is not None and left["n"] == 0
    assert rows.get("o1") is not None


def test_prune_claims_drops_only_old_claims(rows: OutputRows, projection: JobProjection) -> None:
    assert rows.claim("old", "cube") is True
    assert rows.claim("new", "cube") is True
    with projection.pool.connection() as conn:
        conn.execute(
            "UPDATE output_claims SET claimed_at = clock_timestamp() - interval '2 hours'"
            " WHERE output_id = 'old'"
        )
    assert rows.prune_claims(timedelta(hours=1)) == 1
    assert rows.claim("old", "cube") is True
    assert rows.claim("new", "cube") is False
    assert rows.prune_claims(timedelta(0)) == 2
