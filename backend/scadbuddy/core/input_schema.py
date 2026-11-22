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

The API runs every ``pattern`` on the values it checks, so a pattern is bounded
(`MAX_PATTERN`), needs a ``maxLength`` (`MAX_PATTERNED_LENGTH`) and runs with a deadline
(`PATTERN_TIMEOUT_S`, `PATTERNS_BUDGET_S`; #2661).
"""

from __future__ import annotations

import json
import keyword
import re
import time
from collections.abc import Collection, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
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
_OBJECT_OWN = {"properties", "required", "additionalProperties"}
_DESCRIBED = {"type", "title", "description"}
#: Names a pydantic model cannot hold as fields (flows' `inputs_model`).
_PYDANTIC = frozenset(name for name in dir(BaseModel) if not name.startswith("__"))


def schema_problems(
    schema: Any,
    *,
    stored: bool = False,
    objects: bool = False,
    reserved: Collection[str] = (),
) -> list[str]:
    """Everything wrong with ``schema`` as an inputs schema; empty when it may be used.

    ``stored``: a schema registered already, checked again as it is used. The two rules
    #2661 added are a registration's only: a stored `pattern` with no `maxLength` is
    capped as it runs, and a stored required boolean is harmless (it is false when left
    out), so neither stops it.
    ``objects``: a top-level input may be an object of scalar inputs (#5701).
    ``reserved``: top-level names the host owns (a pipeline's ``params`` and ``v``)."""
    if not isinstance(schema, dict):
        return ["the inputs schema must be a JSON object"]
    if len(json.dumps(schema).encode()) > MAX_SCHEMA_BYTES:
        return [f"the inputs schema is over {MAX_SCHEMA_BYTES // 1024} KiB"]
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as err:
        return [f"not a JSON Schema: {err.message}"]
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
            found = _object_problems(name, prop, stored=stored)
        else:
            found = _property_problems(name, prop, stored=stored)
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


def _object_problems(name: str, prop: dict[str, Any], *, stored: bool) -> list[str]:
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
        problems += [f"{inner}: {p}" for p in _property_problems(inner, spec, stored=stored)]
    problems += _required_problems(prop, properties, stored=stored, what="a property")
    return problems


def _property_problems(name: str, prop: Any, *, stored: bool = False) -> list[str]:
    if not _NAME.match(name) or keyword.iskeyword(name):
        return ["a name is lower-case letters, digits and _, starting with a letter"]
    if name in _PYDANTIC:
        return [f"{name!r} is reserved"]
    if not isinstance(prop, dict):
        return ["must be an object"]
    kind = prop.get("type")
    if kind not in _OWN:
        return ["type must be one of " + ", ".join(_OWN)]
    allowed = _DESCRIBED | {"default"} | _OWN[kind]
    problems = [f"{key!r} is not supported for {kind}" for key in prop if key not in allowed]
    if kind == "array":
        items = prop.get("items")
        if not isinstance(items, dict) or items.get("type") not in SCALARS:
            problems.append("items must be a string, integer, number or boolean schema")
        else:
            own = _DESCRIBED | _OWN[items["type"]]
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
    if "default" in prop and not problems:
        try:
            Validator(prop).validate(prop["default"])
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
            f"is refused: the patterns took over {PATTERNS_BUDGET_S}s to check together"
        )
        return
    try:
        found = regex.search(pattern, instance, timeout=timeout)
    except TimeoutError:
        yield jsonschema.ValidationError(
            f"is refused: its pattern took over {PATTERN_TIMEOUT_S}s to check it"
        )
        return
    except regex.error as err:
        yield jsonschema.ValidationError(f"its pattern does not compile: {err}")
        return
    if found is None:
        # The value is not quoted, as the over-long refusal never quotes it (#3416).
        yield jsonschema.ValidationError(f"does not match {pattern!r}")


#: Draft 2020-12, its `pattern` run by `_pattern`.
Validator = jsonschema.validators.extend(  # type: ignore[no-untyped-call]
    jsonschema.Draft202012Validator, {"pattern": _pattern}
)


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
    token = patterns_deadline.set(time.monotonic() + PATTERNS_BUDGET_S)
    try:
        errors = sorted(
            Validator(schema).iter_errors(values), key=lambda e: [str(p) for p in e.absolute_path]
        )
    finally:
        patterns_deadline.reset(token)
    return [
        ValueProblem(".".join(str(p) for p in err.absolute_path), str(err.validator), err.message)
        for err in errors
    ]
