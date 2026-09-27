from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeGuard

from pydantic import BaseModel, Field

from scadbuddy.render.colours import CSS_COLOURS

ParameterType = Literal[
    "number", "integer", "string", "boolean", "select", "color", "font", "slider", "file"
]
ParamValue = bool | int | float | str

HIDDEN_GROUP = "Hidden"
GLOBAL_GROUP = "Global"

#: What a `// file` parameter can take (#204): an SVG for `import()`, a PNG for
#: `surface()`. The upload route accepts exactly these, sniffed from the content.
FILE_KINDS: tuple[str, ...] = ("svg", "png")

#: What a `file` parameter may hand OpenSCAD: a bare name in the model's directory,
#: never a path. Uploads are staged under names that match (#204), and a template's
#: own sample files are offered only when theirs do. No leading dot, so a dotfile is
#: never one.
_BARE_FILENAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}")


def is_bare_filename(name: str) -> bool:
    return _BARE_FILENAME.fullmatch(name) is not None and ".." not in name


_ANNOTATION_RE = re.compile(
    r"^[^\S\n]*(?P<name>[A-Za-z_]\w*)[^\S\n]*=[^;\n]*;[^\S\n]*//[^\S\n]*"
    r"(?P<kind>color|font|file)\b"
    # `// file:svg,png` -- the extensions a file parameter accepts. OpenSCAD's own
    # customizer reads the whole comment as a (non-matching) hint and keeps the
    # parameter a plain string, measured on 2026.09.23.
    r"(?:[^\S\n]*:(?P<accept>[A-Za-z0-9.,\t ]*))?",
    re.MULTILINE,
)

#: Bumped when the derived schema changes shape for an UNCHANGED source, so a cache
#: entry written by an older ScadBuddy is re-derived rather than served. 2: `file`.
SCHEMA_FORMAT = 2


@dataclass(frozen=True)
class Annotation:
    kind: str
    accept: tuple[str, ...] = ()


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
    #: A `file` parameter's accepted kinds, from `// file:svg,png` (#204).
    accept: list[str] = Field(default_factory=list)
    #: A `file` parameter's sample files: the bare names of the files in the model's
    #: own directory whose extension it accepts. Listed when the schema is served,
    #: never cached, since a sample can change without the source changing.
    samples: list[str] = Field(default_factory=list)


class CustomizerSchema(BaseModel):
    title: str | None = None
    source_sha256: str = ""
    groups: list[str] = Field(default_factory=list)
    parameters: list[Parameter] = Field(default_factory=list)


#: Bumped when `build_schema` derives something new from the same source, so an entry
#: written by an older derivation misses. 2: CSS-name colour defaults as hex (#187).
SCHEMA_CACHE_VERSION = 2


def source_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _accepted(raw: str | None) -> tuple[str, ...]:
    """The kinds a `// file` annotation names, in FILE_KINDS order. A bare `// file`
    takes every kind; one that names only kinds ScadBuddy cannot take takes none."""
    if raw is None or not raw.strip():
        return FILE_KINDS
    named = {part.strip().lower().lstrip(".") for part in raw.split(",")}
    return tuple(kind for kind in FILE_KINDS if kind in named)


def find_annotations(source: str) -> dict[str, Annotation]:
    return {
        m["name"]: Annotation(
            kind=m["kind"], accept=_accepted(m["accept"]) if m["kind"] == "file" else ()
        )
        for m in _ANNOTATION_RE.finditer(source)
    }


def _is_whole(value: Any) -> TypeGuard[int | float]:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    return float(value).is_integer()


def _resolve_type(raw: dict[str, Any], annotation: Annotation | None) -> ParameterType:
    declared = raw.get("type")
    if declared == "boolean":
        return "boolean"
    if raw.get("options"):
        return "select"
    if declared == "string":
        kind = annotation.kind if annotation else None
        if kind == "color":
            return "color"
        if kind == "font":
            return "font"
        # An annotation naming no kind ScadBuddy can store (`// file:stl`) stays a
        # plain string rather than a picker nothing can be dropped on.
        if kind == "file" and annotation is not None and annotation.accept:
            return "file"
        return "string"
    if raw.get("min") is not None and raw.get("max") is not None:
        return "slider"
    if _is_whole(raw.get("step", 1)) and _is_whole(raw.get("initial")):
        return "integer"
    return "number"


def _normalise_parameter(raw: dict[str, Any], annotations: dict[str, Annotation]) -> Parameter:
    name = str(raw["name"])
    annotation = annotations.get(name)
    resolved = _resolve_type(raw, annotation)
    initial = raw.get("initial")
    if resolved == "integer" and _is_whole(initial):
        initial = int(initial)
    # #187: a CSS-name default is served as the hex OpenSCAD renders it as, so the
    # frontend's colour widget and extruder numbering never need the name table.
    if resolved == "color" and isinstance(initial, str):
        initial = CSS_COLOURS.get(initial.strip().lower(), initial)
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
        accept=list(annotation.accept) if resolved == "file" and annotation else [],
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
    if body.get("version") != SCHEMA_CACHE_VERSION:
        return None
    # Absent on an entry written before #93, which had no libraries.
    if body.get("library_path", []) != [str(path) for path in library_path]:
        return None
    # Absent on an entry written before #204, whose `// file` parameters it typed
    # as plain strings.
    if body.get("format", 1) != SCHEMA_FORMAT:
        return None
    return CustomizerSchema.model_validate(cached)


def store_cached_schema(
    cache_path: Path, schema: CustomizerSchema, *, library_path: Sequence[Path] = ()
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "version": SCHEMA_CACHE_VERSION,
        "schema": schema.model_dump(mode="json"),
        "library_path": [str(path) for path in library_path],
        "format": SCHEMA_FORMAT,
    }
    cache_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
