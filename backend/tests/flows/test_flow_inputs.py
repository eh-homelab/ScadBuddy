"""A flow's typed inputs (#2483): the schema's check, a run's values, and the script's
type check against them."""

import time
import warnings
from typing import Any

import pytest

from scadbuddy.flows.inputs import InputsError, inputs_model, resolve_inputs, schema_problems
from scadbuddy.flows.typecheck import check_script

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "The template to render"},
        "copies": {"type": "integer", "minimum": 1, "maximum": 12, "default": 1},
        "colour": {"type": "string", "enum": ["red", "blue"], "default": "red"},
        "scale": {"type": "number"},
        "dry_run": {"type": "boolean", "default": False},
        "plates": {"type": "array", "items": {"type": "integer"}, "default": [1]},
    },
    "required": ["slug"],
    "additionalProperties": False,
}


def _script(*body: str) -> str:
    lines = ["import asyncio", "async def main():", *(f"    {b}" for b in body)]
    return "\n".join([*lines, "asyncio.run(main())"])


def test_a_schema_in_the_subset_has_no_problems() -> None:
    assert schema_problems(SCHEMA) == []
    assert schema_problems({"type": "object"}) == []


@pytest.mark.parametrize(
    ("schema", "fragment"),
    [
        ([], "must be a JSON object"),
        ({"type": "array"}, "top level"),
        ({"type": "object", "properties": {"x": {"type": "nope"}}}, "not a JSON Schema"),
        ({"type": "object", "properties": {"x": {"type": "object"}}}, "type must be one of"),
        ({"type": "object", "properties": {"X-y": {"type": "string"}}}, "a name is"),
        ({"type": "object", "properties": {"class": {"type": "string"}}}, "a name is"),
        ({"type": "object", "properties": {"json": {"type": "string"}}}, "reserved"),
        ({"type": "object", "properties": {"x": {"type": "string", "oneOf": []}}}, "not a JSON"),
        ({"type": "object", "properties": {"x": {"type": "string", "format": "uri"}}}, "'format'"),
        ({"type": "object", "properties": {"x": {"type": "integer", "default": "a"}}}, "default"),
        ({"type": "object", "properties": {"x": {"type": "array"}}}, "items must be"),
        ({"type": "object", "required": ["y"]}, "not an input"),
        ({"type": "object", "additionalProperties": True}, "additionalProperties"),
        ({"type": "object", "$defs": {}}, "'$defs'"),
    ],
)
def test_a_schema_outside_the_subset_is_refused(schema: Any, fragment: str) -> None:
    problems = schema_problems(schema)
    assert problems and any(fragment in p for p in problems), problems


def test_defaults_are_filled_in_and_the_missing_optional_is_none() -> None:
    assert resolve_inputs(SCHEMA, {"slug": "box"}) == {
        "slug": "box",
        "copies": 1,
        "colour": "red",
        "scale": None,
        "dry_run": False,
        "plates": [1],
    }


def test_given_values_win_and_are_held_to_their_types() -> None:
    resolved = resolve_inputs(SCHEMA, {"slug": "box", "copies": 3.0, "scale": 2, "plates": []})
    assert resolved["copies"] == 3 and type(resolved["copies"]) is int
    assert resolved["scale"] == 2.0 and type(resolved["scale"]) is float
    assert resolved["plates"] == []


@pytest.mark.parametrize(
    ("values", "fragment"),
    [
        ({}, "'slug' is a required property"),
        ({"slug": "box", "copies": 0}, "copies:"),
        ({"slug": "box", "colour": "green"}, "colour:"),
        ({"slug": 3}, "slug:"),
        ({"slug": "box", "extra": 1}, "'extra' was unexpected"),
    ],
)
def test_values_that_do_not_fit_are_refused(values: dict[str, Any], fragment: str) -> None:
    with pytest.raises(InputsError) as caught:
        resolve_inputs(SCHEMA, values)
    assert any(fragment in p for p in caught.value.problems), caught.value.problems


def test_an_integer_enum_sent_as_a_float_is_its_integer() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer", "enum": [3, 4]}}}
    assert resolve_inputs(schema, {"n": 4}) == {"n": 4}
    with pytest.raises(InputsError):
        resolve_inputs(schema, {"n": 3.5})
    assert resolve_inputs(schema, {"n": 3.0}) == {"n": 3}


def test_a_boolean_left_out_is_false_and_typed_bool() -> None:
    schema = {"type": "object", "properties": {"dry": {"type": "boolean"}}}
    assert resolve_inputs(schema, {}) == {"dry": False}


async def test_a_boolean_left_out_reads_as_bool_in_the_script() -> None:
    schema = {"type": "object", "properties": {"dry": {"type": "boolean"}}}
    script = _script("i = await inputs()", "x: bool = i['dry']", "return x")
    assert await check_script(script, schema) == []


def test_a_long_pattern_is_refused() -> None:
    schema = {"type": "object", "properties": {"s": {"type": "string", "pattern": "a" * 300}}}
    assert any("pattern" in p for p in schema_problems(schema))


def test_a_required_boolean_is_refused() -> None:
    """#2661: left out, a boolean is false, so `required` could never refuse it."""
    schema = {"type": "object", "properties": {"dry": {"type": "boolean"}}, "required": ["dry"]}
    assert any("never missing" in p for p in schema_problems(schema))


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "string", "pattern": "^(a+)+$"},
        {"type": "string", "pattern": "^(a+)+$", "maxLength": 100_000},
        {"type": "array", "items": {"type": "string", "pattern": "^(a+)+$"}},
    ],
)
def test_a_pattern_needs_a_bounded_max_length(prop: dict[str, Any]) -> None:
    """#2661: every start runs the pattern; a short backtracking one on a long value is slow."""
    schema = {"type": "object", "properties": {"s": prop}}
    assert any("maxLength" in p for p in schema_problems(schema))
    bounded = {
        "type": "object",
        "properties": {"s": {"type": "string", "pattern": "^a+$", "maxLength": 64}},
    }
    assert schema_problems(bounded) == []


SLOW = {"type": "string", "pattern": "^[a-z]+$", "maxLength": 64}


@pytest.mark.parametrize(
    ("schema", "values", "where"),
    [
        ({"type": "object", "properties": {"name": SLOW}}, {"name": "A" * 5000}, "name"),
        (
            {"type": "object", "properties": {"tags": {"type": "array", "items": SLOW}}},
            {"tags": ["ok", "A" * 5000]},
            "tags.1",
        ),
    ],
)
def test_an_over_long_patterned_value_is_refused_before_its_pattern_runs(
    schema: dict[str, Any], values: dict[str, Any], where: str
) -> None:
    """#2661 review: maxLength is what bounds a pattern's work, and the validator runs
    every keyword, so the length is checked first and the pattern never sees the value."""
    with pytest.raises(InputsError) as err:
        resolve_inputs(schema, values)
    assert err.value.problems == [f"{where}: is longer than its maxLength of 64"]


def test_a_backtracking_pattern_never_sees_a_long_value() -> None:
    """27 characters against `^(a+)+$` take seconds of backtracking; refused on length,
    they take none. (Longer would only make the old code hang rather than fail.)"""
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "pattern": "^(a+)+$", "maxLength": 16}},
    }
    started = time.monotonic()
    with pytest.raises(InputsError):
        resolve_inputs(schema, {"name": "a" * 26 + "!"})
    assert time.monotonic() - started < 0.5


def test_an_input_in_pydantics_protected_namespace_raises_no_warning() -> None:
    """#2661: pydantic's protected namespaces (`model_validate`, `model_dump`) are not the
    script's."""
    schema = {
        "type": "object",
        "properties": {"model_validate_size": {"type": "integer", "default": 1}},
    }
    assert schema_problems(schema) == []
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        model = inputs_model(schema)
    assert resolve_inputs(schema, {}) == {"model_validate_size": 1}
    assert "model_validate_size" in model.model_fields


def test_a_flow_without_a_schema_takes_no_inputs() -> None:
    assert resolve_inputs(None, None) == {}
    assert resolve_inputs(None, {}) == {}
    with pytest.raises(InputsError):
        resolve_inputs(None, {"slug": "box"})


async def test_a_script_reads_its_inputs_typed() -> None:
    script = _script(
        "i = await inputs()",
        "n: int = i['copies'] * 2",
        "if i['dry_run']:",
        "    return [i['colour'].upper(), n, i['plates'][0] + 1]",
        "if i['scale'] is not None:",
        "    await sleep(i['scale'])",
        "return i['slug']",
    )
    assert await check_script(script, SCHEMA) == []


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("return i['slugg']", "invalid-key"),
        ("return i['copies'] + 'x'", "unsupported-operator"),
        ("await sleep(i['colour'])", "invalid-argument-type"),
        ("await sleep(i['scale'])", "invalid-argument-type"),
        ("return i['plates'].upper()", "unresolved-attribute"),
        ("x: FlowInputs = {'slug': 1}", "missing-typed-dict-key"),
    ],
)
async def test_the_type_check_catches_misuse_of_inputs(line: str, kind: str) -> None:
    problems = await check_script(_script("i = await inputs()", line), SCHEMA)
    assert problems and {p.line for p in problems} == {4}, problems
    assert any(kind in p.message for p in problems), problems


async def test_a_flow_without_a_schema_has_no_inputs_function() -> None:
    problems = await check_script(_script("i = await inputs()", "return i"))
    assert problems and "inputs" in problems[0].message
    assert await check_script(_script("await sleep(1)")) == []


async def test_a_bad_schema_is_reported_against_inputs_json() -> None:
    problems = await check_script(_script("await sleep(1)"), {"type": "array"})
    assert problems and all(p.file == "inputs.json" for p in problems)
