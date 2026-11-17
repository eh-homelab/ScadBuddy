"""The copy of ScadBuddy's native spools into the filament tables (#6261)."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from scadbuddy.filament.ledger import refresh_derived
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
            " tray_uuid, note, created_at, brand, name, preset)"
            " VALUES ('pla', '#aabbcc', 1000, 600, 'TRAY1', 'hi', '2026-11-01T10:00:00Z',"
            " 'Bambu Lab', 'Jade', 'GFA00')"
            " RETURNING id"
        ).fetchone()
        conn.execute(
            "INSERT INTO native_spools (material, remaining_g, brand)"
            " VALUES ('PLA', 1, 'BAMBU LAB')"
        )
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
            "INSERT INTO native_spool_uses (run_id, subtask_name, slot_id, spool_id, grams,"
            " settled_at, applied) VALUES ('r3', 's', 2, %s, 7, now(), false)",
            (live[0],),
        )
        conn.execute(
            "INSERT INTO native_spool_uses (run_id, subtask_name, slot_id, spool_id, grams)"
            " VALUES ('r2', 's', 1, %s, 3)",
            (live[0],),
        )
        migrate(conn, MIGRATIONS[: _index("filament_presets_per_nozzle")])

        spools = conn.execute(
            "SELECT s.id, s.rgba, s.grams_left, s.label_weight_g, s.note, s.type_id,"
            " s.archived_at, m.name FROM filament_spools s"
            " JOIN filament_materials m ON m.id = s.material_id"
        ).fetchall()
        assert len(spools) == 4
        by_id = {s[0]: s for s in spools}
        a = by_id[live[0]]
        # 600 native, less the 3 g of the print still running: what the ledger gives.
        assert a[1:6] == ("#AABBCCFF", 597, 1000, "hi", None)
        assert a[7] == "PLA"
        assert by_id[old[0]][6] is not None
        assert by_id[old[0]][7] == "Carbon Fibre"
        assert physical[0] in by_id and linked[0] not in by_id
        assert conn.execute(
            "SELECT count(*) FROM filament_materials WHERE upper(name) = 'PLA'"
        ).fetchone() == (1,)

        copied = conn.execute(
            "SELECT b.name, s.colour_name FROM filament_spools s"
            " LEFT JOIN filament_brands b ON b.id = s.brand_id WHERE s.id = %s",
            (live[0],),
        ).fetchone()
        assert copied is not None and copied[0].upper() == "BAMBU LAB" and copied[1] == "Jade"
        assert conn.execute(
            "SELECT count(*) FROM filament_brands WHERE upper(name) = 'BAMBU LAB'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT spool_id, printer_model, nozzle_diameter, slicer_filament"
            " FROM filament_spool_presets"
        ).fetchall() == [(live[0], "*", "*", "GFA00")]

        tags: dict[str, str] = dict(
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
        # The archived spool keeps its tag as a closed binding, as an archive leaves one.
        closed = conn.execute(
            "SELECT t.tag_uid, b.spool_id, b.unbound_at, b.reason FROM filament_tag_bindings b"
            " JOIN filament_tags t ON t.id = b.tag_id WHERE b.unbound_at IS NOT NULL"
        ).fetchall()
        assert [(c[0], c[1], c[2].isoformat()[:10], c[3]) for c in closed] == [
            ("UID2", old[0], "2026-11-05", "archived")
        ]

        created = conn.execute(
            "SELECT spool_id, source, grams_after, at FROM filament_spool_events"
            " WHERE kind = 'created'"
        ).fetchall()
        assert len(created) == 4
        mine = next(c for c in created if c[0] == live[0])
        # 600 left after the applied 12.5 g: the reading is the weight before it.
        assert mine[1:3] == ("person", 612.5)
        assert mine[3].isoformat().startswith("2026-11-01T10:00:00")

        uses = conn.execute(
            "SELECT kind, source, delta_g, ref_kind, ref_id, data->>'basis'"
            " FROM filament_spool_events WHERE ref_kind = 'print_run' ORDER BY ref_id"
        ).fetchall()
        assert uses == [
            ("print_ended", "print_actual", -12.5, "print_run", "r1/s/0", "sliced"),
            ("print_started", "print_estimate", -3, "print_run", "r2/s/1", None),
            ("print_ended", "print_actual", 0, "print_run", "r3/s/2", "not_applied"),
        ]

        # A first refresh from the ledger does not move the copied value.
        conn.commit()
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as rows:
        refresh_derived(rows, live[0])
        left = rows.execute(
            "SELECT grams_left FROM filament_spools WHERE id = %s", (live[0],)
        ).fetchone()
        assert left == {"grams_left": 597}


@pg
def test_a_linked_native_record_keeps_its_cache_and_link_under_its_new_id(
    pg_conninfo: str,
) -> None:
    """A native record linked to a physical spool becomes that spool: its cached copy and
    its (person's) link take the physical id, the id the native provider now reports."""
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index("filament_from_native")])
        native = conn.execute(
            "INSERT INTO native_spools (material, remaining_g) VALUES ('PLA', 10) RETURNING id"
        ).fetchone()
        alone = conn.execute(
            "INSERT INTO native_spools (material, remaining_g) VALUES ('PLA', 20) RETURNING id"
        ).fetchone()
        physical = conn.execute(
            "INSERT INTO physical_spools DEFAULT VALUES RETURNING id"
        ).fetchone()
        assert native and alone and physical
        conn.execute(
            "INSERT INTO spool_record_links (provider, record_id, physical_spool_id, linked_by)"
            " VALUES ('native', %s, %s, 'manual')",
            (str(native[0]), physical[0]),
        )
        for record in (native[0], alone[0]):
            conn.execute(
                "INSERT INTO spool_provider_records (provider, record_id, snapshot, refreshed_at)"
                " VALUES ('native', %s, jsonb_build_object('record_id', %s::text), now())",
                (str(record), str(record)),
            )
        migrate(conn)

        links = conn.execute(
            "SELECT record_id, physical_spool_id, linked_by FROM spool_record_links"
        ).fetchall()
        assert links == [(str(physical[0]), physical[0], "manual")]
        cached = conn.execute(
            "SELECT record_id, snapshot->>'record_id' FROM spool_provider_records"
            " ORDER BY record_id"
        ).fetchall()
        assert sorted(cached) == sorted([(str(physical[0]),) * 2, (str(alone[0]),) * 2])
        ids = {row[0] for row in conn.execute("SELECT id FROM filament_spools").fetchall()}
        assert ids == {physical[0], alone[0]}
