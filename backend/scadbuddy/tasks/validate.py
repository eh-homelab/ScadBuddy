"""Check an answer against its task's schema."""

from __future__ import annotations

from typing import Any

import jsonschema
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from scadbuddy.core.input_schema import (
    Validator,
    iter_errors,
    schema_problems,
    too_long_for_pattern,
    without_patterns,
)
from scadbuddy.tasks.models import AnswerProblem

_FIXED = {
    "enum": "not one of the allowed values",
    "const": "not the allowed value",
    "pattern": "does not match the required pattern",
    "additionalProperties": "has a field the schema does not allow",
    "uniqueItems": "has repeated items",
}
_BOUNDS = {
    "minLength": "is shorter than {} characters",
    "maxLength": "is longer than {} characters",
    "minimum": "is less than {}",
    "maximum": "is more than {}",
    "exclusiveMinimum": "is not more than {}",
    "exclusiveMaximum": "is not less than {}",
    "multipleOf": "is not a multiple of {}",
    "minItems": "has fewer than {} items",
    "maxItems": "has more than {} items",
}


def _messages(error: ValidationError) -> list[str]:
    """What is wrong, from the keyword and the schema. Never from the instance, which
    jsonschema's own messages quote."""
    keyword = str(error.validator)
    value: Any = error.validator_value
    if keyword == "required":
        present = error.instance if isinstance(error.instance, dict) else {}
        return [f"{name} is required" for name in value if name not in present]
    if keyword == "format":
        return [f"not a valid {value}"]
    if keyword == "type":
        kinds = value if isinstance(value, list) else [value]
        return ["must be " + " or ".join(map(str, kinds))]
    if keyword in _FIXED:
        return [_FIXED[keyword]]
    if keyword in _BOUNDS:
        return [_BOUNDS[keyword].format(value)]
    return [f"fails {keyword}"]


def validate_answer(schema: dict[str, Any], answer: Any) -> list[AnswerProblem]:
    """Every way ``answer`` fails ``schema``. Patterns are bounded as flows' inputs are
    (#2661), extra keys are refused, and a ``$ref`` is never fetched."""
    properties: dict[str, Any] = schema.get("properties", {})
    given = answer if isinstance(answer, dict) else {}
    too_long = too_long_for_pattern(properties, given)
    checked = {**without_patterns(schema, too_long), "additionalProperties": False}
    validator = Validator(
        checked,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
        registry=Registry(),
    )
    problems: list[AnswerProblem] = []
    seen: set[tuple[str, str]] = set()

    def add(field: str, message: str) -> None:
        # `required` makes one error per missing field, and each lists them all.
        if (field, message) not in seen:
            seen.add((field, message))
            problems.append(AnswerProblem(field=field, message=message))

    try:
        errors = iter_errors(validator, answer)
    except Unresolvable:
        return [AnswerProblem(field="", message="the schema refers to a schema that is not there")]
    for error in errors:
        path = error.absolute_path
        if error.validator == "maxLength" and path and path[0] in too_long:
            # Said below, with its limit.
            continue
        for message in _messages(error):
            add("/".join(str(part) for part in path), message)
    for said in too_long.values():
        for line in said:
            where, _, message = line.partition(": ")
            add(where.replace(".", "/"), message)
    return problems


def prompt_schema_problems(schema: dict[str, Any]) -> list[str]:
    return schema_problems(schema, formats=True)
