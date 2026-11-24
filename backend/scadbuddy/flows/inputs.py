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
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from scadbuddy.core.input_schema import (
    PATTERNS_BUDGET_S,
    SCALARS,
    Validator,
    patterns_deadline,
    too_long_for_pattern,
    without_patterns,
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


def resolve_inputs(schema: dict[str, Any] | None, values: dict[str, Any] | None) -> dict[str, Any]:
    """A run's inputs: ``values`` with every default filled in and every input the run
    was not given set to None, checked against ``schema``. :class:`InputsError` when they
    do not fit; a flow with no schema takes none."""
    token = patterns_deadline.set(time.monotonic() + PATTERNS_BUDGET_S)
    try:
        return _resolve_inputs(schema, values)
    finally:
        patterns_deadline.reset(token)


def filled(schema: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """``values`` as a script gets them, with every default filled in and every property
    left out set to None. ``values`` fit ``schema`` already: nothing is checked against it,
    so no pattern runs and a workflow may call this. :class:`InputsError` when the model
    does not take one."""
    return _typed(schema, _with_defaults(schema, values))


def _with_defaults(schema: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    given = dict(values)
    for name, prop in schema.get("properties", {}).items():
        has, default = _default(prop)
        if name not in given and has:
            given[name] = copy.deepcopy(default)
    return given


def _typed(schema: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """As the script's type says: an `integer` sent as 3.0 is 3, a `number` sent as 2 is 2.0."""
    try:
        return inputs_model(schema).model_validate(given).model_dump()
    except ValidationError as err:
        raise InputsError(
            [f"{'.'.join(str(p) for p in e['loc']) or 'inputs'}: {e['msg']}" for e in err.errors()]
        ) from None


def _resolve_inputs(schema: dict[str, Any] | None, values: dict[str, Any] | None) -> dict[str, Any]:
    if schema is None:
        if values:
            raise InputsError(["this flow takes no inputs"])
        return {}
    given = _with_defaults(schema, values or {})
    properties: dict[str, Any] = schema.get("properties", {})
    # The validator evaluates every keyword, so `pattern` would still run on a value its
    # `maxLength` refuses, and maxLength is what bounds the pattern's work (#2661): an
    # over-long input is said here, and checked for everything but its pattern.
    too_long = too_long_for_pattern(properties, given)
    checked = {**without_patterns(schema, too_long), "additionalProperties": False}
    validator = Validator(checked)
    problems = [
        f"{'.'.join(str(p) for p in path)}: is longer than its maxLength of {limit}"
        for said in too_long.values()
        for path, limit in said
    ] + [
        f"{'.'.join(str(p) for p in err.absolute_path) or 'inputs'}: {err.message}"
        for err in sorted(validator.iter_errors(given), key=lambda e: list(e.absolute_path))
        # Said above already, with its limit.
        if not (
            err.validator == "maxLength" and err.absolute_path and err.absolute_path[0] in too_long
        )
    ]
    if problems:
        raise InputsError(problems)
    return _typed(schema, given)
