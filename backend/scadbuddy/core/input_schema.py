"""The closed JSON Schema subset that typed inputs are declared in: a flow's
``inputs.json`` (#2483) and a pipeline template's ``pipeline.inputs`` (#5701, spec
2026-11-17 §4.1). One checker, so the two never drift.

The subset keeps one Python type and one form control per input:

- the top level is ``{"type": "object", "properties": {...}, "required": [...]}``, with
  optional ``$schema``, ``title``, ``description`` and ``additionalProperties: false``
  (extra inputs are always refused);
- each input is a ``string``, ``integer``, ``number``, ``boolean``, or an ``array`` of
  one of the first four, with optional ``title``, ``description``, ``default`` and the
  type's own constraints; a ``string`` or ``integer`` may be an ``enum``;
- with ``objects``, a top-level input may also be an ``object`` of such scalar inputs,
  with its own ``required`` and ``additionalProperties: false``: one level, never two.

A prompt's schema (a task's, ``prompt_schema_problems``) is checked with ``formats``: then a
``string`` (or a string ``items``) may also have a ``format`` of ``email``, ``date`` or
``date-time``, which `FORMAT_CHECKER` checks and which a ``default`` must fit. No other
format is allowed. Without ``formats``, ``format`` stays refused, for flows' inputs and for
pipelines.

The API runs every ``pattern`` on the values it checks, so a pattern is bounded
(`MAX_PATTERN`), needs a ``maxLength`` (`MAX_PATTERNED_LENGTH`) and runs with a deadline
(`PATTERN_TIMEOUT_S`, `PATTERNS_BUDGET_S`; #2661).
"""

from __future__ import annotations

import json
import keyword
import math
import re
import time
from collections.abc import Collection, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Any, Final

import jsonschema
import regex
from pydantic import BaseModel

MAX_SCHEMA_BYTES = 16_384
MAX_INPUTS = 50
MAX_PATTERN = 256
#: The longest value a ``pattern`` runs against, which such an input must declare as its
#: ``maxLength`` (#2661): every check runs the regex in the API process.
MAX_PATTERNED_LENGTH = 1024
#: How long a pattern may take on one value before the value is refused (#2661): a bounded
#: `maxLength` bounds a linear pattern's work, not an exponential one's (`^(a|aa)+$`).
PATTERN_TIMEOUT_S: Final = 0.1
#: How long all of one check's patterns may take together: the per-value deadline alone
#: lets an array of many items, or many patterned inputs, hold the event loop for each.
PATTERNS_BUDGET_S: Final = 0.5
#: When the patterns of the check under way must be done by (monotonic), None outside one.
patterns_deadline: ContextVar[float | None] = ContextVar("patterns_deadline", default=None)

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOP = {"$schema", "type", "title", "description", "properties", "required", "additionalProperties"}
SCALARS: Final[dict[str, type]] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}
_NUMERIC = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
_OWN: dict[str, set[str]] = {
    "string": {"minLength", "maxLength", "pattern", "enum"},
    "integer": _NUMERIC | {"enum"},
    "number": _NUMERIC,
    "boolean": set(),
    "array": {"items", "minItems", "maxItems", "uniqueItems"},
}
#: The formats a prompt's strings may have (``formats=True``).
FORMATS: Final = ("email", "date", "date-time")
#: Checks exactly `FORMATS`, with no optional dependency. jsonschema's shared checker has a
#: format only where its package is installed, and the image has no rfc3339-validator.
FORMAT_CHECKER: Final = jsonschema.FormatChecker(formats=("email", "date"))
_DATE_TIME = re.compile(
    r"(\d{4}-\d{2}-\d{2})[Tt](?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
    re.ASCII,
)
_OBJECT_OWN = {"properties", "required", "additionalProperties"}
_DESCRIBED = {"type", "title", "description"}
#: Names a pydantic model cannot hold as fields (flows' `inputs_model`).
_PYDANTIC = frozenset(name for name in dir(BaseModel) if not name.startswith("__"))


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_date_time(instance: object) -> bool:
    """RFC 3339, so with an offset. A leap second is refused: `datetime` holds none."""
    if not isinstance(instance, str):
        return True
    found = _DATE_TIME.fullmatch(instance)
    return found is not None and bool(date.fromisoformat(found[1]))


def schema_problems(
    schema: Any,
    *,
    stored: bool = False,
    objects: bool = False,
    reserved: Collection[str] = (),
    formats: bool = False,
    defaults: bool = True,
) -> list[str]:
    """Everything wrong with ``schema`` as an inputs schema; empty when it may be used.

    ``stored``: a schema registered already, checked again as it is used. The two rules
    #2661 added are a registration's only: a stored `pattern` with no `maxLength` is
    capped as it runs, and a stored required boolean is harmless (it is false when left
    out), so neither stops it.
    ``objects``: a top-level input may be an object of scalar inputs (#5701).
    ``formats``: a string may have a ``format`` of `FORMATS` (a prompt's, #7364).
    ``defaults``: false skips the check of each ``default``, which runs its pattern within
    the budget below; the structure is still checked.
    ``reserved``: top-level names the host owns (a pipeline's ``params`` and ``v``).

    Every default's pattern runs within one `PATTERNS_BUDGET_S`, the caller's if it set
    one."""
    return _schema_problems(
        schema,
        stored=stored,
        objects=objects,
        reserved=reserved,
        formats=formats,
        defaults=defaults,
    )


def _schema_problems(
    schema: Any,
    *,
    stored: bool,
    objects: bool,
    reserved: Collection[str],
    formats: bool,
    defaults: bool,
) -> list[str]:
    if not isinstance(schema, dict):
        return ["the inputs schema must be a JSON object"]
    if len(json.dumps(schema).encode()) > MAX_SCHEMA_BYTES:
        return [f"the inputs schema is over {MAX_SCHEMA_BYTES // 1024} KiB"]
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as err:
        return [f"not a JSON Schema: {err.message}"]
    except (OverflowError, RecursionError):
        # What `re` raises for `a{99999999999999}`, and for thousands of groups.
        return ["not a JSON Schema: a pattern is too large, or it is nested too deeply"]
    # After the metaschema check, which runs no pattern: a busy pod must not spend the budget there.
    token = None
    if patterns_deadline.get() is None:
        token = patterns_deadline.set(time.monotonic() + PATTERNS_BUDGET_S)
    try:
        return _properties_problems(
            schema,
            stored=stored,
            objects=objects,
            reserved=reserved,
            formats=formats,
            defaults=defaults,
        )
    finally:
        if token is not None:
            patterns_deadline.reset(token)


def _properties_problems(
    schema: dict[str, Any],
    *,
    stored: bool,
    objects: bool,
    reserved: Collection[str],
    formats: bool,
    defaults: bool,
) -> list[str]:
    problems = [f"{key!r} is not supported at the top level" for key in schema if key not in _TOP]
    if schema.get("type") != "object":
        problems.append('the top level must be {"type": "object"}')
    if schema.get("additionalProperties", False) is not False:
        problems.append("additionalProperties may only be false: extra inputs are refused")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return [*problems, "properties must be an object"]
    if len(properties) > MAX_INPUTS:
        problems.append(f"at most {MAX_INPUTS} inputs")
    for name, prop in properties.items():
        if name in reserved:
            problems.append(f"{name}: {name!r} is reserved")
            continue
        if objects and isinstance(prop, dict) and prop.get("type") == "object":
            found = _object_problems(name, prop, stored=stored, formats=formats, defaults=defaults)
        else:
            found = _property_problems(
                name, prop, stored=stored, formats=formats, defaults=defaults
            )
        problems += [f"{name}: {p}" for p in found]
    problems += _required_problems(schema, properties, stored=stored, what="an input")
    return problems


def _required_problems(
    schema: dict[str, Any], properties: dict[str, Any], *, stored: bool, what: str
) -> list[str]:
    problems: list[str] = []
    for name in schema.get("required", []):
        if name not in properties:
            problems.append(f"required names {name!r}, which is not {what}")
        elif (
            not stored
            and isinstance(properties[name], dict)
            and properties[name].get("type") == "boolean"
        ):
            # Left out, a boolean is false (flows' `_default`), so it is never missing (#2661).
            problems.append(f"required names {name!r}, a boolean, which is never missing")
    return problems


def _object_problems(
    name: str, prop: dict[str, Any], *, stored: bool, formats: bool, defaults: bool
) -> list[str]:
    """One level of object (#5701): scalar properties only, and no extra keys."""
    if not _NAME.match(name) or keyword.iskeyword(name):
        return ["a name is lower-case letters, digits and _, starting with a letter"]
    if name in _PYDANTIC:
        return [f"{name!r} is reserved"]
    allowed = _DESCRIBED | _OBJECT_OWN
    problems = [f"{key!r} is not supported for object" for key in prop if key not in allowed]
    if prop.get("additionalProperties") is not False:
        problems.append("additionalProperties may only be false: extra keys are refused")
    properties = prop.get("properties", {})
    if not isinstance(properties, dict):
        return [*problems, "properties must be an object"]
    if len(properties) > MAX_INPUTS:
        problems.append(f"at most {MAX_INPUTS} properties")
    for inner, spec in properties.items():
        problems += [
            f"{inner}: {p}"
            for p in _property_problems(
                inner, spec, stored=stored, formats=formats, defaults=defaults
            )
        ]
    problems += _required_problems(prop, properties, stored=stored, what="a property")
    return problems


def _format_problems(spec: dict[str, Any], where: str) -> list[str]:
    if "format" not in spec or spec["format"] in FORMATS:
        return []
    shown = str(spec["format"])[:40]
    return [f"{where}format {shown!r} is not allowed: use one of {', '.join(FORMATS)}"]


def _property_problems(
    name: str, prop: Any, *, stored: bool = False, formats: bool = False, defaults: bool = True
) -> list[str]:
    if not _NAME.match(name) or keyword.iskeyword(name):
        return ["a name is lower-case letters, digits and _, starting with a letter"]
    if name in _PYDANTIC:
        return [f"{name!r} is reserved"]
    if not isinstance(prop, dict):
        return ["must be an object"]
    kind = prop.get("type")
    if not isinstance(kind, str) or kind not in _OWN:
        return ["type must be one of " + ", ".join(_OWN)]
    extra = {"format"} if formats and kind == "string" else set()
    allowed = _DESCRIBED | {"default"} | _OWN[kind] | extra
    problems = [f"{key!r} is not supported for {kind}" for key in prop if key not in allowed]
    if extra:
        problems += _format_problems(prop, "")
    if kind == "array":
        items = prop.get("items")
        if not isinstance(items, dict) or items.get("type") not in tuple(SCALARS):
            problems.append("items must be a string, integer, number or boolean schema")
        else:
            own = _DESCRIBED | _OWN[items["type"]]
            if formats and items["type"] == "string":
                own |= {"format"}
                problems += _format_problems(items, "items: ")
            problems += [f"items: {key!r} is not supported" for key in items if key not in own]
    if "enum" in prop and (not isinstance(prop["enum"], list) or not prop["enum"]):
        problems.append("enum must list at least one value")
    items = prop.get("items")
    for where in (prop, items if isinstance(items, dict) else {}):
        # Only a string's: on any other type `pattern` is "not supported" above, and JSON
        # Schema ignores it there.
        if "pattern" not in where or where.get("type") != "string":
            continue
        if len(str(where["pattern"])) > MAX_PATTERN:
            # Every check runs it: a pasted one is bounded.
            problems.append(f"a pattern is at most {MAX_PATTERN} characters")
        # And so is what it runs against: a short backtracking pattern on a long value is
        # still slow (#2661).
        length = where.get("maxLength")
        unbounded = (
            not isinstance(length, int) or isinstance(length, bool) or length > MAX_PATTERNED_LENGTH
        )
        if unbounded and not stored:
            problems.append(f"a pattern needs a maxLength of at most {MAX_PATTERNED_LENGTH}")
    if defaults and "default" in prop and not problems:
        try:
            checker = FORMAT_CHECKER if formats else None
            Validator(prop, format_checker=checker).validate(prop["default"])
        except jsonschema.ValidationError as err:
            problems.append(f"default does not fit: {err.message}")
    return problems


def _pattern(
    validator: Any, pattern: str, instance: Any, schema: dict[str, Any]
) -> Iterator[jsonschema.ValidationError]:
    """jsonschema's `pattern`, run with a deadline: past PATTERN_TIMEOUT_S the value is
    refused rather than left to backtrack on the API's event loop (#2661).

    A pattern runs on `regex` (VERSION0), but registration checks it with the metaschema's
    `regex` format, which compiles it with `re`: so the dialect is what both accept, and a
    pattern only `regex` takes (`\\p{L}`) is refused when the schema is checked (#3416)."""
    if not validator.is_type(instance, "string"):
        return
    timeout = PATTERN_TIMEOUT_S
    if (deadline := patterns_deadline.get()) is not None:
        timeout = min(timeout, deadline - time.monotonic())
    if timeout <= 0:
        yield jsonschema.ValidationError(
            f"is refused: the patterns took over {PATTERNS_BUDGET_S}s to check together",
            cause=TimeoutError(),
        )
        return
    try:
        found = regex.search(pattern, instance, timeout=timeout)
    except TimeoutError as err:
        yield jsonschema.ValidationError(
            f"is refused: its pattern took over {PATTERN_TIMEOUT_S}s to check it", cause=err
        )
        return
    except regex.error as err:
        yield jsonschema.ValidationError(f"its pattern does not compile: {err}", cause=err)
        return
    if found is None:
        # The value is not quoted, as the over-long refusal never quotes it (#3416).
        yield jsonschema.ValidationError(f"does not match {pattern!r}")


def _shown(value: Any) -> str:
    try:
        return repr(value)
    except (ValueError, RecursionError):
        # CPython writes no integer of more than 4300 digits, and no deep nesting.
        return "the value"


def _is_multiple(number: float, of: float) -> bool:
    if not all(math.isfinite(n) for n in (number, of) if isinstance(n, float)):
        return False
    try:
        if not isinstance(of, float):
            return bool(number % of == 0)
        quotient = number / of
        return int(quotient) == quotient
    except OverflowError:
        # One of them, or the quotient, is too large for a float.
        return (Fraction(number) / Fraction(of)).denominator == 1


def _multiple_of(
    validator: Any, multiple: Any, instance: Any, schema: dict[str, Any]
) -> Iterator[jsonschema.ValidationError]:
    """jsonschema's `multipleOf`, which raises for a number that is not finite, and for a
    huge integer against a fraction."""
    if not validator.is_type(instance, "number") or _is_multiple(instance, multiple):
        return
    yield jsonschema.ValidationError(f"{_shown(instance)} is not a multiple of {_shown(multiple)}")


def _too_many(instance: Any, schema: dict[str, Any]) -> bool:
    return bool(len(instance) > schema.get("maxItems", len(instance)))


def _items(
    validator: Any, items: Any, instance: Any, schema: dict[str, Any]
) -> Iterator[jsonschema.ValidationError]:
    """jsonschema's `items`, for an array that `maxItems` has not refused."""
    if validator.is_type(instance, "array") and not _too_many(instance, schema):
        yield from jsonschema.Draft202012Validator.VALIDATORS["items"](
            validator, items, instance, schema
        )


def _scalar_key(item: Any) -> str | None:
    """The same text for the scalars that JSON Schema says are equal, and None for any other
    item. Text, because a caller can choose integers that all have one hash."""
    if item is None or isinstance(item, bool):
        return str(item)
    if isinstance(item, str):
        return f"s{item}"
    if isinstance(item, float) and not item.is_integer():
        return f"n{item.hex()}"
    if isinstance(item, int | float):
        return f"n{int(item):x}"
    return None


def _unique_items(
    validator: Any, unique: Any, instance: Any, schema: dict[str, Any]
) -> Iterator[jsonschema.ValidationError]:
    """jsonschema's `uniqueItems`, in linear time. An item that is not a scalar is not
    walked: the subset's `items` refuses it."""
    if not unique or not validator.is_type(instance, "array") or _too_many(instance, schema):
        return
    keys = [key for item in instance if (key := _scalar_key(item)) is not None]
    if len(set(keys)) < len(keys):
        yield jsonschema.ValidationError(f"{_shown(instance)} has non-unique elements")


#: Draft 2020-12, with the keywords above, which never raise and stay bounded.
Validator = jsonschema.validators.extend(  # type: ignore[no-untyped-call]
    jsonschema.Draft202012Validator,
    {
        "pattern": _pattern,
        "multipleOf": _multiple_of,
        "items": _items,
        "uniqueItems": _unique_items,
    },
)


def too_long_for_pattern(
    properties: dict[str, Any], given: dict[str, Any]
) -> dict[str, list[tuple[tuple[str | int, ...], int]]]:
    """Each input with a patterned string value longer than its maxLength: where each such
    value is, and the limit. The limit is never above MAX_PATTERNED_LENGTH: a schema stored
    before #2661 may have no maxLength, or one far larger, and either would let the pattern
    run on any value."""
    found: dict[str, list[tuple[tuple[str | int, ...], int]]] = {}
    for name, prop in properties.items():
        if not isinstance(prop, dict) or name not in given:
            continue
        value = given[name]
        items = prop.get("items")
        checks: list[tuple[tuple[str | int, ...], dict[str, Any], Any]] = [((name,), prop, value)]
        if isinstance(items, dict) and isinstance(value, list):
            checks = [((name, i), items, item) for i, item in enumerate(value)]
        for where, spec, one in checks:
            if "pattern" not in spec or not isinstance(one, str):
                continue
            limit = spec.get("maxLength")
            if not isinstance(limit, int) or isinstance(limit, bool):
                limit = MAX_PATTERNED_LENGTH
            limit = min(limit, MAX_PATTERNED_LENGTH)
            if len(one) > limit:
                found.setdefault(name, []).append((where, limit))
    return found


def without_patterns(schema: dict[str, Any], names: Iterable[str]) -> dict[str, Any]:
    """``schema`` with those inputs' patterns left out (and their items'), so the
    validator still checks everything else about them."""
    properties = dict(schema.get("properties", {}))
    for name in names:
        prop = {k: v for k, v in properties[name].items() if k != "pattern"}
        if isinstance(prop.get("items"), dict):
            prop["items"] = {k: v for k, v in prop["items"].items() if k != "pattern"}
        properties[name] = prop
    return {**schema, "properties": properties}


def iter_errors(validator: Any, values: Any) -> list[jsonschema.ValidationError]:
    """Every error of ``values`` against the validator, in path order, its patterns within
    one `PATTERNS_BUDGET_S` (#2661)."""
    token = patterns_deadline.set(time.monotonic() + PATTERNS_BUDGET_S)
    try:
        return sorted(
            validator.iter_errors(values), key=lambda e: [str(p) for p in e.absolute_path]
        )
    finally:
        patterns_deadline.reset(token)


@dataclass(frozen=True)
class ValueProblem:
    """One value that does not fit: where (``house.cols``; empty for the top level), the
    keyword it failed (``maximum``, ``required``, ``additionalProperties`` …), and why."""

    path: str
    keyword: str
    message: str


def value_problems(schema: dict[str, Any], values: Any) -> list[ValueProblem]:
    """Every way ``values`` fails ``schema`` (a schema `schema_problems` passed), in path
    order, its patterns within one `PATTERNS_BUDGET_S`. Defaults are not filled in."""
    errors = iter_errors(Validator(schema), values)
    return [
        ValueProblem(".".join(str(p) for p in err.absolute_path), str(err.validator), err.message)
        for err in errors
    ]
