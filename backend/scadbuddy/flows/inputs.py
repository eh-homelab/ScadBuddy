"""A flow's typed inputs (#2483): a JSON Schema versioned with its script, as
``<name>/inputs.json`` in ``data/flows``, in the same commit.

The schema is the closed subset of `scadbuddy.core.input_schema` (shared with pipeline
templates, #5701), without objects, so that each input has one Python type the type
check can hold a script to, and one control the Start run form can draw.

It is checked when a version is registered (`input_schema.schema_problems`, then the script's
type check against :func:`inputs_model`), and a run's values when it starts
(:func:`resolve_inputs`: validated, defaults filled in). The script reads them with
``inputs()``, typed as the ``FlowInputs`` TypedDict the model renders to.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Iterable
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from scadbuddy.core.input_schema import (
    MAX_PATTERNED_LENGTH,
    PATTERNS_BUDGET_S,
    SCALARS,
    Validator,
    patterns_deadline,
)

INPUTS_FILE: Final = "inputs.json"
#: The type the script's `inputs()` returns, in the stubs and the editor's help.
INPUTS_TYPE = "FlowInputs"


class InputsError(Exception):
    """The values do not fit the schema: one message per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _python_type(prop: dict[str, Any]) -> Any:
    if prop["type"] == "array":
        return list[_python_type(prop["items"])]  # type: ignore[misc]
    if "enum" in prop:
        return Literal[tuple(prop["enum"])]
    return SCALARS[prop["type"]]


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
    token = patterns_deadline.set(time.monotonic() + PATTERNS_BUDGET_S)
    try:
        return _resolve_inputs(schema, values)
    finally:
        patterns_deadline.reset(token)


def _resolve_inputs(schema: dict[str, Any] | None, values: dict[str, Any] | None) -> dict[str, Any]:
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
    validator = Validator(checked)
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
