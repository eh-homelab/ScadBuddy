"""`OutputRows` (#7255, outputs-in-Postgres spec 2026-11-23 §4.2): the saved outputs as
rows. The tables come first; the row store's tests follow."""

from __future__ import annotations

from collections.abc import Iterator
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


def test_upsert_sets_copied_at_and_ids_copied_before_finds_the_rest(
    rows: OutputRows,
) -> None:
    rows.upsert(make_row("a"))
    # The mark is the database's clock, as the copy's is (`run_started_at`).
    mark = rows.run_started_at(rows.start_run())
    rows.upsert(make_row("b"))
    rows.upsert(make_row("a", name="again"))
    assert rows.ids_copied_before(mark) == set()
    rows.upsert(make_row("c"))
    later = rows.run_started_at(rows.start_run())
    assert rows.ids_copied_before(later) == {"a", "b", "c"}
    got = rows.get("a")
    assert got is not None
    assert got.meta.name == "again"


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
