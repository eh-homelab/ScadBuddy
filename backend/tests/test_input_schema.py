"""The closed JSON Schema subset that flows and pipeline templates share (#2483, #5701;
spec 2026-11-17 §4.1, §10.1). The flows' own cases are in tests/flows/test_flow_inputs.py."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest

from scadbuddy.core import input_schema
from scadbuddy.core.input_schema import (
    FORMATS,
    PATTERN_TIMEOUT_S,
    PATTERNS_BUDGET_S,
    Validator,
    patterns_deadline,
    schema_problems,
    value_problems,
)

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


def _failed(schema: dict[str, Any], value: Any) -> list[str]:
    """The keyword of each error (#7364)."""
    return [str(error.validator) for error in Validator(schema).iter_errors(value)]


@pytest.mark.parametrize(
    ("value", "multiple", "fits"),
    [
        (10**400, 0.5, True),
        (10**400, 0.7, False),
        (-(10**400), 0.5, True),
        (1e400, 0.5, False),
        (-1e400, 0.5, False),
        (float("nan"), 0.5, False),
        (1e400, 2, False),
        (float("nan"), 2, False),
        (1.5, 10**400, False),
        (10**401, 10**400, True),
        (1e308, 1e-10, False),
        # As jsonschema's own.
        (4, 2, True),
        (5, 2, False),
        (4.0, 2, True),
        (4.5, 0.5, True),
        (4.6, 0.5, False),
        (0.3, 0.1, False),
        (0.2, 0.1, True),
    ],
    ids=lambda v: str(v)[:12],
)
def test_multiple_of_never_raises(value: Any, multiple: Any, fits: bool) -> None:
    expected = [] if fits else ["multipleOf"]
    assert _failed({"type": "number", "multipleOf": multiple}, value) == expected


def test_multiple_of_takes_an_integer_too_long_to_write() -> None:
    assert _failed({"multipleOf": 0.5}, 10**5000) == []
    assert _failed({"multipleOf": 2}, 10**5000 + 1) == ["multipleOf"]


@pytest.mark.parametrize(
    ("items", "unique"),
    [
        ([], True),
        ([1, 2, 1.5, "1", None, True, False, 0], True),
        ([1, True, 0, False], True),
        ([1, 1.0], False),
        ([0, -0.0], False),
        ([True, True], False),
        ([None, None], False),
        (["a", "b", "a"], False),
        (["1", 1], True),
        ([2**53, 2**53 + 1], True),
        ([float(2**53), 2**53], False),
        ([0.1, 0.1], False),
        (["None", None, "true", True, "0x1", 1], True),
        ([10**5000, 10**5000], False),
    ],
)
def test_unique_items_follow_json_equality(items: list[Any], unique: bool) -> None:
    expected = [] if unique else ["uniqueItems"]
    assert _failed({"uniqueItems": True}, items) == expected
    assert _failed({"uniqueItems": False}, items) == []


def test_unique_items_do_not_walk_an_item_that_is_not_a_scalar() -> None:
    schema = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
    deep = [json.loads("[" * 900 + "]" * 900) for _ in range(2)]
    assert _failed(schema, deep) == ["type", "type"]
    assert _failed(schema, [{"a": 1}, {"a": 1}, "b", "b"]) == ["type", "type", "uniqueItems"]


@pytest.mark.parametrize(
    "items",
    [
        [{"a": i, "b": [i]} for i in range(16_000)],
        [i if i % 2 else f"s{i}" for i in range(32_000)],
    ],
    ids=["objects", "mixed"],
)
def test_unique_items_are_linear(items: list[Any]) -> None:
    started = time.monotonic()
    list(Validator({"uniqueItems": True}).iter_errors(items))
    assert time.monotonic() - started < 8


def test_items_are_not_walked_once_there_are_too_many() -> None:
    schema = {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 2,
        "uniqueItems": True,
    }
    assert _failed(schema, [1, 1, 1]) == ["maxItems"]
    assert _failed(schema, [1, 1]) == ["type", "type", "uniqueItems"]


@pytest.mark.parametrize(
    "prop",
    [
        {"type": ["string", "null"]},
        {"type": "array", "items": {"type": ["string"]}},
        {"type": "string", "maxLength": 5, "pattern": "a{99999999999999}"},
        {"type": "string", "maxLength": 5, "pattern": "(" * 4000 + ")" * 4000},
    ],
    ids=["types", "item-types", "repeat", "parens"],
)
def test_a_schema_outside_the_subset_is_a_problem_not_an_exception(prop: dict[str, Any]) -> None:
    assert schema_problems(_top(x=prop))


def test_the_format_checker_holds_exactly_the_formats() -> None:
    from scadbuddy.core.input_schema import FORMAT_CHECKER

    assert set(FORMAT_CHECKER.checkers) == set(FORMATS)


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-24T09:00:00Z",
        "2026-11-24t09:00:00z",
        "2026-11-24T09:00:00.123456789Z",
        "2026-11-24T23:59:59+05:30",
        "2028-02-29T00:00:00-23:59",
        7,
    ],
)
def test_a_date_time_with_an_offset_fits(value: Any) -> None:
    from scadbuddy.core.input_schema import FORMAT_CHECKER

    assert FORMAT_CHECKER.conforms(value, "date-time")


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-24T09:00:00",
        "2026-11-24T09:00:00.5",
        "2026-11-24 09:00:00Z",
        "2026-11-24T09:00Z",
        "2026-11-24T24:00:00Z",
        "2026-11-24T09:60:00Z",
        "2026-11-24T09:00:60Z",
        "2026-11-24T09:00:00+24:00",
        "2026-11-24T09:00:00+01:60",
        "2026-11-24T09:00:00+0100",
        "2026-11-24T09:00:00+01",
        "2026-11-24T09:00:00.Z",
        "2026-02-29T09:00:00Z",
        "2026-13-01T09:00:00Z",
        "2026-11-24",
        "20261124T090000Z",
        "2026-11-24T09:00:00Z\n",
        "\u0662026-11-24T09:00:00Z",
        "\u0662\u0660\u0662\u0666-11-24T09:00:00Z",
        "",
    ],
)
def test_any_other_date_time_is_refused(value: str) -> None:
    from scadbuddy.core.input_schema import FORMAT_CHECKER

    assert not FORMAT_CHECKER.conforms(value, "date-time")


LONG_LABEL = "b" * 63


@pytest.mark.parametrize(
    "value",
    [
        "jane@example.com",
        "jane.doe+tag@mail.example.co.uk",
        "o'brien!#$%&*/=?^`{|}~@example.com",
        "j\u00fcrgen@m\u00fcnchen.example",
        "\u30e6\u30fc\u30b6\u30fc@\u4f8b\u3048.\u30c6\u30b9\u30c8",
        "a@b.c",
        # 254 characters.
        f"{'a' * 60}@{LONG_LABEL}.{LONG_LABEL}.{LONG_LABEL}.c",
        7,
        None,
    ],
)
def test_an_email_with_a_local_part_and_a_domain_fits(value: Any) -> None:
    from scadbuddy.core.input_schema import FORMAT_CHECKER

    assert FORMAT_CHECKER.conforms(value, "email")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "@",
        "jane",
        "a@b",
        "@example.com",
        "jane@",
        "jane@example",
        "jane@@example.com",
        "jane@doe@example.com",
        "jane@.example.com",
        "jane@example.com.",
        "jane@example..com",
        "jane@.",
        " jane@example.com",
        "jane doe@example.com",
        "jane@exam ple.com",
        "jane@example.com ",
        "jane@example.com\n",
        "jane\n@example.com",
        "jane@example.com\r\nBcc: eve@example.com",
        "jane\t@example.com",
        "jane\x00@example.com",
        "jane@example\x7f.com",
        "jane@example\x85.com",
        "jane\u00a0doe@example.com",
        "jane@example\u2028.com",
        # Characters that mean something in a mail header.
        "jane@example.com,eve",
        "jane,eve@example.com",
        "jane@example.com;eve",
        "<jane>@example.com",
        "jane@example.com>",
        "jane(x)@example.com",
        "jane@[127.0.0.1]",
        '"jane"@example.com',
        "jane\\x@example.com",
        # 255 characters.
        f"{'a' * 61}@{LONG_LABEL}.{LONG_LABEL}.{LONG_LABEL}.c",
    ],
)
def test_any_other_email_is_refused(value: str) -> None:
    from scadbuddy.core.input_schema import FORMAT_CHECKER

    assert not FORMAT_CHECKER.conforms(value, "email")


SLOW_DEFAULT = {
    "type": "string",
    "pattern": "^(a|aa)+$",
    "maxLength": 64,
    "default": "a" * 60 + "!",
}


def test_every_default_of_a_schema_shares_one_pattern_budget() -> None:
    """As a run's values (#6010): counted, not timed."""
    problems = schema_problems(_top(**{f"x{i}": SLOW_DEFAULT for i in range(40)}))
    assert len(problems) == 40
    ran = [p for p in problems if f"its pattern took over {PATTERN_TIMEOUT_S}s" in p]
    together = [p for p in problems if f"took over {PATTERNS_BUDGET_S}s to check together" in p]
    assert len(ran) + len(together) == 40
    assert 1 <= len(ran) <= PATTERNS_BUDGET_S / PATTERN_TIMEOUT_S + 1
    assert patterns_deadline.get() is None


def test_the_patterns_budget_starts_after_the_metaschema_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fake clock: the metaschema check takes ten budgets."""
    now = [100.0]

    def check_schema(schema: Any) -> None:
        now[0] += PATTERNS_BUDGET_S * 10

    monkeypatch.setattr(jsonschema.Draft202012Validator, "check_schema", check_schema)
    monkeypatch.setattr(input_schema, "time", SimpleNamespace(monotonic=lambda: now[0]))
    prop = {"type": "string", "pattern": "^a$", "maxLength": 8, "default": "a"}
    assert schema_problems(_top(x=prop)) == []


def test_a_schema_check_keeps_the_deadline_that_is_set() -> None:
    token = patterns_deadline.set(time.monotonic() - 1)
    try:
        [problem] = schema_problems(_top(x=SLOW_DEFAULT))
    finally:
        patterns_deadline.reset(token)
    assert "to check together" in problem


def _past_deadline(schema: dict[str, Any], **kwargs: Any) -> list[str]:
    token = patterns_deadline.set(time.monotonic() - 1)
    try:
        return schema_problems(schema, **kwargs)
    finally:
        patterns_deadline.reset(token)


def test_a_default_whose_pattern_times_out_raises_for_a_retry() -> None:
    """#7437: a prompt's task tries again; the message never quotes the value."""
    nested = _top(
        o={"type": "object", "additionalProperties": False, "properties": {"x": SLOW_DEFAULT}}
    )
    for schema, kwargs in ((_top(x=SLOW_DEFAULT), {}), (nested, {"objects": True})):
        with pytest.raises(input_schema.PatternsTimedOutError) as err:
            _past_deadline(schema, retry_timeouts=True, **kwargs)
        assert str(SLOW_DEFAULT["default"]) not in str(err.value)


def test_a_timed_out_default_is_refused_when_the_schema_has_another_problem() -> None:
    bad = {"type": "string", "pattern": "^a$", "maxLength": 5, "default": 3}
    problems = _past_deadline(_top(x=SLOW_DEFAULT, y=bad), retry_timeouts=True)
    assert problems == ["y: default does not fit: 3 is not of type 'string'"]


def test_a_timed_out_default_is_refused_without_retry_timeouts() -> None:
    [problem] = _past_deadline(_top(x=SLOW_DEFAULT))
    assert problem.startswith("x: default does not fit:")


def test_a_default_with_a_timeout_and_a_real_error_names_the_real_error() -> None:
    """#7688: the refusal is jsonschema's pick among the real errors, so a timeout does
    not hide a value that is too long; and it is a refusal, not a retry."""
    both = {**SLOW_DEFAULT, "maxLength": 8}
    # A prompt's task (retry_timeouts) and a registration (#7762) name the same error.
    for kwargs in ({"retry_timeouts": True}, {}):
        [problem] = _past_deadline(_top(x=both), **kwargs)
        assert problem.startswith("x: default does not fit:") and "too long" in problem
        assert "to check" not in problem
