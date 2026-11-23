"""`OutputRows` (#7255, outputs-in-Postgres spec 2026-11-23 §4.2): the saved outputs as
rows. The tables come first; the row store's tests follow."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from scadbuddy.render.projection import JobProjection

pytestmark = pytest.mark.requires_postgres


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
