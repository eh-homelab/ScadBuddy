"""`OutputRows` (#7255, outputs-in-Postgres spec 2026-11-23 §4.2): the saved outputs as
rows. The tables come first; the row store's tests follow."""

from __future__ import annotations

import dataclasses
import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from scadbuddy.library import output_rows
from scadbuddy.library.output_query import AfterNotListedError, OutputQuery, params_text
from scadbuddy.library.output_rows import (
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
from tests.support.row_outputs import (
    mark_cut_over,
    name_blobs,
    record_byte_run,
    record_copy_run,
)

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


def _clock(rows: OutputRows) -> datetime:
    """The database's clock, as the retired copy's mark was."""
    with rows._pool.connection() as conn:
        found = conn.execute("SELECT clock_timestamp() AS now").fetchone()
    assert found is not None
    now: datetime = found["now"]
    return now


def _stamp(rows: OutputRows, output_id: str) -> None:
    """Mark the row as the retired copy wrote it (#7255): with a stamp of the files it read."""
    with rows._pool.connection() as conn:
        conn.execute("UPDATE outputs SET files_stamp = 'stamp' WHERE output_id = %s", (output_id,))


def test_every_live_writer_moves_copied_at(rows: OutputRows) -> None:
    """A row a live writer touched moves ``copied_at`` (kept by plan D11)."""
    rows.insert(make_row("o1"))
    writes: list[Callable[[], object]] = [
        lambda: rows.update_meta("o1", make_row("o1", name="New").meta),
        lambda: rows.set_backfill("o1", {"job_id": "j"}),
        lambda: rows.attach_backfill("o1", {"plate_key": "p"}, []),
    ]
    for write in writes:
        mark = _clock(rows)
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


def test_a_save_under_another_slug_does_not_replace_the_copys_row(rows: OutputRows) -> None:
    """Ids can repeat across models (`orders/quote.py` makes them with uuid5): a save
    replaces the copy's row of its own folder only, never one of another model."""
    rows.insert(make_row("o1", "cube"))
    _stamp(rows, "o1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        rows.insert(make_row("o1", "ball"))
    assert rows.slug_of("o1") == "cube"
    rows.insert(make_row("o1", "cube", name="saved"))
    got = rows.get("o1")
    assert got is not None and got.meta.name == "saved" and got.files_stamp is None


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


def test_last_run_names_a_finished_run(rows: OutputRows) -> None:
    assert rows.last_run() is None
    record_copy_run(
        rows,
        copied=2,
        refreshed=1,
        dropped=0,
        not_copied=["x"],
        damaged=[{"id": "y", "why": "bad"}],
    )
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
    rows.insert(make_row("o1"))
    _stamp(rows, "o1")
    copied = _blobs("o1", "a.stl")
    assert name_blobs(rows, "o1", copied)
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


def test_byte_run_round_trip(rows: OutputRows) -> None:
    """`GET /outputs/copy-status` reads the last byte run release A recorded."""
    assert rows.last_byte_run() is None
    first = record_byte_run(
        rows,
        finished_at=None,
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
    assert last is not None and last["id"] == first
    assert last["finished_at"] is None and last["incomplete"] is True
    assert last["refused"] == "store full"
    assert (last["copied"], last["verified"], last["skipped"]) == (1, 2, 3)
    assert (last["needed_bytes"], last["needed_count"]) == (400, 5)
    assert last["failed"] == [{"id": "x", "error": "boom"}]
    assert last["damaged"] == [{"id": "y", "role": "model", "error": "missing"}]
    second = record_byte_run(rows)
    last = rows.last_byte_run()
    assert last is not None and last["id"] == second and last["finished_at"] is not None


def test_pending_bytes_counts_rows_without_bytes_at(rows: OutputRows) -> None:
    rows.insert(make_row("files"))
    rows.insert(dataclasses.replace(make_row("saved"), blobs=_blobs("saved")))
    rows.insert(make_row("copied"))
    assert rows.pending_bytes() == 2
    name_blobs(rows, "copied", _blobs("copied"))
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


def test_cut_over_at_reads_the_mark(rows: OutputRows) -> None:
    """Release A wrote the mark (#7257); release B only reads it, for copy-status."""
    assert rows.cut_over_at() is None
    mark_cut_over(rows)
    assert rows.cut_over_at() is not None


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


def test_query_on_outputs(rows: OutputRows) -> None:
    """`OutputRows.query`: the filters, the order, the pages and the totals (the cases
    of the old ``output_index`` query tests, #4777, #7257)."""
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
    # A row with no text (the copy wrote it): it is listed, and `q` passes it by.
    old = _listed("4" * 32, "sign", age=timedelta(days=4), params={"w": 40})
    rows.insert(dataclasses.replace(old, params_text=""))
    got = rows.get("4" * 32)
    assert got is not None and got.params_text == ""
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
    # `after` must be an id the filters list: an unknown one, or the storefront's
    # output in the back office's list.
    for missing in ("9" * 32, "f" * 32):
        with pytest.raises(AfterNotListedError):
            rows.query(OutputQuery(after=missing))


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
            # Only a backend that the save's own connection blocks: another xdist worker's
            # wait must not end this one early (A3 review M3).
            with projection.pool.connection() as conn:
                while not conn.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                    (save.info.backend_pid,),
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
