"""Check an answer against its task's schema."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from itertools import islice
from typing import Any, Final

from jsonschema.exceptions import ValidationError

from scadbuddy.core.input_schema import (
    FORMAT_CHECKER,
    Validator,
    iter_errors,
    too_long_for_pattern,
    without_patterns,
)
from scadbuddy.core.json_values import unstorable
from scadbuddy.tasks.models import AnswerProblem
from scadbuddy.tasks.schema import prompt_schema_problems

__all__ = ["prompt_schema_problems", "validate_answer"]

#: The most problems one check returns.
MAX_PROBLEMS: Final = 50
#: An answer is an object of scalars or of arrays of scalars.
MAX_ANSWER_DEPTH: Final = 2

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
    if keyword == "pattern" and isinstance(error.cause, TimeoutError):
        return ["could not be checked: its pattern took too long"]
    if keyword == "pattern" and error.cause is not None:
        return ["could not be checked: its pattern does not compile"]
    if keyword in _FIXED:
        return [_FIXED[keyword]]
    if keyword in _BOUNDS:
        return [_BOUNDS[keyword].format(value)]
    return [f"fails {keyword}"]


def _field(schema: Any, path: Sequence[str | int]) -> str:
    """The declared property the path is in, and its array index. Never a name the answer
    made up."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not path or not isinstance(properties, dict) or path[0] not in properties:
        return ""
    index = [str(part) for part in path[1:2] if isinstance(part, int)]
    return "/".join([str(path[0]), *index])


def _problems(schema: Any, found: Iterable[tuple[Sequence[str | int], str]]) -> list[AnswerProblem]:
    # `required` makes one error per missing field, and each lists them all.
    unique = dict.fromkeys((_field(schema, path), message) for path, message in found)
    return [
        AnswerProblem(field=field, message=message)
        for field, message in islice(unique, MAX_PROBLEMS)
    ]


def _failures(schema: dict[str, Any], answer: Any) -> Iterator[tuple[Sequence[str | int], str]]:
    properties: dict[str, Any] = schema.get("properties", {})
    too_long = too_long_for_pattern(properties, answer if isinstance(answer, dict) else {})
    checked = {**without_patterns(schema, too_long), "additionalProperties": False}
    for error in iter_errors(Validator(checked, format_checker=FORMAT_CHECKER), answer):
        path = list(error.absolute_path)
        if error.validator == "maxLength" and path and path[0] in too_long:
            # Said below, with its limit.
            continue
        for message in _messages(error):
            yield path, message
    for said in too_long.values():
        for where, limit in said:
            yield where, f"is longer than its maxLength of {limit}"


def validate_answer(schema: dict[str, Any], answer: Any) -> list[AnswerProblem]:
    """Every way ``answer`` fails ``schema``, at most `MAX_PROBLEMS`. Patterns are bounded
    as flows' inputs are (#2661) and extra keys are refused. A stored schema outside the
    prompts' subset is never validated against, so no ``$ref`` is ever resolved."""
    # First: the validator compares and prints values, which a number that is not finite,
    # a huge integer or a deep nesting breaks.
    bad = unstorable(answer, max_depth=MAX_ANSWER_DEPTH)
    problems = _problems(schema, ((one.path, one.message) for one in bad))
    if problems:
        return problems
    if prompt_schema_problems(schema, stored=True):
        return [AnswerProblem(field="", message="this question's form is broken")]
    return _problems(schema, _failures(schema, answer))
