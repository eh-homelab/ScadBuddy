"""`OutputRows` (#7255, outputs-in-Postgres spec 2026-11-23 §4.2): the saved outputs as
rows. The tables come first; the row store's tests follow."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from scadbuddy.library.output_rows import OutputRow, OutputRows
from scadbuddy.library.outputs import OutputMeta
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
        assert row["bytes_at"] is None and row["bytes_damaged"] is None
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
