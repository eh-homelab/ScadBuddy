"""Check an answer against its task's schema."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from scadbuddy.core.input_schema import schema_problems
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
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    problems: list[AnswerProblem] = []
    seen: set[tuple[str, str]] = set()
    for error in sorted(
        validator.iter_errors(answer), key=lambda e: [str(p) for p in e.absolute_path]
    ):
        field = "/".join(str(part) for part in error.absolute_path)
        for message in _messages(error):
            # `required` makes one error per missing field, and each lists them all.
            if (field, message) not in seen:
                seen.add((field, message))
                problems.append(AnswerProblem(field=field, message=message))
    return problems


def prompt_schema_problems(schema: dict[str, Any]) -> list[str]:
    return schema_problems(schema)
