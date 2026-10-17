"""A flow's typed inputs (#2483): the schema's check, a run's values, and the script's
type check against them."""

import time
import warnings
from typing import Any

import pytest

from scadbuddy.flows.inputs import (
    MAX_PATTERNED_LENGTH,
    PATTERN_TIMEOUT_S,
    PATTERNS_BUDGET_S,
    InputsError,
    inputs_model,
    resolve_inputs,
    schema_problems,
)
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


def test_a_pattern_that_does_not_compile_is_refused_when_saved() -> None:
    """#3416: found when the version is registered, not at its first Start: the
    metaschema's `regex` format already refuses it, before any rule of ours runs."""
    schema = {
        "type": "object",
        "properties": {"s": {"type": "string", "pattern": "(", "maxLength": 8}},
    }
    assert schema_problems(schema) == ["not a JSON Schema: '(' is not a 'regex'"]


def test_a_mismatch_names_the_pattern_not_the_value() -> None:
    """#3416: as the over-long refusal, the value is never quoted back."""
    schema = {
        "type": "object",
        "properties": {"s": {"type": "string", "pattern": "^a+$", "maxLength": 32}},
    }
    with pytest.raises(InputsError) as caught:
        resolve_inputs(schema, {"s": "secret-ish"})
    assert caught.value.problems == ["s: does not match '^a+$'"]


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


def test_a_pattern_with_a_bounded_max_length_is_accepted() -> None:
    bounded = {
        "type": "object",
        "properties": {"s": {"type": "string", "pattern": "^a+$", "maxLength": 64}},
    }
    assert schema_problems(bounded) == []


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "integer", "pattern": "^1$"},
        {"type": "array", "items": {"type": "integer", "pattern": "^1$"}},
    ],
)
def test_a_pattern_on_another_type_is_not_supported_and_asks_no_max_length(
    prop: dict[str, Any],
) -> None:
    problems = schema_problems({"type": "object", "properties": {"s": prop}})
    assert any("'pattern' is not supported" in p for p in problems), problems
    assert not any("maxLength" in p for p in problems), problems


def test_many_slow_values_share_one_budget() -> None:
    """Each value's deadline alone would let an array of slow items hold the loop for
    each: the run's patterns share PATTERNS_BUDGET_S."""
    schema = {
        "type": "object",
        "properties": {
            "s": {
                "type": "array",
                "items": {"type": "string", "pattern": "^(a|aa)+$", "maxLength": 64},
            }
        },
    }
    started = time.monotonic()
    with pytest.raises(InputsError) as err:
        resolve_inputs(schema, {"s": ["a" * 60 + "!"] * 40})
    assert time.monotonic() - started < PATTERNS_BUDGET_S + 0.5
    assert len(err.value.problems) == 40
    assert any("took over" in p and "together" in p for p in err.value.problems)


def test_an_exponential_pattern_within_its_max_length_is_cut_off() -> None:
    """#2661 review: a bounded maxLength bounds a linear pattern, not `^(a|aa)+$`; the
    value is refused at PATTERN_TIMEOUT_S rather than backtracking for minutes."""
    schema = {
        "type": "object",
        "properties": {"s": {"type": "string", "pattern": "^(a|aa)+$", "maxLength": 64}},
    }
    started = time.monotonic()
    with pytest.raises(InputsError) as err:
        resolve_inputs(schema, {"s": "a" * 60 + "!"})
    assert time.monotonic() - started < 1.0
    assert err.value.problems == [
        f"s: is refused: its pattern took over {PATTERN_TIMEOUT_S}s to check it"
    ]
    assert resolve_inputs(schema, {"s": "aaa"}) == {"s": "aaa"}


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


def test_a_stored_maxlength_above_the_cap_does_not_lift_it() -> None:
    """A schema stored before #2661 may carry any maxLength: the pattern still never sees a
    value longer than MAX_PATTERNED_LENGTH."""
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "pattern": "^a*$", "maxLength": 10**6}},
    }
    with pytest.raises(InputsError) as err:
        resolve_inputs(schema, {"name": "a" * (MAX_PATTERNED_LENGTH + 1)})
    assert err.value.problems == [f"name: is longer than its maxLength of {MAX_PATTERNED_LENGTH}"]
    assert resolve_inputs(schema, {"name": "a" * MAX_PATTERNED_LENGTH})["name"]


def test_an_over_long_value_is_said_beside_every_other_problem() -> None:
    """One 422 says them all: the over-long input, and what else is wrong, its pattern
    never run."""
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "pattern": "^(a+)+$", "maxLength": 16},
            "count": {"type": "integer"},
        },
        "required": ["count"],
    }
    started = time.monotonic()
    with pytest.raises(InputsError) as err:
        resolve_inputs(schema, {"name": "a" * 26 + "!", "count": "three"})
    assert time.monotonic() - started < 0.5
    assert err.value.problems == [
        "name: is longer than its maxLength of 16",
        "count: 'three' is not of type 'integer'",
    ]


def test_each_over_long_item_is_said() -> None:
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string", "pattern": "^a*$", "maxLength": 2},
            }
        },
    }
    with pytest.raises(InputsError) as err:
        resolve_inputs(schema, {"tags": ["aaa", "a", "aaaa"]})
    assert err.value.problems == [
        "tags.0: is longer than its maxLength of 2",
        "tags.2: is longer than its maxLength of 2",
    ]


def test_a_stored_schema_keeps_starting_under_the_rules_added_since() -> None:
    """#2661's two rules are a registration's: a version registered before them still
    starts (its pattern is capped as it runs; a required boolean is false when left out)."""
    schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "pattern": "^[a-z]+$"},
            "on": {"type": "boolean"},
        },
        "required": ["on"],
    }
    assert len(schema_problems(schema)) == 2
    assert schema_problems(schema, stored=True) == []
    assert resolve_inputs(schema, {"code": "abc"}) == {"code": "abc", "on": False}


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
