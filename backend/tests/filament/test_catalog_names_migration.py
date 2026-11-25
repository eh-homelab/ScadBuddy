"""The catalog names migration (#6706, R-6706-3, -9, -10): it fills each
`filament_<kind>_names` table from the rows that exist, merges rows whose keys collide
(within one material, for subtypes) into the survivor (lowest `sort_order`, then earliest
`created_at`, then lowest id), moves every reference to the survivor, and deletes the
losers. It is idempotent and safe on an empty database."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from scadbuddy.render.pg_store import MIGRATIONS, migrate

pytestmark = [pytest.mark.requires_postgres, pytest.mark.pg_empty]

Conn = psycopg.Connection[tuple[Any, ...]]

SLUG = "_filament_catalog_names"


def _index() -> int:
    return next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith(SLUG))


def _before(conn: Conn) -> None:
    migrate(conn, MIGRATIONS[: _index()])


def _one(conn: Conn, sql: str, *args: object) -> Any:
    row = conn.execute(sql, args).fetchone()
    assert row is not None
    return row[0]


def _effect(conn: Conn, name: str, sort: int, created: str, *, archived: bool = False) -> Any:
    return _one(
        conn,
        "INSERT INTO filament_effects (name, sort_order, created_at, archived_at)"
        " VALUES (%s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id",
        name,
        sort,
        created,
        archived,
    )


def _names(conn: Conn) -> list[tuple[str, str, str, bool]]:
    return [
        (row[0], row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT n.key, e.name, n.name, n.canonical FROM filament_effect_names n"
            " JOIN filament_effects e ON e.id = n.effect_id ORDER BY n.key"
        ).fetchall()
    ]


def test_colliding_effects_merge_into_the_survivor_with_every_reference(
    pg_conninfo: str,
) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        # A table no migration knows: the merge must find its foreign key in the catalog.
        conn.execute(
            "CREATE TABLE side_effect_refs (effect_id uuid REFERENCES filament_effects (id))"
        )
        survivor = _one(conn, "SELECT id FROM filament_effects WHERE name = 'Dual Color'")
        loser = _effect(conn, "dual-color", 1000, "2026-11-15T00:00:00Z")
        # The same sort order: the earlier one survives. The later one is archived.
        tri_loser = _effect(conn, "TRI color", 130, "2099-01-01T00:00:00Z", archived=True)
        tri = _one(conn, "SELECT id FROM filament_effects WHERE name = 'Tri Color'")
        material = _one(conn, "SELECT id FROM filament_materials WHERE name = 'PLA'")
        kind = _one(
            conn,
            "INSERT INTO filament_spool_types (code, material_id, effect_id, rgba, label_weight_g)"
            " VALUES ('T1', %s, %s, '#FFFFFFFF', 1000) RETURNING id",
            material,
            loser,
        )
        spool = _one(
            conn,
            "INSERT INTO filament_spools (material_id, effect_id) VALUES (%s, %s) RETURNING id",
            material,
            tri_loser,
        )
        colour = _one(
            conn,
            "INSERT INTO filament_colours (name, rgba, effect_id) VALUES ('C', '#000000FF', %s)"
            " RETURNING id",
            loser,
        )
        conn.execute("INSERT INTO side_effect_refs VALUES (%s)", (loser,))

        migrate(conn)

        left = {
            row[0]
            for row in conn.execute(
                "SELECT id FROM filament_effects WHERE id IN (%s, %s)", (loser, tri_loser)
            ).fetchall()
        }
        assert left == set()
        assert _one(conn, "SELECT effect_id FROM filament_spool_types WHERE id = %s", kind) == (
            survivor
        )
        assert _one(conn, "SELECT effect_id FROM filament_spools WHERE id = %s", spool) == tri
        assert _one(conn, "SELECT effect_id FROM filament_colours WHERE id = %s", colour) == (
            survivor
        )
        assert _one(conn, "SELECT effect_id FROM side_effect_refs") == survivor
        names = _names(conn)
        assert ("DUALCOLOR", "Dual Color", "Dual Color", True) in names
        assert ("TRICOLOR", "Tri Color", "Tri Color", True) in names
        # Every effect has exactly one canonical row, keyed by its own name.
        assert _one(conn, "SELECT count(*) FROM filament_effects") == _one(
            conn, "SELECT count(*) FROM filament_effect_names WHERE canonical"
        )


def test_the_lowest_id_breaks_a_full_tie_and_a_live_loser_restores_the_survivor(
    pg_conninfo: str,
) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        at = "2026-11-01T00:00:00Z"
        first = _effect(conn, "Neon Glow", 5, at, archived=True)
        second = _effect(conn, "neon-glow", 5, at)
        survivor, loser = sorted([first, second])
        migrate(conn)
        assert _one(conn, "SELECT count(*) FROM filament_effects WHERE id = %s", loser) == 0
        assert _one(conn, "SELECT effect_id FROM filament_effect_names WHERE key = 'NEONGLOW'") == (
            survivor
        )
        # One of the merged effects was live: the survivor is live.
        assert (
            _one(conn, "SELECT archived_at IS NULL FROM filament_effects WHERE id = %s", survivor)
            is True
        )


def test_the_migration_runs_twice_and_on_an_empty_database(pg_conninfo: str) -> None:
    sql = MIGRATIONS[_index()].sql.encode()
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        for table in ("subtypes", "materials", "brands", "categories", "effects"):
            conn.execute(f"DELETE FROM filament_{table}")
        conn.execute(sql)
        conn.execute(sql)
        assert _names(conn) == []
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("DROP TRIGGER filament_effects_claim_name ON filament_effects")
        conn.execute("DELETE FROM filament_effect_names")
        conn.execute(
            "INSERT INTO filament_effects (name, sort_order) VALUES ('Wood', 0), ('wood!', 1)"
        )
        conn.execute(sql)
        once = _names(conn)
        conn.execute(sql)
        assert _names(conn) == once == [("WOOD", "Wood", "Wood", True)]


def _row(conn: Conn, table: str, name: str, sort: int, created: str, **more: object) -> Any:
    columns = ", ".join(["name", "sort_order", "created_at", *more])
    marks = ", ".join(["%s"] * (3 + len(more)))
    return _one(
        conn,
        f"INSERT INTO {table} ({columns}) VALUES ({marks}) RETURNING id",
        name,
        sort,
        created,
        *more.values(),
    )


def test_materials_merge_first_and_their_subtypes_then_merge_in_one_scope(
    pg_conninfo: str,
) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        pla = _one(conn, "SELECT id FROM filament_materials WHERE name = 'PLA'")
        petg = _one(conn, "SELECT id FROM filament_materials WHERE name = 'PETG'")
        # A later spelling of PLA: its key is PLA's.
        loser = _row(conn, "filament_materials", "P.L.A", 900, "2026-11-15T00:00:00Z")
        kept = _row(conn, "filament_subtypes", "Glossy", 1, "2026-11-01T00:00:00Z", material_id=pla)
        moved = _row(
            conn, "filament_subtypes", "glossy", 2, "2026-11-02T00:00:00Z", material_id=loser
        )
        # Same key, another material: not merged.
        apart = _row(
            conn, "filament_subtypes", "GLOSSY", 0, "2026-11-03T00:00:00Z", material_id=petg
        )
        brand = _row(conn, "filament_brands", "Bambu Lab", 0, "2026-11-01T00:00:00Z")
        brand_loser = _row(conn, "filament_brands", "bambu-lab", 0, "2026-11-02T00:00:00Z")
        category = _row(conn, "filament_categories", "Eng", 0, "2026-11-01T00:00:00Z")
        category_loser = _row(conn, "filament_categories", "E.N.G", 0, "2026-11-02T00:00:00Z")
        spool = _one(
            conn,
            "INSERT INTO filament_spools (material_id, subtype_id, brand_id, category_id)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            loser,
            moved,
            brand_loser,
            category_loser,
        )

        migrate(conn)

        got = conn.execute(
            "SELECT material_id, subtype_id, brand_id, category_id FROM filament_spools"
            " WHERE id = %s",
            (spool,),
        ).fetchone()
        assert got == (pla, kept, brand, category)
        gone = conn.execute(
            "SELECT count(*) FROM (SELECT id FROM filament_materials UNION ALL"
            " SELECT id FROM filament_subtypes UNION ALL SELECT id FROM filament_brands"
            " UNION ALL SELECT id FROM filament_categories) a WHERE id IN (%s, %s, %s, %s)",
            (loser, moved, brand_loser, category_loser),
        ).fetchone()
        assert gone == (0,)
        scopes = conn.execute(
            "SELECT material_scope, subtype_id FROM filament_subtype_names WHERE key = 'GLOSSY'"
            " ORDER BY subtype_id = %s",
            (kept,),
        ).fetchall()
        assert sorted(scopes) == sorted([(pla, kept), (petg, apart)])
        nil = _one(
            conn,
            "SELECT material_scope FROM filament_subtype_names n JOIN filament_subtypes s"
            " ON s.id = n.subtype_id WHERE s.name = 'Basic'",
        )
        assert str(nil) == "00000000-0000-0000-0000-000000000000"
        for table, owner in (
            ("filament_material_names", "material_id"),
            ("filament_brand_names", "brand_id"),
            ("filament_category_names", "category_id"),
        ):
            catalog = table.replace("_names", "s").replace("categorys", "categories")
            assert _one(conn, f"SELECT count(*) FROM {catalog}") == _one(
                conn, f"SELECT count(DISTINCT {owner}) FROM {table} WHERE canonical"
            )


def test_a_writer_that_skips_the_store_still_claims_its_key(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn)
        made = _one(conn, "INSERT INTO filament_brands (name) VALUES ('Acme') RETURNING id")
        assert _one(conn, "SELECT brand_id FROM filament_brand_names WHERE key = 'ACME'") == made
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO filament_brands (name) VALUES ('a-c-m-e')")


def test_a_plus_variant_survives_and_its_spellings_merge(pg_conninfo: str) -> None:
    """R-6706-17: `PLA+` stays apart from `PLA` and `Silk+` from `Silk`; `PLA Plus` is
    `PLA+`, so it merges into it."""
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        pla = _one(conn, "SELECT id FROM filament_materials WHERE name = 'PLA'")
        silk = _one(conn, "SELECT id FROM filament_subtypes WHERE name = 'Silk'")
        plus = _row(conn, "filament_materials", "PLA+", 1, "2026-11-01T00:00:00Z")
        spelled = _row(conn, "filament_materials", "PLA Plus", 2, "2026-11-02T00:00:00Z")
        silk_plus = _row(conn, "filament_subtypes", "Silk+", 31, "2026-11-01T00:00:00Z")
        spool = _one(
            conn,
            "INSERT INTO filament_spools (material_id) VALUES (%s) RETURNING id",
            spelled,
        )

        migrate(conn)

        keys: dict[str, Any] = dict(
            conn.execute(
                "SELECT key, material_id FROM filament_material_names WHERE canonical"
            ).fetchall()
        )
        assert (keys["PLA"], keys["PLAPLUS"]) == (pla, plus)
        assert _one(conn, "SELECT count(*) FROM filament_materials WHERE id = %s", spelled) == 0
        assert _one(conn, "SELECT material_id FROM filament_spools WHERE id = %s", spool) == plus
        subtypes: dict[str, Any] = dict(
            conn.execute(
                "SELECT key, subtype_id FROM filament_subtype_names WHERE canonical"
                " AND material_scope = '00000000-0000-0000-0000-000000000000'"
            ).fetchall()
        )
        assert (subtypes["SILK"], subtypes["SILKPLUS"]) == (silk, silk_plus)
