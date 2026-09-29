"""Template inputs (spec 2026-09-27 §4.3): the one piece of customizer state.

A JSON object the template owns. ``params`` is the reserved key the default pipeline
renders: the `-D` values of ``model.scad``. ``v`` is the template's inputs version,
0 when the template declares none (phase 4's ``migrate`` reads it). Every other key
is the template UI's own state: stored with presets and outputs, never rendered.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from scadbuddy.render.schema import ParamValue

#: A preset or output holds a few KB of state; this is far past any real UI's and
#: keeps one request from filling a jsonb column or an output directory.
MAX_INPUTS_BYTES = 65536


class InputsError(ValueError):
    """Inputs that are not a template's inputs; the message names the key."""


def legacy_inputs(params: Mapping[str, ParamValue]) -> dict[str, Any]:
    """What a params-only record (a pre-inputs preset, output or request) reads as."""
    return {"params": dict(params), "v": 0}


def normalize_inputs(
    inputs: Mapping[str, Any] | None, params: Mapping[str, ParamValue] | None
) -> dict[str, Any]:
    if inputs is None:
        return legacy_inputs(params or {})
    result = dict(inputs)
    raw = result.get("params", {})
    if not isinstance(raw, dict):
        raise InputsError("inputs.params must be an object of parameter values")
    checked: dict[str, ParamValue] = {}
    for name, value in raw.items():
        if not isinstance(value, bool | int | float | str):
            raise InputsError(f"inputs.params.{name} must be a number, string or boolean")
        checked[name] = value
    if params and dict(params) != checked:
        raise InputsError("params and inputs.params disagree; send inputs only")
    result["params"] = checked
    version = result.setdefault("v", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise InputsError("inputs.v must be a non-negative integer")
    size = len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > MAX_INPUTS_BYTES:
        raise InputsError(f"inputs are {size} bytes; at most {MAX_INPUTS_BYTES}")
    return result
