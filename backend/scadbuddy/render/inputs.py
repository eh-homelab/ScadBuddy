"""Template inputs (spec 2026-09-27 §4.3): the one piece of customizer state.

A JSON object the template owns. ``params`` is the reserved key the default pipeline
renders: the `-D` values of ``model.scad``. ``v`` is the template's inputs version,
0 when the template declares none (phase 4's ``migrate`` reads it). Every other key
is the template UI's own state: stored with presets and outputs, never rendered.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from scadbuddy.render.schema import ParamValue

#: A preset or output holds a few KB of state; this is far past any real UI's and
#: keeps one request from filling a jsonb column or an output directory.
MAX_INPUTS_BYTES = 65536


class InputsError(ValueError):
    """Inputs that are not a template's inputs; the message names the key."""


class InputsDisagreeError(InputsError):
    """``params`` sent beside ``inputs`` that are not ``inputs.params``."""


@dataclass(frozen=True)
class NormalizedInputs:
    """Checked inputs: the whole object, and its `params` with their type kept for
    the checks and the render that take `Mapping[str, ParamValue]`."""

    #: The JSON object to store and echo; ``data["params"]`` is ``params``.
    data: dict[str, Any]
    params: dict[str, ParamValue]


def legacy_inputs(params: Mapping[str, ParamValue]) -> dict[str, Any]:
    """What a params-only record (a pre-inputs preset, output or request) reads as."""
    return {"params": dict(params), "v": 0}


def _nul_at(value: Any, path: str) -> str | None:
    """The first key or string under ``value`` holding a NUL, by path: Postgres can store
    neither in text nor in jsonb (#965)."""
    if isinstance(value, str):
        return path if "\x00" in value else None
    if isinstance(value, dict):
        for key, item in value.items():
            if "\x00" in key:
                return f"{path}.{key!r}"
            found = _nul_at(item, f"{path}.{key}")
            if found is not None:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _nul_at(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _typed(params: Mapping[str, ParamValue]) -> dict[str, tuple[type[object], ParamValue]]:
    return {name: (type(value), value) for name, value in params.items()}


def normalize_inputs(
    inputs: Mapping[str, Any] | None, params: Mapping[str, ParamValue] | None
) -> NormalizedInputs:
    if inputs is None:
        # The params-only body is these inputs, and takes the same checks: an
        # integer parameter at Infinity would otherwise reach `int()` downstream.
        inputs, params = legacy_inputs(params or {}), None
    result = dict(inputs)
    raw = result.get("params", {})
    if not isinstance(raw, dict):
        raise InputsError("inputs.params must be an object of parameter values")
    checked: dict[str, ParamValue] = {}
    for name, value in raw.items():
        if not isinstance(value, bool | int | float | str):
            raise InputsError(f"inputs.params.{name} must be a number, string or boolean")
        if "\x00" in name or (isinstance(value, str) and "\x00" in value):
            raise InputsError(f"parameter {name!r} contains a NUL byte")
        checked[name] = value
    result["params"] = checked
    nul = _nul_at(result, "inputs")
    if nul is not None:
        raise InputsError(f"{nul} contains a NUL byte")
    version = result.setdefault("v", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise InputsError("inputs.v must be a non-negative integer")
    try:
        encoded = json.dumps(result, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError:
        raise InputsError("inputs must be JSON: no NaN or Infinity") from None
    size = len(encoded.encode("utf-8"))
    if size > MAX_INPUTS_BYTES:
        raise InputsError(f"inputs are {size} bytes; at most {MAX_INPUTS_BYTES}")
    # Type and value: True must not agree with 1. An empty `params` beside
    # `inputs` is not a claim about them, so it is not checked. After the JSON
    # check: NaN never equals itself, and the message must say NaN, not disagree.
    if params and _typed(params) != _typed(checked):
        raise InputsDisagreeError("params and inputs.params disagree; send inputs only")
    return NormalizedInputs(data=result, params=checked)


def inputs_key(slug: str, inputs: Mapping[str, Any], model_version: str | None) -> str:
    """The job key of a pipeline template (§3.4): its whole inputs, which the pipeline
    reads, not only ``params``."""
    raw = json.dumps(
        ["inputs", slug, model_version, dict(inputs)], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def arrange_key(slug: str, inputs: Mapping[str, Any]) -> str:
    """An arrange job's key: identical requests coalesce like renders (§3.3)."""
    raw = json.dumps(["arrange", slug, dict(inputs)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
