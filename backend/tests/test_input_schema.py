"""The closed JSON Schema subset that flows and pipeline templates share (#2483, #5701;
spec 2026-11-17 §4.1, §10.1). The flows' own cases are in tests/flows/test_flow_inputs.py."""

from __future__ import annotations

from typing import Any

from scadbuddy.core.input_schema import schema_problems, value_problems

HOUSE: dict[str, Any] = {
    "type": "object",
    "title": "House",
    "properties": {
        "cols": {"type": "integer", "minimum": 1, "maximum": 4},
        "name": {"type": "string", "maxLength": 20, "pattern": "^[a-z]+$"},
    },
    "required": ["cols"],
    "additionalProperties": False,
}


def _top(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


def test_flows_take_no_object_input() -> None:
    assert any("type must be one of" in p for p in schema_problems(_top(house=HOUSE)))


def test_one_level_of_object_passes_when_allowed() -> None:
    assert schema_problems(_top(house=HOUSE), objects=True) == []


def test_two_levels_of_object_are_refused() -> None:
    inner = {**HOUSE, "properties": {"roof": HOUSE}, "required": []}
    problems = schema_problems(_top(house=inner), objects=True)
    assert problems == ["house: roof: type must be one of string, integer, number, boolean, array"]


def test_an_object_must_refuse_extra_keys() -> None:
    open_object = {k: v for k, v in HOUSE.items() if k != "additionalProperties"}
    problems = schema_problems(_top(house=open_object), objects=True)
    assert problems == ["house: additionalProperties may only be false: extra keys are refused"]
    loose = schema_problems(_top(house={**HOUSE, "additionalProperties": True}), objects=True)
    assert loose == problems


def test_an_objects_required_names_its_own_properties() -> None:
    problems = schema_problems(_top(house={**HOUSE, "required": ["rows"]}), objects=True)
    assert problems == ["house: required names 'rows', which is not a property"]


def test_an_objects_property_follows_the_flows_rules() -> None:
    house = {**HOUSE, "properties": {"name": {"type": "string", "pattern": "^a$"}}, "required": []}
    problems = schema_problems(_top(house=house), objects=True)
    assert problems == ["house: name: a pattern needs a maxLength of at most 1024"]


def test_reserved_names_are_refused_at_the_top_level() -> None:
    schema = _top(params={"type": "string"}, v={"type": "integer"}, house=HOUSE)
    problems = schema_problems(schema, objects=True, reserved={"params", "v"})
    assert problems == ["params: 'params' is reserved", "v: 'v' is reserved"]


def test_value_problems_name_the_path_and_the_keyword() -> None:
    schema = {**_top(house=HOUSE), "required": ["house"]}
    assert value_problems(schema, {"house": {"cols": 2, "name": "abc"}}) == []
    found = value_problems(schema, {"house": {"cols": 9, "name": "ABC", "x": 1}})
    assert [(p.path, p.keyword) for p in found] == [
        ("house", "additionalProperties"),
        ("house.cols", "maximum"),
        ("house.name", "pattern"),
    ]
    assert [(p.path, p.keyword) for p in value_problems(schema, {})] == [("", "required")]


def test_an_object_takes_no_pydantic_attribute_name() -> None:
    """#6913 review: an object's own name is refused as a scalar's is (`json`, `copy` …)."""
    problems = schema_problems(_top(json=HOUSE), objects=True)
    assert problems == ["json: 'json' is reserved"]


def test_an_objects_required_may_not_name_a_boolean() -> None:
    """#6913 review: a boolean left out is false, so an object cannot require one."""
    house = {**HOUSE, "properties": {**HOUSE["properties"], "flag": {"type": "boolean"}}}
    problems = schema_problems(_top(house={**house, "required": ["flag"]}), objects=True)
    assert problems == ["house: required names 'flag', a boolean, which is never missing"]
