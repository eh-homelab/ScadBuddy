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
    check_schema,
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


_REDOS = {"type": "string", "pattern": "^(a+)+$"}


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "additionalProperties": False},
        {"type": "object", "required": ["mpn"]},
        {"type": "object", "propertyNames": {"maxLength": 3}},
        {"type": "object", "patternProperties": {"^x": {}}},
        {"type": "array"},
        {"type": "object", "properties": {"a": {"type": "nope"}}},
        {"type": "object", "unevaluatedProperties": False},
        {"type": "object", "maxProperties": 0},
        {"type": "object", "dependentRequired": {"mpn": ["gtin"]}},
        {"type": "object", "allOf": [{"required": ["mpn"]}]},
        {"type": "object", "not": {"properties": {"mpn": True}, "required": ["mpn"]}},
        {"type": "object", "properties": {"mpn": {"not": {}}}},
        {"type": "object", "properties": {"mpn": _REDOS}},
        {
            "type": "object",
            "$defs": {"s": {"type": "string"}},
            "properties": {"mpn": {"$ref": "#/$defs/s"}},
        },
        {"type": "object", "properties": {"mpn": {"$ref": "#/$defs/s"}}},
        {"type": "object", "properties": {"mpn": {"type": "string", "format": "regex"}}},
        {"type": "object", "if": {"properties": {"mpn": {"type": "string"}}}},
        {"type": "object", "dependentSchemas": {"mpn": {"required": ["gtin"]}}},
        {"type": "object", "properties": {"mpn": True}},
        {"type": "object", "properties": {"mpn": False}},
        {
            "type": "object",
            "properties": {"tags": {"type": "array", "items": {"type": "array"}}},
        },
        {
            "type": "object",
            "properties": {"tags": {"type": "array", "items": {"type": "string", "items": {}}}},
        },
        {"type": "object", "properties": {"tags": {"type": "array", "items": _REDOS}}},
        {"type": "object", "properties": {"meta": {"type": "object"}}},
    ],
    ids=[
        "additional",
        "required",
        "names",
        "patterns",
        "array",
        "invalid",
        "unevaluated",
        "max_properties",
        "dependent_required",
        "all_of",
        "not_top",
        "not_key",
        "pattern",
        "ref_with_defs",
        "ref",
        "format_regex",
        "if",
        "dependent_schemas",
        "true_subschema",
        "false_subschema",
        "nested_array_items",
        "nested_items_keyword",
        "items_pattern",
        "object_type",
    ],
)
def test_set_schema_refuses_a_keyword_outside_the_subset(
    props: EntityPropertyStore, schema: dict[str, Any]
) -> None:
    before = props.schema("spool_type")

    with pytest.raises(BadPropertySchema):
        props.set_schema("spool_type", schema, BY)

    assert props.schema("spool_type") == before


def _key(sub: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": {"k": sub}}


@pytest.mark.parametrize(
    "schema",
    [
        _key({"type": "array", "items": {"type": "number"}}),
        _key({"type": "array", "items": {"type": "boolean"}}),
        _key({"enum": [None, {"a": 1}, [1], 1e400]}),
        _key({"enum": []}),
        _key({"type": "array", "items": {"type": "string", "enum": [1, "x" * 501]}}),
        _key({"const": None}),
        _key({"const": {"a": 1}}),
        _key({"const": [1]}),
        _key({"type": "array", "items": {"type": "string", "const": 1}}),
        _key({"type": "number", "minimum": 2, "maximum": 1}),
        _key({"type": "number", "exclusiveMinimum": 1, "maximum": 1}),
        _key({"type": "number", "minimum": 1, "exclusiveMaximum": 1}),
        _key({"type": "number", "exclusiveMinimum": 1, "exclusiveMaximum": 1}),
        _key({"type": "string", "minLength": 3, "maxLength": 2}),
        _key({"type": "string", "minLength": 2001}),
        _key({"type": "array", "items": {"type": "string", "minLength": 501}}),
        _key({"type": "array", "minItems": 3, "maxItems": 2}),
        _key({"type": "array", "minItems": 51}),
        _key({"type": "number", "enum": ["a"]}),
        _key({"type": "string", "const": 3}),
        _key({"type": "number", "minimum": 5, "enum": [1, 2]}),
        {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"},
    ],
    ids=[
        "number_items",
        "boolean_items",
        "enum_no_value",
        "enum_empty",
        "items_enum_no_string",
        "const_null",
        "const_object",
        "const_number_list",
        "items_const_number",
        "min_over_max",
        "exclusive_min_at_max",
        "min_at_exclusive_max",
        "exclusive_min_at_exclusive_max",
        "min_length_over_max_length",
        "min_length_over_cap",
        "items_min_length_over_cap",
        "min_items_over_max_items",
        "min_items_over_cap",
        "enum_off_type",
        "const_off_type",
        "enum_out_of_bounds",
        "draft_07",
    ],
)
def test_set_schema_refuses_a_schema_that_leaves_a_key_no_value(
    props: EntityPropertyStore, schema: dict[str, Any]
) -> None:
    before = props.schema("spool_type")

    with pytest.raises(BadPropertySchema):
        props.set_schema("spool_type", schema, BY)

    assert props.schema("spool_type") == before


def test_bounds_that_leave_a_value_are_accepted(props: EntityPropertyStore) -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "number", "minimum": 1, "maximum": 1},
            "b": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
            "c": {"type": "string", "minLength": 2000, "maxLength": 2000},
            "d": {"type": "array", "minItems": 50, "items": {"type": "string", "minLength": 500}},
            "e": {"enum": [None, {"a": 1}, "x"], "const": "x"},
            "f": {"type": "array", "items": {"type": "string", "enum": [1, "y"]}},
        },
    }

    assert props.set_schema("spool_type", schema, BY) == schema


def test_a_refusal_names_the_keyword_and_where_it_is(props: EntityPropertyStore) -> None:
    with pytest.raises(BadPropertySchema) as top:
        props.set_schema("spool_type", {"type": "object", "allOf": [{}]}, BY)
    assert top.value.reason == "keyword 'allOf' is not allowed at the top level"

    with pytest.raises(BadPropertySchema) as key:
        props.set_schema("spool_type", {"type": "object", "properties": {"mpn": _REDOS}}, BY)
    assert key.value.reason == "keyword 'pattern' is not allowed in property 'mpn'"


def test_every_allowed_keyword_is_accepted(props: EntityPropertyStore) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": "c",
        "title": "Spool type",
        "description": "d",
        "examples": [{"mpn": "x"}],
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "grade": {
                "type": ["integer", "null"],
                "title": "Grade",
                "description": "d",
                "$comment": "c",
                "default": 1,
                "examples": [1, 2],
                "deprecated": False,
                "readOnly": False,
                "writeOnly": False,
                "enum": [1, 2, 3, None],
                "minimum": 0,
                "maximum": 9,
                "exclusiveMinimum": -1,
                "exclusiveMaximum": 10,
                "multipleOf": 1,
            },
            "url": {"type": "string", "const": "x", "minLength": 1, "maxLength": 9},
            "site": {"type": "string", "format": "uri"},
            "tags": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "description": "a tag"},
                "minItems": 0,
                "maxItems": 5,
                "uniqueItems": True,
            },
            "ok": {"type": "boolean"},
        },
    }

    assert props.set_schema("spool_type", schema, BY) == schema


def test_a_schema_documents_keys_named_like_keywords(
    pg_pool: PgPool, props: EntityPropertyStore
) -> None:
    """Names under `properties` are not keywords, and `default`, `enum` and `examples`
    hold data, which is never walked (`const` must be a value a key takes)."""
    kind = _spool_type(pg_pool)
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "default": {"pattern": "x", "$ref": "y"}},
            "$ref": {"type": "string", "enum": ["a", "b", {"allOf": [], "not": {}}]},
            "format": {"type": "string", "examples": [{"patternProperties": 1}]},
            "patternProperties": {"type": "integer", "const": 3},
        },
    }

    assert props.set_schema("spool_type", schema, BY) == schema

    props.apply(kind, _patch({"pattern": "ok", "format": "regex"}), BY)
    with pytest.raises(BadPropertyValue) as caught:
        props.apply(kind, _patch({"pattern": 5}), BY)
    assert caught.value.key == "pattern"
    assert _values(props, kind)["pattern"] == "ok"


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
    for kind in ("spool", "spool_type"):
        check_schema(props.schema(kind))


def test_apply_on_a_missing_owner_raises_entity_not_found(
    props: EntityPropertyStore,
) -> None:
    with pytest.raises(EntityNotFound):
        props.apply(
            EntityRef("spool_type", "00000000-0000-0000-0000-000000000000"),
            _patch({"mpn": "x"}),
            BY,
        )


@pytest.mark.parametrize("value", ["a\u0000b", ["ok", "a\u0000b"]], ids=["string", "list_item"])
def test_a_nul_in_a_value_is_refused(
    pg_pool: PgPool, props: EntityPropertyStore, value: JsonValue
) -> None:
    kind = _spool_type(pg_pool)

    with pytest.raises(BadPropertyValue) as caught:
        props.apply(kind, _patch({"lot_number": value}), BY)

    assert caught.value.key == "lot_number"
    assert "U+0000" in caught.value.reason
    assert props.list(kind) == []


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "description": "a\u0000b"},
        {"type": "object", "properties": {"a\u0000b": {"type": "string"}}},
        {"type": "object", "properties": {"a": {"type": "string", "enum": ["x\u0000"]}}},
        {"type": "object", "properties": {"a": {"type": "string", "title": "\u0000"}}},
        {"type": "object", "properties": {"a": {"type": "string", "examples": [["\u0000"]]}}},
        {"type": "object", "properties": {"a": {"type": "string", "default": {"k\u0000": 1}}}},
    ],
    ids=["description", "property_name", "enum", "title", "examples", "default_key"],
)
def test_set_schema_refuses_a_nul_anywhere(
    props: EntityPropertyStore, schema: dict[str, Any]
) -> None:
    before = props.schema("spool_type")

    with pytest.raises(BadPropertySchema):
        props.set_schema("spool_type", schema, BY)

    assert props.schema("spool_type") == before
