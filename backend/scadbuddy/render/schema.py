from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, TypeGuard

from pydantic import BaseModel, Field

ParameterType = Literal[
    "number", "integer", "string", "boolean", "select", "color", "font", "slider"
]
ParamValue = bool | int | float | str

HIDDEN_GROUP = "Hidden"
GLOBAL_GROUP = "Global"

_ANNOTATION_RE = re.compile(
    r"^[^\S\n]*(?P<name>[A-Za-z_]\w*)[^\S\n]*=[^;\n]*;[^\S\n]*//[^\S\n]*(?P<kind>color|font)\b",
    re.MULTILINE,
)


class Option(BaseModel):
    name: str
    value: ParamValue


class Parameter(BaseModel):
    name: str
    type: ParameterType
    initial: ParamValue | None = None
    caption: str | None = None
    group: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    max_length: int | None = None
    options: list[Option] = Field(default_factory=list)


class CustomizerSchema(BaseModel):
    title: str | None = None
    source_sha256: str = ""
    groups: list[str] = Field(default_factory=list)
    parameters: list[Parameter] = Field(default_factory=list)


def source_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def find_annotations(source: str) -> dict[str, str]:
    return {m["name"]: m["kind"] for m in _ANNOTATION_RE.finditer(source)}


def _is_whole(value: Any) -> TypeGuard[int | float]:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    return float(value).is_integer()


def _resolve_type(raw: dict[str, Any], annotation: str | None) -> ParameterType:
    declared = raw.get("type")
    if declared == "boolean":
        return "boolean"
    if raw.get("options"):
        return "select"
    if declared == "string":
        if annotation == "color":
            return "color"
        if annotation == "font":
            return "font"
        return "string"
    if raw.get("min") is not None and raw.get("max") is not None:
        return "slider"
    if _is_whole(raw.get("step", 1)) and _is_whole(raw.get("initial")):
        return "integer"
    return "number"


def _normalise_parameter(raw: dict[str, Any], annotations: dict[str, str]) -> Parameter:
    name = str(raw["name"])
    resolved = _resolve_type(raw, annotations.get(name))
    initial = raw.get("initial")
    if resolved == "integer" and _is_whole(initial):
        initial = int(initial)
    return Parameter(
        name=name,
        type=resolved,
        initial=initial,
        caption=raw.get("caption"),
        group=str(raw.get("group", "")),
        min=raw.get("min"),
        max=raw.get("max"),
        # Only a declared range carries a real step. OpenSCAD 2026.09.23 writes
        # `step: 1` on every un-ranged number (2026.01.19 omitted it); that is
        # the customizer's default, and passing it through would put an HTML
        # `step=1` on a value like 1.2.
        step=raw.get("step") if resolved == "slider" else None,
        max_length=raw.get("maxLength"),
        options=[Option(name=str(o["name"]), value=o["value"]) for o in raw.get("options", [])],
    )


def build_schema(param_json: dict[str, Any], source: str) -> CustomizerSchema:
    annotations = find_annotations(source)
    parameters: list[Parameter] = []
    groups: list[str] = []
    for raw in param_json.get("parameters", []):
        parameter = _normalise_parameter(raw, annotations)
        if parameter.group == HIDDEN_GROUP:
            continue
        parameters.append(parameter)
        if parameter.group not in (*groups, GLOBAL_GROUP, ""):
            groups.append(parameter.group)
    return CustomizerSchema(
        title=param_json.get("title"),
        source_sha256=source_sha256(source),
        groups=groups,
        parameters=parameters,
    )


def load_cached_schema(
    cache_path: Path, expected_sha: str, *, library_path: Sequence[Path] = ()
) -> CustomizerSchema | None:
    """Read a derived schema back, or ``None`` when it does not match the source.

    ``cache_path`` is a file under ``data/cache`` (`SCHEMA_CACHE_NAME`), never the
    model's ``model.json``: this is derived, it is written by a read, and it must
    not land in the versioned models repository.

    ``library_path`` is part of the key (#93): the same source derives against
    whatever its libraries define, and each checkout on it is named by the commit
    it is pinned to -- so a re-pin, a changed declaration and a restore of old pins
    all miss here, without anything having to find and drop the entry.
    """
    if not cache_path.is_file():
        return None
    body = json.loads(cache_path.read_text(encoding="utf-8"))
    cached = body.get("schema")
    if not isinstance(cached, dict) or cached.get("source_sha256") != expected_sha:
        return None
    # Absent on an entry written before #93, which had no libraries.
    if body.get("library_path", []) != [str(path) for path in library_path]:
        return None
    return CustomizerSchema.model_validate(cached)


def store_cached_schema(
    cache_path: Path, schema: CustomizerSchema, *, library_path: Sequence[Path] = ()
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "schema": schema.model_dump(mode="json"),
        "library_path": [str(path) for path in library_path],
    }
    cache_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
