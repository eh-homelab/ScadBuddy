"""A flow's typed inputs (#2483): a JSON Schema versioned with its script, as
``<name>/inputs.json`` in ``data/flows``, in the same commit.

The schema is a closed subset of JSON Schema, so that each input has one Python type the
type check can hold a script to, and one control the Start run form can draw:

- the top level is ``{"type": "object", "properties": {...}, "required": [...]}``, with
  optional ``$schema``, ``title``, ``description`` and ``additionalProperties: false``
  (extra inputs are always refused);
- each input is a ``string``, ``integer``, ``number``, ``boolean``, or an ``array`` of
  one of the first four, with optional ``title``, ``description``, ``default`` and the
  type's own constraints; a ``string`` or ``integer`` may be an ``enum``.

It is checked when a version is registered (:func:`schema_problems`, then the script's
type check against :func:`inputs_model`), and a run's values when it starts
(:func:`resolve_inputs`: validated, defaults filled in). The script reads them with
``inputs()``, typed as the ``FlowInputs`` TypedDict the model renders to.
"""

from __future__ import annotations

import copy
import json
import keyword
import re
from collections.abc import Iterable, Iterator
from typing import Any, Final, Literal

import jsonschema
import regex
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

INPUTS_FILE: Final = "inputs.json"
MAX_SCHEMA_BYTES = 16_384
MAX_INPUTS = 50
MAX_PATTERN = 256
#: The longest value a ``pattern`` runs against, which such an input must declare as its
#: ``maxLength`` (#2661): every start runs the regex in the API process.
MAX_PATTERNED_LENGTH = 1024
#: How long a pattern may take on one value before the value is refused (#2661): a bounded
#: `maxLength` bounds a linear pattern's work, not an exponential one's (`^(a|aa)+$`).
PATTERN_TIMEOUT_S: Final = 0.1
#: The type the script's `inputs()` returns, in the stubs and the editor's help.
INPUTS_TYPE = "FlowInputs"

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOP = {"$schema", "type", "title", "description", "properties", "required", "additionalProperties"}
_SCALARS: dict[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool}
_NUMERIC = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
_OWN: dict[str, set[str]] = {
    "string": {"minLength", "maxLength", "pattern", "enum"},
    "integer": _NUMERIC | {"enum"},
    "number": _NUMERIC,
    "boolean": set(),
    "array": {"items", "minItems", "maxItems", "uniqueItems"},
}
_DESCRIBED = {"type", "title", "description"}
#: Names a pydantic model cannot hold as fields (`inputs_model`).
_RESERVED = frozenset(name for name in dir(BaseModel) if not name.startswith("__"))


class InputsError(Exception):
    """The values do not fit the schema: one message per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def schema_problems(schema: Any, *, stored: bool = False) -> list[str]:
    """Everything wrong with ``schema`` as a flow's inputs; empty when it may be used.

    ``stored``: the schema of a version registered already, checked again as it starts.
    The two rules #2661 added are a registration's only: a stored `pattern` with no
    `maxLength` is capped as it runs (`_too_long_for_pattern`), and a stored required
    boolean is harmless (it is false when left out), so neither stops it starting."""
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
        problems += [f"{name}: {p}" for p in _property_problems(name, prop, stored=stored)]
    for name in schema.get("required", []):
        if name not in properties:
            problems.append(f"required names {name!r}, which is not an input")
        elif (
            not stored
            and isinstance(properties[name], dict)
            and properties[name].get("type") == "boolean"
        ):
            # Left out, a boolean is false (`_default`), so it is never missing (#2661).
            problems.append(f"required names {name!r}, a boolean, which is never missing")
    return problems


def _property_problems(name: str, prop: Any, *, stored: bool = False) -> list[str]:
    if not _NAME.match(name) or keyword.iskeyword(name):
        return ["a name is lower-case letters, digits and _, starting with a letter"]
    if name in _RESERVED:
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
        if not isinstance(items, dict) or items.get("type") not in _SCALARS:
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
            # Every start runs it: a pasted one is bounded.
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
            _Validator(prop).validate(prop["default"])
        except jsonschema.ValidationError as err:
            problems.append(f"default does not fit: {err.message}")
    return problems


def _pattern(
    validator: Any, pattern: str, instance: Any, schema: dict[str, Any]
) -> Iterator[jsonschema.ValidationError]:
    """jsonschema's `pattern`, run with a deadline: past PATTERN_TIMEOUT_S the value is
    refused rather than left to backtrack on the API's event loop (#2661)."""
    if not validator.is_type(instance, "string"):
        return
    try:
        found = regex.search(pattern, instance, timeout=PATTERN_TIMEOUT_S)
    except TimeoutError:
        yield jsonschema.ValidationError(
            f"is refused: its pattern took over {PATTERN_TIMEOUT_S}s to check it"
        )
        return
    except regex.error as err:
        yield jsonschema.ValidationError(f"its pattern does not compile: {err}")
        return
    if found is None:
        yield jsonschema.ValidationError(f"{instance!r} does not match {pattern!r}")


#: Draft 2020-12, its `pattern` run by `_pattern`.
_Validator = jsonschema.validators.extend(  # type: ignore[no-untyped-call]
    jsonschema.Draft202012Validator, {"pattern": _pattern}
)


def _python_type(prop: dict[str, Any]) -> Any:
    if prop["type"] == "array":
        return list[_python_type(prop["items"])]  # type: ignore[misc]
    if "enum" in prop:
        return Literal[tuple(prop["enum"])]
    return _SCALARS[prop["type"]]


def _default(prop: dict[str, Any]) -> tuple[bool, Any]:
    """Whether the input has a default, and it: a boolean without one is false, as the
    Start run form's unticked checkbox sends it."""
    if "default" in prop:
        return True, prop["default"]
    if prop.get("type") == "boolean":
        return True, False
    return False, None


def inputs_model(schema: dict[str, Any]) -> type[BaseModel]:
    """The schema as the model the stubs render to ``FlowInputs``: an input that is
    required or has a default (a boolean always has one, false) is always there; any
    other is ``T | None``, None when the run was not given it."""
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for name, prop in schema.get("properties", {}).items():
        kind = _python_type(prop)
        present = name in required or _default(prop)[0]
        fields[name] = (kind, ...) if present else (kind | None, None)
    # An input may be named `model_<x>`: pydantic's own namespace is not the script's (#2661).
    model: type[BaseModel] = create_model(
        INPUTS_TYPE, __config__=ConfigDict(protected_namespaces=()), **fields
    )
    model.__doc__ = schema.get("description") or "This run's inputs."
    return model


def _too_long_for_pattern(
    properties: dict[str, Any], given: dict[str, Any]
) -> dict[str, list[str]]:
    """Each input with a patterned string value longer than its maxLength, and its problems,
    named by where it is but never quoted. The limit is never above MAX_PATTERNED_LENGTH:
    a schema stored before #2661 may have no maxLength, or one far larger, and either
    would let the pattern run on any value."""
    found: dict[str, list[str]] = {}
    for name, prop in properties.items():
        if not isinstance(prop, dict) or name not in given:
            continue
        value = given[name]
        items = prop.get("items")
        checks = [(name, prop, value)]
        if isinstance(items, dict) and isinstance(value, list):
            checks = [(f"{name}.{i}", items, item) for i, item in enumerate(value)]
        for where, spec, one in checks:
            if "pattern" not in spec or not isinstance(one, str):
                continue
            limit = spec.get("maxLength")
            if not isinstance(limit, int) or isinstance(limit, bool):
                limit = MAX_PATTERNED_LENGTH
            limit = min(limit, MAX_PATTERNED_LENGTH)
            if len(one) > limit:
                found.setdefault(name, []).append(
                    f"{where}: is longer than its maxLength of {limit}"
                )
    return found


def _without_patterns(schema: dict[str, Any], names: Iterable[str]) -> dict[str, Any]:
    """``schema`` with those inputs' patterns left out (and their items'), so the
    validator still checks everything else about them."""
    properties = dict(schema.get("properties", {}))
    for name in names:
        prop = {k: v for k, v in properties[name].items() if k != "pattern"}
        if isinstance(prop.get("items"), dict):
            prop["items"] = {k: v for k, v in prop["items"].items() if k != "pattern"}
        properties[name] = prop
    return {**schema, "properties": properties}


def resolve_inputs(schema: dict[str, Any] | None, values: dict[str, Any] | None) -> dict[str, Any]:
    """A run's inputs: ``values`` with every default filled in and every input the run
    was not given set to None, checked against ``schema``. :class:`InputsError` when they
    do not fit; a flow with no schema takes none."""
    given = dict(values or {})
    if schema is None:
        if given:
            raise InputsError(["this flow takes no inputs"])
        return {}
    properties: dict[str, Any] = schema.get("properties", {})
    for name, prop in properties.items():
        has, default = _default(prop)
        if name not in given and has:
            given[name] = copy.deepcopy(default)
    # The validator evaluates every keyword, so `pattern` would still run on a value its
    # `maxLength` refuses, and maxLength is what bounds the pattern's work (#2661): an
    # over-long input is said here, and checked for everything but its pattern.
    too_long = _too_long_for_pattern(properties, given)
    checked = {**_without_patterns(schema, too_long), "additionalProperties": False}
    validator = _Validator(checked)
    problems = [p for said in too_long.values() for p in said] + [
        f"{'.'.join(str(p) for p in err.absolute_path) or 'inputs'}: {err.message}"
        for err in sorted(validator.iter_errors(given), key=lambda e: list(e.absolute_path))
        # Said above already, with its limit.
        if not (
            err.validator == "maxLength" and err.absolute_path and err.absolute_path[0] in too_long
        )
    ]
    if problems:
        raise InputsError(problems)
    # As the script's type says: an `integer` sent as 3.0 is 3, a `number` sent as 2 is 2.0.
    try:
        return inputs_model(schema).model_validate(given).model_dump()
    except ValidationError as err:
        raise InputsError(
            [f"{'.'.join(str(p) for p in e['loc']) or 'inputs'}: {e['msg']}" for e in err.errors()]
        ) from None
