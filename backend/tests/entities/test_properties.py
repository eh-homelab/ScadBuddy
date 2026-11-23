"""Key-value properties on any entity (#7026, spec 6.3a): one row per entity and key, a
value of four shapes, and kind schemas that type known keys and never refuse a key."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import JsonValue

from scadbuddy.entities.properties import (
    BadPropertySchema,
    BadPropertyValue,
    EntityPropertyStore,
    PropertyPatch,
)
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityNotFound, EntityStore
from tests.conftest import PgPool
from tests.entities.test_sweep import BY

pytestmark = pytest.mark.requires_postgres


def _spool_type(pool: PgPool, code: str = "T7026") -> EntityRef:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g)"
            " SELECT %s, id, '#FFFFFFFF', 1000 FROM filament_materials WHERE name = 'PLA'"
            " RETURNING id",
            (code,),
        ).fetchone()
    assert row is not None
    return EntityRef("spool_type", str(row["id"]))


@pytest.fixture
def props(pg_pool: PgPool) -> EntityPropertyStore:
    return EntityPropertyStore(pg_pool, EntityStore(pg_pool))


def _patch(values: dict[str, JsonValue]) -> PropertyPatch:
    return PropertyPatch(set=values)


def _values(props: EntityPropertyStore, ref: EntityRef) -> dict[str, Any]:
    return {p.key: p.value for p in props.list(ref)}


def test_set_list_and_remove(pg_pool: PgPool, props: EntityPropertyStore) -> None:
    kind = _spool_type(pg_pool)

    props.apply(kind, _patch({"td": 1.8, "gtin": ["0123", "0456"]}), BY)

    listed = props.list(kind)
    assert [(p.key, p.value, p.source) for p in listed] == [
        ("gtin", ["0123", "0456"], "manual"),
        ("td", 1.8, "manual"),
    ]
    assert listed[0].updated_by == BY

    assert [p.key for p in props.apply(kind, _patch({"td": None}), BY)] == ["gtin"]
    assert _values(props, kind) == {"gtin": ["0123", "0456"]}


def test_a_rename_is_one_patch_and_all_or_nothing(
    pg_pool: PgPool, props: EntityPropertyStore
) -> None:
    kind = _spool_type(pg_pool)
    props.apply(kind, _patch({"mpn": "A1"}), BY)

    props.apply(kind, _patch({"mpn": None, "part_no": "X"}), BY)
    assert _values(props, kind) == {"part_no": "X"}

    props.apply(kind, _patch({"mpn": "A1"}), BY)
    with pytest.raises(BadPropertyValue) as caught:
        props.apply(kind, _patch({"mpn": None, "empty_spool_weight_g": "heavy"}), BY)
    assert caught.value.key == "empty_spool_weight_g"
    assert _values(props, kind) == {"mpn": "A1", "part_no": "X"}


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1},
        ["1", 2],
        [["x"]],
        float("nan"),
        10**400,
        "x" * 2001,
        ["y"] * 51,
        ["z" * 501],
    ],
    ids=[
        "object",
        "mixed_list",
        "nested_list",
        "nan",
        "past_a_double",
        "long_string",
        "long_list",
        "long_item",
    ],
)
def test_values_outside_the_four_shapes_are_refused(
    pg_pool: PgPool, props: EntityPropertyStore, value: JsonValue
) -> None:
    kind = _spool_type(pg_pool)

    with pytest.raises(BadPropertyValue) as caught:
        props.apply(kind, _patch({"ok": "fine", "a": value}), BY)

    assert caught.value.key == "a"
    assert props.list(kind) == []


@pytest.mark.parametrize("key", ["GTIN", "1abc", "a" * 65, "a-b"])
def test_a_bad_key_is_refused(pg_pool: PgPool, props: EntityPropertyStore, key: str) -> None:
    kind = _spool_type(pg_pool)

    with pytest.raises(BadPropertyValue) as caught:
        props.apply(kind, _patch({key: "x"}), BY)

    assert caught.value.key == key
    assert props.list(kind) == []


def test_more_than_200_keys_is_refused(pg_pool: PgPool, props: EntityPropertyStore) -> None:
    kind = _spool_type(pg_pool)
    props.apply(kind, _patch({f"k{i}": i for i in range(200)}), BY)

    with pytest.raises(BadPropertyValue) as caught:
        props.apply(kind, _patch({"k0": None, "one": 1, "two": 2}), BY)

    assert caught.value.key == "*"
    assert len(props.list(kind)) == 200
    assert "k0" in _values(props, kind)


def test_unknown_keys_pass_a_schema_and_known_keys_are_typed(
    pg_pool: PgPool, props: EntityPropertyStore
) -> None:
    kind = _spool_type(pg_pool)

    props.apply(kind, _patch({"colour_family": "blue"}), BY)
    assert _values(props, kind) == {"colour_family": "blue"}

    with pytest.raises(BadPropertyValue) as caught:
        props.apply(kind, _patch({"gtin": "0123"}), BY)
    assert caught.value.key == "gtin"
    assert _values(props, kind) == {"colour_family": "blue"}


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "additionalProperties": False},
        {"type": "object", "required": ["mpn"]},
        {"type": "object", "propertyNames": {"maxLength": 3}},
        {"type": "object", "patternProperties": {"^x": {}}},
        {"type": "array"},
        {"type": "object", "properties": {"a": {"type": "nope"}}},
        {"type": "object", "properties": {"mpn": False}},
        {
            "type": "object",
            "$defs": {"s": {"type": "string"}},
            "properties": {"mpn": {"$ref": "#/$defs/s"}},
        },
        {
            "type": "object",
            "properties": {"gtin": {"type": "array", "items": {"$dynamicRef": "#x"}}},
        },
        {
            "type": "object",
            "properties": {"mpn": {"type": "string", "pattern": "^(a+)+$"}},
        },
        {
            "type": "object",
            "properties": {"meta": {"type": "object", "patternProperties": {"^(a+)+$": {}}}},
        },
        {"type": "object", "properties": {"mpn": {"type": "string", "format": "regex"}}},
    ],
    ids=[
        "additional",
        "required",
        "names",
        "patterns",
        "array",
        "invalid",
        "false_subschema",
        "ref",
        "dynamic_ref",
        "nested_pattern",
        "nested_pattern_properties",
        "format_regex",
    ],
)
def test_set_schema_refuses_a_restricting_schema(
    props: EntityPropertyStore, schema: dict[str, Any]
) -> None:
    before = props.schema("spool_type")

    with pytest.raises(BadPropertySchema):
        props.set_schema("spool_type", schema, BY)

    assert props.schema("spool_type") == before


def test_set_schema_stores_a_schema_that_types_a_new_key(
    pg_pool: PgPool, props: EntityPropertyStore
) -> None:
    schema = {
        "type": "object",
        "additionalProperties": True,
        "properties": {"grade": {"type": "integer"}},
    }

    assert props.set_schema("supplier", schema, BY) == schema
    assert props.schema("supplier") == schema
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT updated_by FROM entity_property_schemas WHERE kind = 'supplier'"
        ).fetchone()
    assert row is not None and row["updated_by"] == BY.model_dump(mode="json")


def test_a_kind_with_no_schema_row_has_an_empty_one(props: EntityPropertyStore) -> None:
    first = props.schema("printer")
    assert first == {"type": "object", "properties": {}}
    first["properties"]["x"] = {}
    assert props.schema("printer") == {"type": "object", "properties": {}}


def test_an_entity_past_the_limit_may_shrink_but_not_grow(
    pg_pool: PgPool, props: EntityPropertyStore
) -> None:
    """A printer merge keeps every key, so an entity can hold more than 200."""
    kind = _spool_type(pg_pool)
    props.apply(kind, _patch({"k0": 0}), BY)
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO entity_properties (entity_id, key, value, updated_by)"
            " SELECT e.id, 'k' || n, to_jsonb(n), '{\"kind\": \"browser\"}'"
            " FROM entities e, generate_series(1, 200) n"
            " WHERE e.kind = 'spool_type' AND e.ref = %s",
            (kind.ref,),
        )
    assert len(props.list(kind)) == 201

    with pytest.raises(BadPropertyValue) as caught:
        props.apply(kind, _patch({"extra": 1}), BY)
    assert caught.value.key == "*"
    assert len(props.list(kind)) == 201

    after = props.apply(kind, _patch({"k0": None, "k1": "changed"}), BY)
    assert len(after) == 200
    assert _values(props, kind)["k1"] == "changed"


def test_of_kind_and_keys_in_use(pg_pool: PgPool, props: EntityPropertyStore) -> None:
    one = _spool_type(pg_pool, "T1")
    two = _spool_type(pg_pool, "T2")
    props.apply(one, _patch({"gtin": ["1"], "mpn": "M1"}), BY)
    props.apply(two, _patch({"gtin": ["2"]}), BY)

    assert props.of_kind("spool_type") == {
        one.ref: {"gtin": ["1"], "mpn": "M1"},
        two.ref: {"gtin": ["2"]},
    }
    assert props.of_kind("spool") == {}
    assert props.keys_in_use("spool_type") == [("gtin", 2), ("mpn", 1)]


def test_the_seeded_schemas(props: EntityPropertyStore) -> None:
    assert list(props.schema("spool")["properties"]) == ["lot_number"]
    assert set(props.schema("spool_type")["properties"]) == {
        "mpn",
        "gtin",
        "asin",
        "manufacturer_url",
        "empty_spool_weight_g",
        "td",
    }


def test_apply_on_a_missing_owner_raises_entity_not_found(
    props: EntityPropertyStore,
) -> None:
    with pytest.raises(EntityNotFound):
        props.apply(
            EntityRef("spool_type", "00000000-0000-0000-0000-000000000000"),
            _patch({"mpn": "x"}),
            BY,
        )
