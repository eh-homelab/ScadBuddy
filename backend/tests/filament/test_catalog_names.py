"""Catalog names and aliases (#6706): each of the materials, subtypes, brands, categories
and effects catalogs has one key space for its names and aliases, held unique by its
`filament_<kind>_names` table, in the same transaction as the write."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from scadbuddy.filament.catalog import (
    CATALOGS,
    AliasKeyEmptyError,
    CatalogKeyClashError,
    CatalogKeyEmptyError,
    CatalogStore,
    catalog_key,
    find_by_key,
    key_sql,
    lookup_key,
    resolve_or_add,
)
from scadbuddy.filament.models import (
    BrandCreate,
    BrandPatch,
    Effect,
    EffectCreate,
    EffectPatch,
    MaterialCreate,
    SubtypeCreate,
    SubtypePatch,
)
from tests.conftest import PgPool

pg = pytest.mark.requires_postgres

ASCII = [
    "Dual Color",
    "dual-color",
    "DUAL_COLOR",
    "  tri  color ",
    "Glow-in-the-Dark 2",
    "a.b,c;d:e/f\\g|h",
    "!!!",
    "",
    "PLA+",
    "Silk++",
    "+",
    "x\u2013y\u00a0z",  # an en dash and a no-break space are not letters
]
#: Letters outside ASCII: SQL keeps them only under a ctype that classifies them.
WIDE = ["Café crème", "Ünïcödé", "Grün-Glanz"]


def test_the_key_drops_every_non_alphanumeric_and_upper_cases() -> None:
    assert catalog_key("dual-color") == catalog_key("Dual Color") == "DUALCOLOR"
    assert catalog_key("  tri  color ") == "TRICOLOR"
    assert catalog_key("!!!") == ""


def test_a_plus_is_a_word_of_the_key() -> None:
    """R-6706-17: `PLA+` is not `PLA`, and `Silk+` is not `Silk`; `PLA+` is `PLA Plus`."""
    assert catalog_key("PLA+") == catalog_key("PLA Plus") == "PLAPLUS"
    assert catalog_key("PLA+") != catalog_key("PLA")
    assert catalog_key("Silk+") != catalog_key("Silk")


@pg
def test_a_plus_variant_is_its_own_row(pg_pool: PgPool) -> None:
    store: Any = CatalogStore(pg_pool)
    plus = store.create("materials", MaterialCreate(name="PLA+"))
    silk = store.create("subtypes", SubtypeCreate(name="Silk+"))
    with pytest.raises(CatalogKeyClashError) as clash:
        store.create("materials", MaterialCreate(name="pla plus"))
    assert clash.value.holder_id == plus.id
    with pg_pool.connection() as conn:
        pla = find_by_key(conn, "materials", "PLA")
        assert pla is not None and pla != plus.id
        assert find_by_key(conn, "materials", "PLA Plus") == plus.id
        assert find_by_key(conn, "subtypes", "silk+") == silk.id
        assert find_by_key(conn, "subtypes", "silk") not in (None, silk.id)


@pg
def test_a_name_with_no_key_is_refused_and_never_found(pg_pool: PgPool) -> None:
    store: Any = CatalogStore(pg_pool)
    with pytest.raises(CatalogKeyEmptyError):
        store.create("brands", BrandCreate(name="!!!"))
    made = store.create("brands", BrandCreate(name="Acme"))
    with pytest.raises(CatalogKeyEmptyError):
        store.update("brands", made.id, BrandPatch(name="---"))
    with pg_pool.connection() as conn:
        assert find_by_key(conn, "brands", "???") is None


@pg
def test_the_python_key_matches_the_sql_key(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        ctype = conn.execute("SELECT datctype FROM pg_database WHERE datname = current_database()")
        row = ctype.fetchone()
        assert row is not None
        names = ASCII + ([] if row[0] in ("C", "POSIX") else WIDE)
        for name in names:
            got = conn.execute(f"SELECT {key_sql('%s')}", (name,)).fetchone()
            assert got is not None
            assert got[0] == catalog_key(name), name


def _effect(store: CatalogStore, name: str) -> Effect:
    made = store.create("effects", EffectCreate(name=name))
    assert isinstance(made, Effect)
    return made


@pg
def test_a_new_effect_reads_back_with_no_aliases_and_holds_its_key(pg_pool: PgPool) -> None:
    store: Any = CatalogStore(pg_pool)
    made = _effect(store, "Neon")
    assert made.aliases == []
    with pytest.raises(CatalogKeyClashError) as clash:
        store.create("effects", EffectCreate(name="n-e-o-n"))
    assert (clash.value.holder_id, clash.value.holder_name) == (made.id, "Neon")


@pg
def test_aliases_are_set_listed_and_unique_across_every_name(pg_pool: PgPool) -> None:
    store: Any = CatalogStore(pg_pool)
    dual = next(r for r in store.list("effects") if r.name == "Dual Color")
    tri = next(r for r in store.list("effects") if r.name == "Tri Color")

    got = store.set_aliases("effects", dual.id, ["Bicolor", " Two Tone "])
    assert got is not None and got.aliases == ["Bicolor", "Two Tone"]
    assert store.get("effects", dual.id).aliases == ["Bicolor", "Two Tone"]

    # Another effect's name, another effect's alias, and its own name all clash.
    for taken, holder in (("sparkle", "Sparkle"), ("two-tone", "Dual Color")):
        with pytest.raises(CatalogKeyClashError) as clash:
            store.set_aliases("effects", tri.id, [taken])
        assert clash.value.holder_name == holder
    with pytest.raises(CatalogKeyClashError) as own:
        store.set_aliases("effects", tri.id, ["tri-color"])
    assert own.value.holder_id == tri.id
    with pytest.raises(CatalogKeyClashError):
        store.set_aliases("effects", tri.id, ["Trio", "TRIO"])
    with pytest.raises(AliasKeyEmptyError):
        store.set_aliases("effects", tri.id, ["--"])
    # A refusal changes nothing.
    assert store.get("effects", tri.id).aliases == []

    # A new name takes the same key space.
    with pytest.raises(CatalogKeyClashError) as by_name:
        store.create("effects", EffectCreate(name="BI-COLOR"))
    assert by_name.value.holder_id == dual.id

    # Replacing the list frees the old aliases.
    store.set_aliases("effects", dual.id, [])
    assert _effect(store, "Bicolor").aliases == []
    assert store.set_aliases("effects", UUID(int=1), ["x"]) is None


@pg
def test_a_rename_rekeys_the_canonical_row(pg_pool: PgPool) -> None:
    store: Any = CatalogStore(pg_pool)
    made = _effect(store, "Shimmer")
    store.set_aliases("effects", made.id, ["Shine", "Gleam"])
    with pytest.raises(CatalogKeyClashError) as clash:
        store.update("effects", made.id, EffectPatch(name="Wood"))
    assert clash.value.holder_name == "Wood"
    renamed = store.update("effects", made.id, EffectPatch(name="Glimmer"))
    assert renamed is not None and renamed.name == "Glimmer"
    # The old name is free; the new one is taken.
    assert _effect(store, "shimmer").name == "shimmer"
    with pytest.raises(CatalogKeyClashError):
        _effect(store, "GLIMMER")
    # A rename to one of its own aliases takes that alias's key.
    again = store.update("effects", made.id, EffectPatch(name="shine"))
    assert again is not None and again.aliases == ["Gleam"]


@pg
def test_archiving_keeps_the_keys_reserved(pg_pool: PgPool) -> None:
    store: Any = CatalogStore(pg_pool)
    made = _effect(store, "Pearl")
    store.set_aliases("effects", made.id, ["Pearlescent"])
    store.archive("effects", made.id, True)
    for name in ("pearl", "PEARLESCENT"):
        with pytest.raises(CatalogKeyClashError) as clash:
            _effect(store, name)
        assert clash.value.holder_id == made.id


@pg
def test_a_lookup_resolves_any_spelling_and_adds_a_missing_key(pg_pool: PgPool) -> None:
    store: Any = CatalogStore(pg_pool)
    dual = next(r for r in store.list("effects") if r.name == "Dual Color")
    store.set_aliases("effects", dual.id, ["Bicolor"])
    with pg_pool.connection() as conn:
        assert find_by_key(conn, "effects", "DUAL-COLOR") == dual.id
        assert find_by_key(conn, "effects", "bi color") == dual.id
        assert find_by_key(conn, "effects", "Holo") is None
        made = resolve_or_add(conn, "effects", "holo!", sort_order=1000)
        assert resolve_or_add(conn, "effects", "HOLO", sort_order=1000) == made
    holo = store.get("effects", made)
    assert holo is not None and holo.name == "holo!"


@pg
@pytest.mark.parametrize(
    ("kind", "name", "spelling"),
    [
        ("materials", "PLA", "p.l.a"),
        ("brands", "Bambu Lab", "bambu-lab"),
        ("categories", "Engineering", "ENGINEERING!"),
    ],
)
def test_every_keyed_catalog_has_one_key_space(
    pg_pool: PgPool, kind: str, name: str, spelling: str
) -> None:
    store: Any = CatalogStore(pg_pool)
    spec = CATALOGS[kind]
    if name not in [r.name for r in store.list(kind)]:
        store.create(kind, spec.create.model_validate({"name": name}))
    row = next(r for r in store.list(kind) if r.name == name)
    with pytest.raises(CatalogKeyClashError) as clash:
        store.create(kind, spec.create.model_validate({"name": spelling}))
    assert (clash.value.holder_id, clash.value.holder_name) == (row.id, name)
    got = store.set_aliases(kind, row.id, ["Alias One"])
    assert got is not None and got.aliases == ["Alias One"]
    with pg_pool.connection() as conn:
        assert find_by_key(conn, kind, "alias-one") == row.id
        assert find_by_key(conn, kind, spelling) == row.id


@pg
def test_a_subtype_key_is_unique_within_its_material(pg_pool: PgPool) -> None:
    store: Any = CatalogStore(pg_pool)
    pla = next(r for r in store.list("materials") if r.name == "PLA")
    petg = next(r for r in store.list("materials") if r.name == "PETG")
    mine = store.create("subtypes", SubtypeCreate(name="Glossy", material_id=pla.id))
    # Another material, and no material, are other scopes.
    other = store.create("subtypes", SubtypeCreate(name="glossy", material_id=petg.id))
    anyone = store.create("subtypes", SubtypeCreate(name="GLOSSY"))
    with pytest.raises(CatalogKeyClashError) as clash:
        store.create("subtypes", SubtypeCreate(name="Gloss-y", material_id=pla.id))
    assert clash.value.holder_id == mine.id
    store.set_aliases("subtypes", mine.id, ["Shiny"])
    with pg_pool.connection() as conn:
        assert find_by_key(conn, "subtypes", "glossy", material=pla.id) == mine.id
        assert find_by_key(conn, "subtypes", "shiny", material=pla.id) == mine.id
        # A material with none of its own falls back to the subtype for every material.
        abs_id = next(r for r in store.list("materials") if r.name == "ABS").id
        assert find_by_key(conn, "subtypes", "glossy", material=abs_id) == anyone.id
        assert find_by_key(conn, "subtypes", "shiny", material=abs_id) is None
    # Moving a subtype to a material where its key (or an alias's) is taken is a clash.
    with pytest.raises(CatalogKeyClashError) as moved:
        store.update("subtypes", mine.id, SubtypePatch(material_id=petg.id))
    assert moved.value.holder_id == other.id
    store.update("subtypes", other.id, SubtypePatch(name="Glossy PETG"))
    # Now only an alias meets a key there: the clash still names the holder (R-6706-16).
    sheen = store.create("subtypes", SubtypeCreate(name="Shiny", material_id=petg.id))
    with pytest.raises(CatalogKeyClashError) as by_alias:
        store.update("subtypes", mine.id, SubtypePatch(material_id=petg.id))
    assert (by_alias.value.holder_id, by_alias.value.holder_name) == (sheen.id, "Shiny")
    store.update("subtypes", sheen.id, SubtypePatch(name="Sheen"))
    store.update("subtypes", mine.id, SubtypePatch(material_id=petg.id))
    with pg_pool.connection() as conn:
        assert find_by_key(conn, "subtypes", "shiny", material=petg.id) == mine.id
        assert find_by_key(conn, "subtypes", "shiny", material=pla.id) is None


@pg
def test_two_aliases_with_no_key_are_refused_as_empty_not_as_a_clash(pg_pool: PgPool) -> None:
    """R-6706-21: an alias with no key is refused before it is written."""
    store: Any = CatalogStore(pg_pool)
    made = _effect(store, "Matte Sheen")
    with pytest.raises(AliasKeyEmptyError):
        store.set_aliases("effects", made.id, ["--", "??"])
    assert store.get("effects", made.id).aliases == []


def _keyless(conn: psycopg.Connection[Any], table: str, trigger: str, name: str) -> UUID:
    """A row whose name has no key, as the migration leaves one (R-6706-14): the name
    trigger is off for its insert."""
    with conn.transaction():
        conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
        row = conn.execute(f"INSERT INTO {table} (name) VALUES (%s) RETURNING id", (name,))
        made = row.fetchone()
        conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
    assert made is not None
    return UUID(str(made["id"]))


@pg
def test_a_keyless_row_takes_a_patch_that_keeps_its_name(pg_pool: PgPool) -> None:
    """R-6706-20: a row with no key is refused only when a write gives it a new name."""
    store: Any = CatalogStore(pg_pool)
    pla = next(r for r in store.list("materials") if r.name == "PLA")
    with pg_pool.connection() as conn:
        brand = _keyless(conn, "filament_brands", "filament_brands_claim_name", "!!!")
        subtype = _keyless(conn, "filament_subtypes", "filament_subtypes_claim_name", "***")
    got = store.update("brands", brand, BrandPatch(name="!!!", sort_order=5))
    assert got is not None and (got.name, got.sort_order) == ("!!!", 5)
    moved = store.update("subtypes", subtype, SubtypePatch(name="***", material_id=pla.id))
    assert moved is not None and moved.material_id == pla.id
    with pytest.raises(CatalogKeyEmptyError):
        store.update("brands", brand, BrandPatch(name="???"))
    renamed = store.update("brands", brand, BrandPatch(name="Acme Two"))
    assert renamed is not None and renamed.name == "Acme Two"


@pg
def test_a_keyless_subtype_moved_onto_a_taken_alias_key_is_a_clash(pg_pool: PgPool) -> None:
    """R-6706-24: a keyless subtype's aliases move with it, and a taken key there is the
    409 a normal move gives, naming the holder."""
    store: Any = CatalogStore(pg_pool)
    pla = next(r for r in store.list("materials") if r.name == "PLA")
    with pg_pool.connection() as conn:
        subtype = _keyless(conn, "filament_subtypes", "filament_subtypes_claim_name", "***")
    store.set_aliases("subtypes", subtype, ["Lustre"])
    holder = store.create("subtypes", SubtypeCreate(name="Lustre", material_id=pla.id))
    with pytest.raises(CatalogKeyClashError) as clash:
        store.update("subtypes", subtype, SubtypePatch(material_id=pla.id))
    assert (clash.value.holder_id, clash.value.holder_name) == (holder.id, "Lustre")
    assert store.get("subtypes", subtype).material_id is None


@pg
def test_one_lookup_tells_a_keyless_name_from_an_unknown_key(pg_pool: PgPool) -> None:
    """R-6706-25: `lookup_key` answers both in one query."""
    store: Any = CatalogStore(pg_pool)
    made = store.create("brands", BrandCreate(name="Lookup Co"))
    with pg_pool.connection() as conn:
        assert lookup_key(conn, "brands", "lookup-co") == (True, made.id)
        assert lookup_key(conn, "brands", "Nobody Co") == (True, None)
        assert lookup_key(conn, "brands", "???") == (False, None)
        pla = find_by_key(conn, "materials", "PLA")
        keyed, basic = lookup_key(conn, "subtypes", "basic", material=pla)
        assert keyed and basic == find_by_key(conn, "subtypes", "Basic", material=pla)
        assert basic is not None
