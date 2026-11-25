"""Required properties (#7703, spec 6.3b): a schema may require keys, and a row is complete
when each required key has a value and every defined key's value fits."""

from __future__ import annotations

import pytest

from scadbuddy.entities.properties import (
    BadPropertySchema,
    EntityPropertyStore,
    MissingRequiredProperties,
    PropertyPatch,
    check_schema,
    completeness,
)
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityStore
from scadbuddy.entities.required import check_row, completeness_of_kind
from scadbuddy.orders.models import Actor
from tests.conftest import PgPool

BY = Actor(kind="browser")


def _schema(*required: str, **props: dict[str, object]) -> dict[str, object]:
    return {"type": "object", "properties": props, "required": list(required)}


def test_required_must_name_defined_keys() -> None:
    with pytest.raises(BadPropertySchema, match="'x'"):
        check_schema(_schema("x"), allow_required=True)


def test_required_is_a_list_of_distinct_keys() -> None:
    with pytest.raises(BadPropertySchema):
        check_schema(_schema("a", "a", a={"type": "string"}), allow_required=True)


def test_required_refused_for_a_kind_that_does_not_check() -> None:
    with pytest.raises(BadPropertySchema, match="required"):
        check_schema(_schema("a", a={"type": "string"}))


def test_required_may_name_a_key_defined_elsewhere() -> None:
    check_schema(_schema("a"), allow_required=True, defined={"a"})


def test_completeness_no_value_rules() -> None:
    schema = _schema("a", "b", "c", "d", "e")
    found = completeness(schema, {"a": "", "b": "  ", "c": [], "d": False, "e": 0})
    assert found.missing == ("a", "b", "c")
    assert not found.complete


def test_completeness_reports_an_ill_fitting_stored_value() -> None:
    found = completeness(_schema(n={"type": "number"}), {"n": "x", "free": "y"})
    assert found.missing == ()
    assert [key for key, _ in found.invalid] == ["n"]


def test_a_schema_without_required_is_complete() -> None:
    assert completeness({"type": "object"}, {}).complete


@pytest.mark.requires_postgres
def test_set_schema_takes_required_only_for_a_kind_that_checks(pg_pool: PgPool) -> None:
    props = EntityPropertyStore(pg_pool, EntityStore(pg_pool))
    schema = _schema("mpn", mpn={"type": "string"})
    assert props.set_schema("spool_type", schema, BY)["required"] == ["mpn"]
    with pytest.raises(BadPropertySchema):
        props.set_schema("supplier", schema, BY)


def _type(pool: PgPool) -> EntityRef:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g)"
            " SELECT gen_random_uuid()::text, id, '#112233FF', 1000 FROM filament_materials"
            " WHERE name = 'PLA' RETURNING id"
        ).fetchone()
    assert row is not None
    return EntityRef("spool_type", str(row["id"]))


@pytest.mark.requires_postgres
def test_check_row_names_every_missing_key(pg_pool: PgPool) -> None:
    props = EntityPropertyStore(pg_pool, EntityStore(pg_pool))
    props.set_schema(
        "spool_type", _schema("a", "b", a={"type": "string"}, b={"type": "string"}), BY
    )
    ref = _type(pg_pool)
    with pg_pool.connection() as conn, pytest.raises(MissingRequiredProperties) as caught:
        check_row(conn, ref)
    assert caught.value.missing == ("a", "b")
    props.apply(ref, PropertyPatch(set={"a": "1"}), BY)
    props.apply(ref, PropertyPatch(set={"b": "2"}), BY)
    with pg_pool.connection() as conn:
        check_row(conn, ref)


@pytest.mark.requires_postgres
def test_a_patch_may_not_remove_or_empty_a_required_value(pg_pool: PgPool) -> None:
    props = EntityPropertyStore(pg_pool, EntityStore(pg_pool))
    props.set_schema("spool_type", _schema("a", a={"type": "string"}), BY)
    ref = _type(pg_pool)
    props.apply(ref, PropertyPatch(set={"b": "x"}), BY)  # a missing key refuses nothing
    props.apply(ref, PropertyPatch(set={"a": "1"}), BY)
    for gone in (None, "  "):
        with pytest.raises(MissingRequiredProperties) as caught:
            props.apply(ref, PropertyPatch(set={"a": gone}), BY)
        assert caught.value.missing == ("a",)
    assert [p.value for p in props.list(ref) if p.key == "a"] == ["1"]


@pytest.mark.requires_postgres
def test_completeness_of_kind_batches(pg_pool: PgPool) -> None:
    props = EntityPropertyStore(pg_pool, EntityStore(pg_pool))
    props.set_schema("spool_type", _schema("a", a={"type": "string"}), BY)
    done, bare = _type(pg_pool), _type(pg_pool)
    props.apply(done, PropertyPatch(set={"a": "1"}), BY)
    with pg_pool.connection() as conn:
        found = completeness_of_kind(conn, "spool_type", [done.ref, bare.ref])
    assert found[done.ref].complete
    assert found[bare.ref].missing == ("a",)
