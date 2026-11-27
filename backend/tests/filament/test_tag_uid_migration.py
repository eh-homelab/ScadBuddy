"""#8042 (#7905): the migration that merges a tag stored under a phone's 4-byte UID into
its AMS twin `<uid>00000100`, and re-keys a short UID that has no twin."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from scadbuddy.render.pg_store import MIGRATIONS, MIGRATIONS_DIR, migrate

pytestmark = [pytest.mark.requires_postgres, pytest.mark.pg_empty]

MIGRATION = MIGRATIONS_DIR / "20261126T1817Z_filament_tag_uid_canonical.sql"
SHORT = "7E7482BB"
LONG = "7E7482BB00000100"
TRAY = "D3B5D4BCA6044374A9B52E9C2DC9AC6A"

Conn = psycopg.Connection[dict[str, Any]]


def _before(conn: Conn) -> None:
    before = next(i for i, m in enumerate(MIGRATIONS) if m.id == MIGRATION.stem)
    migrate(conn, MIGRATIONS[:before])


def _apply(conn: Conn) -> None:
    conn.execute(MIGRATION.read_text())


def _one(conn: Conn, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return next(iter(row.values()))


def _spool(conn: Conn, name: str = "PLA") -> UUID:
    material = _material(conn, name)
    spool: UUID = _one(
        conn, "INSERT INTO filament_spools (material_id) VALUES (%s) RETURNING id", (material,)
    )
    return spool


def _material(conn: Conn, name: str) -> UUID:
    found = conn.execute("SELECT id FROM filament_materials WHERE name = %s", (name,)).fetchone()
    if found is not None:
        existing: UUID = found["id"]
        return existing
    made: UUID = _one(
        conn, "INSERT INTO filament_materials (name) VALUES (%s) RETURNING id", (name,)
    )
    return made


def _tag(
    conn: Conn,
    uid: str,
    *,
    kind: str = "other",
    tray: str | None = None,
    seen: str | None = None,
    source: str | None = None,
) -> UUID:
    tag: UUID = _one(
        conn,
        "INSERT INTO filament_tags (kind, tag_uid, tray_uuid, last_seen_at, last_seen_source,"
        " last_seen_records) VALUES (%s, %s, %s, %s::timestamptz, %s, %s::jsonb) RETURNING id",
        (kind, uid, tray, seen, source, None if seen is None else json.dumps([])),
    )
    return tag


def _bind(conn: Conn, tag: UUID, spool: UUID, *, open_: bool = True) -> UUID:
    binding: UUID = _one(
        conn,
        "INSERT INTO filament_tag_bindings (tag_id, spool_id, unbound_at)"
        " VALUES (%s, %s, CASE WHEN %s THEN NULL ELSE now() END) RETURNING id",
        (tag, spool, open_),
    )
    return binding


def _tags(conn: Conn) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM filament_tags ORDER BY id").fetchall()]


def _merge_case(conn: Conn) -> tuple[UUID, UUID]:
    """The prod pair (`becfa1bf…` from the AMS, `a167982e…` from a phone, read 2026-11-26):
    the long row unbound and never seen, the short one seen by a phone and queued."""
    long_id = _tag(conn, LONG, kind="bambu_mifare", tray=TRAY)
    short_id = _tag(conn, SHORT, seen="2026-11-25T18:00:00Z", source="phone")
    conn.execute("INSERT INTO filament_bb_tag_pending (tag_id) VALUES (%s)", (short_id,))
    return long_id, short_id


def test_a_short_row_with_a_long_twin_is_merged(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before(conn)
        long_id, short_id = _merge_case(conn)
        _apply(conn)
        rows = _tags(conn)
        assert [r["id"] for r in rows] == [long_id]
        (merged,) = rows
        assert merged["tag_uid"] == LONG and merged["kind"] == "bambu_mifare"
        assert merged["tray_uuid"] == TRAY
        assert merged["last_seen_source"] == "phone"
        assert merged["last_seen_at"].isoformat() == "2026-11-25T18:00:00+00:00"
        assert merged["last_seen_records"] == []
        pending = [r["tag_id"] for r in conn.execute("SELECT tag_id FROM filament_bb_tag_pending")]
        assert pending == [long_id] and short_id not in pending


def test_a_short_row_with_backups_is_left_alone(pg_conninfo: str) -> None:
    """Backups refuse UPDATE and DELETE (`20261124T0258Z_filament_tag_backups.sql`), so a
    short row that has one cannot be merged."""
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before(conn)
        _tag(conn, LONG, kind="bambu_mifare", tray=TRAY)
        short_id = _tag(conn, SHORT)
        conn.execute(
            "INSERT INTO filament_tag_backups (tag_id, uid, records, reason, actor)"
            " VALUES (%s, %s, '[]'::jsonb, 'spool_write', 'ann')",
            (short_id, SHORT),
        )
        before = _tags(conn)
        _apply(conn)
        assert _tags(conn) == before


def test_a_bound_short_row_moves_its_binding_to_the_long_row(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before(conn)
        spool = _spool(conn)
        long_id = _tag(conn, LONG, kind="bambu_mifare", tray=TRAY)
        short_id = _tag(conn, SHORT)
        binding = _bind(conn, short_id, spool)
        _apply(conn)
        found = conn.execute(
            "SELECT tag_id, spool_id FROM filament_tag_bindings"
            " WHERE id = %s AND unbound_at IS NULL",
            (binding,),
        ).fetchone()
        assert found == {"tag_id": long_id, "spool_id": spool}
        assert [r["id"] for r in _tags(conn)] == [long_id]


def test_the_ledger_follows_the_merged_tag(pg_conninfo: str) -> None:
    """`filament/spools.py` writes `data.tag_id` on `tag_bound` and `tag_unbound`, with no
    foreign key, so the merge rewrites them to the twin's id."""
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before(conn)
        spool = _spool(conn)
        long_id = _tag(conn, LONG, kind="bambu_mifare", tray=TRAY)
        short_id = _tag(conn, SHORT)
        binding = _bind(conn, short_id, spool, open_=False)
        for kind in ("tag_bound", "tag_unbound"):
            conn.execute(
                "INSERT INTO filament_spool_events (spool_id, kind, data)"
                " VALUES (%s, %s, jsonb_build_object('tag_id', %s::text))",
                (spool, kind, short_id),
            )
        count = _one(
            conn, "SELECT count(*) FROM filament_spool_events WHERE spool_id = %s", (spool,)
        )
        _apply(conn)
        ids = [
            r["tag_id"]
            for r in conn.execute(
                "SELECT data->>'tag_id' AS tag_id FROM filament_spool_events"
                " WHERE spool_id = %s AND kind IN ('tag_bound', 'tag_unbound')",
                (spool,),
            )
        ]
        assert ids == [str(long_id), str(long_id)]
        assert (
            _one(conn, "SELECT tag_id FROM filament_tag_bindings WHERE id = %s", (binding,))
            == long_id
        )
        assert (
            _one(conn, "SELECT count(*) FROM filament_spool_events WHERE spool_id = %s", (spool,))
            == count
        )


def test_two_open_bindings_are_left_alone(pg_conninfo: str) -> None:
    """Merging would break the one-open-binding-per-tag index or drop a binding."""
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before(conn)
        long_id = _tag(conn, LONG, kind="bambu_mifare", tray=TRAY)
        short_id = _tag(conn, SHORT)
        _bind(conn, long_id, _spool(conn))
        _bind(conn, short_id, _spool(conn, "PETG"))
        before = _tags(conn)
        _apply(conn)
        assert _tags(conn) == before
        open_tags = {
            r["tag_id"]
            for r in conn.execute(
                "SELECT tag_id FROM filament_tag_bindings WHERE unbound_at IS NULL"
            )
        }
        assert open_tags == {long_id, short_id}


def test_a_short_row_with_no_twin_is_rekeyed(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before(conn)
        tag = _tag(conn, "DC999BE3")
        _apply(conn)
        assert [(r["id"], r["tag_uid"]) for r in _tags(conn)] == [(tag, "DC999BE300000100")]


def test_running_it_again_changes_nothing(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        _before(conn)
        _merge_case(conn)
        _apply(conn)
        once = _tags(conn)
        _apply(conn)
        assert _tags(conn) == once
