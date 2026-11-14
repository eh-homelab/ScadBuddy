"""The copy of ScadBuddy's native spools into the filament tables (#6261)."""

from __future__ import annotations

import psycopg
import pytest

from scadbuddy.render.pg_store import MIGRATIONS, migrate

pg = pytest.mark.requires_postgres


def _index(slug: str) -> int:
    return next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith(f"_{slug}"))


@pg
def test_native_spools_uses_and_tags_are_copied(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index("filament_from_native")])
        live = conn.execute(
            "INSERT INTO native_spools (material, colour, label_weight_g, remaining_g,"
            " tray_uuid, note, created_at)"
            " VALUES ('pla', '#aabbcc', 1000, 600, 'TRAY1', 'hi', '2026-11-01T10:00:00Z')"
            " RETURNING id"
        ).fetchone()
        old = conn.execute(
            "INSERT INTO native_spools (material, colour, label_weight_g, remaining_g,"
            " tag_uid, archived_at)"
            " VALUES ('Carbon Fibre', '#000000', 500, 0, 'UID2', '2026-11-05T00:00:00Z')"
            " RETURNING id"
        ).fetchone()
        linked = conn.execute(
            "INSERT INTO native_spools (material, remaining_g) VALUES ('PETG', 10) RETURNING id"
        ).fetchone()
        physical = conn.execute(
            "INSERT INTO physical_spools (tray_uuid, material) VALUES ('TRAY3', 'PETG')"
            " RETURNING id"
        ).fetchone()
        orphan = conn.execute(
            "INSERT INTO physical_spools (tag_uid, material) VALUES ('UID4', 'PLA') RETURNING id"
        ).fetchone()
        assert live and old and linked and physical and orphan
        conn.execute(
            "INSERT INTO spool_record_links (provider, record_id, physical_spool_id, linked_by)"
            " VALUES ('native', %s, %s, 'manual')",
            (str(linked[0]), physical[0]),
        )
        conn.execute(
            "INSERT INTO native_spool_uses (run_id, subtask_name, slot_id, spool_id, grams,"
            " settled_at, applied) VALUES ('r1', 's', 0, %s, 12.5, now(), true)",
            (live[0],),
        )
        conn.execute(
            "INSERT INTO native_spool_uses (run_id, subtask_name, slot_id, spool_id, grams)"
            " VALUES ('r2', 's', 1, %s, 3)",
            (live[0],),
        )
        migrate(conn)

        spools = conn.execute(
            "SELECT s.id, s.rgba, s.grams_left, s.label_weight_g, s.note, s.type_id,"
            " s.archived_at, m.name FROM filament_spools s"
            " JOIN filament_materials m ON m.id = s.material_id"
        ).fetchall()
        assert len(spools) == 3
        by_id = {s[0]: s for s in spools}
        a = by_id[live[0]]
        assert a[1:6] == ("#AABBCCFF", 600, 1000, "hi", None)
        assert a[7] == "PLA"
        assert by_id[old[0]][6] is not None
        assert by_id[old[0]][7] == "Carbon Fibre"
        assert physical[0] in by_id and linked[0] not in by_id
        assert conn.execute(
            "SELECT count(*) FROM filament_materials WHERE upper(name) = 'PLA'"
        ).fetchone() == (1,)

        tags = dict(
            conn.execute("SELECT coalesce(tray_uuid, tag_uid), kind FROM filament_tags").fetchall()
        )
        assert tags == {
            "TRAY1": "bambu_mifare",
            "UID2": "other",
            "TRAY3": "bambu_mifare",
            "UID4": "other",
        }
        bound = conn.execute(
            "SELECT t.tray_uuid, b.spool_id FROM filament_tag_bindings b"
            " JOIN filament_tags t ON t.id = b.tag_id WHERE b.unbound_at IS NULL"
        ).fetchall()
        assert bound == [("TRAY1", live[0])]

        created = conn.execute(
            "SELECT spool_id, source, grams_after, at FROM filament_spool_events"
            " WHERE kind = 'created'"
        ).fetchall()
        assert len(created) == 3
        mine = next(c for c in created if c[0] == live[0])
        assert mine[1:3] == ("person", 600)
        assert mine[3].isoformat().startswith("2026-11-01T10:00:00")

        uses = conn.execute(
            "SELECT kind, source, delta_g, ref_kind, ref_id, data->>'basis'"
            " FROM filament_spool_events WHERE ref_kind = 'print_run' ORDER BY ref_id"
        ).fetchall()
        assert uses == [
            ("print_ended", "print_actual", -12.5, "print_run", "r1/s/0", "sliced"),
            ("print_started", "print_estimate", -3, "print_run", "r2/s/1", None),
        ]
