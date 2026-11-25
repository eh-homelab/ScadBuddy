"""`TemplateRows` (#7317, model records spec §5.1): each template's derived row and the
copy runs' ledger. ``model.json`` in git stays the record; a row can be rebuilt from it."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from scadbuddy.library.template_rows import TemplateRow, TemplateRows
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _row(template_id: str, **changes: object) -> TemplateRow:
    row = TemplateRow(
        template_id=template_id,
        origin="builtin" if template_id.startswith("builtin:") else "mine",
        meta={"name": template_id, "tags": ["a"]},
        meta_error=None,
        meta_text=None,
        name=template_id,
        version="abc123",
        updated_at=NOW,
        has_readme=False,
        inputs_version=0,
        colour_params=None,
        files={},
        cover=None,
        files_stamp="stamp-" + template_id,
        refreshed_head="head1",
    )
    return dataclasses.replace(row, **changes)  # type: ignore[arg-type]


def _refreshed_at(pg_pool: PgPool, template_id: str) -> datetime:
    with pg_pool.connection() as conn:
        found = conn.execute(
            "SELECT refreshed_at FROM templates WHERE template_id = %s", (template_id,)
        ).fetchone()
    assert found is not None
    value: datetime = found["refreshed_at"]
    return value


def test_put_then_get_round_trips_every_column(pg_pool: PgPool) -> None:
    rows = TemplateRows(pg_pool)
    row = _row(
        "demo",
        meta={"name": "Demo", "nested": {"x": 1}},
        meta_error="name: too long",
        meta_text="{not json",
        name="Demo",
        version=None,
        has_readme=True,
        inputs_version=3,
        colour_params=["body", "text"],
        files={"thumbnail.png": 12, "media/a.png": 34},
        cover={"path": "models/demo/media/a.png", "content_type": "image/png", "sha256": "ff"},
        refreshed_head=None,
    )
    rows.put([row], [])
    assert rows.get("demo") == row
    assert rows.get("absent") is None


def test_put_replaces_a_row_and_moves_refreshed_at(pg_pool: PgPool) -> None:
    rows = TemplateRows(pg_pool)
    rows.put([_row("demo")], [])
    first = _refreshed_at(pg_pool, "demo")
    changed = _row("demo", name="Renamed", version="def456", colour_params=["x"])
    rows.put([changed], [])
    assert rows.get("demo") == changed
    assert _refreshed_at(pg_pool, "demo") > first


def test_put_drops_the_gone_ids(pg_pool: PgPool) -> None:
    rows = TemplateRows(pg_pool)
    rows.put([_row("a"), _row("b")], [])
    rows.put([_row("c")], ["a", "never-there"])
    assert rows.ids() == ["b", "c"]


def test_all_and_ids_are_mine_then_builtins_in_c_order(pg_pool: PgPool) -> None:
    rows = TemplateRows(pg_pool)
    ids = ["builtin:a", "b", "B", "_x", "é"]
    rows.put([_row(i) for i in ids], [])
    expected = [*sorted(["b", "B", "_x", "é"]), "builtin:a"]
    assert expected == ["B", "_x", "b", "é", "builtin:a"]
    assert rows.ids() == expected
    assert [row.template_id for row in rows.all()] == expected


def test_stamps_answer_stamp_and_version(pg_pool: PgPool) -> None:
    rows = TemplateRows(pg_pool)
    rows.put([_row("a"), _row("b", version=None, files_stamp="s2")], [])
    assert rows.stamps() == {"a": ("stamp-a", "abc123"), "b": ("s2", None)}


def test_copied_false_until_a_run_finishes_then_stays_true(pg_pool: PgPool) -> None:
    rows = TemplateRows(pg_pool)
    assert rows.copied() is False
    run = rows.start_run()
    assert rows.copied() is False
    rows.finish_run(run, added=1, refreshed=2, dropped=3, unreadable=[])
    assert rows.copied() is True
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM template_copy_runs")
    assert rows.copied() is True  # cached for the object's life
    assert TemplateRows(pg_pool).copied() is False


def test_incomplete_run_does_not_count_as_copied(pg_pool: PgPool) -> None:
    rows = TemplateRows(pg_pool)
    run = rows.start_run()
    rows.end_incomplete_run(
        run, added=1, refreshed=0, dropped=0, unreadable=[{"template_id": "x", "error": "e"}]
    )
    assert rows.copied() is False
    last = rows.last_run()
    assert last is not None
    assert last["incomplete"] is True
    assert last["finished_at"] is None
    assert last["added"] == 1
    assert last["unreadable"] == [{"template_id": "x", "error": "e"}]


def test_start_run_prunes_runs_older_than_30_days(pg_pool: PgPool) -> None:
    rows = TemplateRows(pg_pool)
    old = rows.start_run()
    recent = rows.start_run()
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE template_copy_runs SET started_at = clock_timestamp() - interval '31 days'"
            " WHERE id = %s",
            (old,),
        )
        conn.execute(
            "UPDATE template_copy_runs SET started_at = clock_timestamp() - interval '29 days'"
            " WHERE id = %s",
            (recent,),
        )
    newest = rows.start_run()
    with pg_pool.connection() as conn:
        left = [r["id"] for r in conn.execute("SELECT id FROM template_copy_runs ORDER BY id")]
    assert left == [recent, newest]


def test_last_run_answers_the_newest(pg_pool: PgPool) -> None:
    rows = TemplateRows(pg_pool)
    assert rows.last_run() is None
    first = rows.start_run()
    rows.finish_run(first, added=5, refreshed=0, dropped=0, unreadable=[])
    second = rows.start_run()
    rows.finish_run(
        second,
        added=0,
        refreshed=1,
        dropped=2,
        unreadable=[{"template_id": "bad", "error": "not JSON"}],
    )
    last = rows.last_run()
    assert last is not None
    assert last["id"] == second
    assert last["refreshed"] == 1
    assert last["dropped"] == 2
    assert last["incomplete"] is False
    assert last["finished_at"] is not None
    assert last["unreadable"] == [{"template_id": "bad", "error": "not JSON"}]
