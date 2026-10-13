"""A mesh model's scene (spec 2026-10-11 §3.1): objects, each its colour parts, then
its ops in the mesh's own frame, then its transform. ``scene.json`` is the source of
truth; ``model.scad`` is compiled from it (`compile.py`)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCENE_NAME = "scene.json"
MESHES_DIR = "meshes"
#: What a generated module or helper is named; an exposed parameter may not shadow one.
#: Nor an OpenSCAD keyword or constant, which would not parse as an assignment's name.
KEYWORDS = (
    "if",
    "else",
    "for",
    "let",
    "each",
    "module",
    "function",
    "include",
    "use",
    "assert",
    "echo",
    "intersection_for",
    "true",
    "false",
    "undef",
    "PI",
)
#: Every pattern ends ``\Z``, not ``$``: under python-re ``$`` also matches before a
#: trailing newline, so ``"x\n"`` would pass as ``x``.
RESERVED = r"(?!o[0-9]+(?:_|\Z))(?!sb_)" + rf"(?!(?:{'|'.join(KEYWORDS)})\Z)"
IDENT = rf"^{RESERVED}[A-Za-z_][A-Za-z0-9_]*\Z"
COLOUR = r"^#[0-9A-F]{6}\Z"
MESH_PATH = r"^meshes/o[0-9]+-p[0-9]+\.3mf\Z"
OBJECT_ID = r"^o[0-9]+\Z"
PART_ID = r"^p[0-9]+\Z"
MAX_OBJECTS = 200
MAX_PARTS = 64
MAX_NAME = 200
#: A source file's name, as long as a filesystem name may be (NAME_MAX).
MAX_SOURCE_NAME = 255
MAX_OPS = 100
#: `section.cut_points` is cubic in an object's cuts.
MAX_CUTS = 16


class _Strict(BaseModel):
    # python-re: `IDENT`'s lookaheads, which the default Rust engine does not support.
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, frozen=True, regex_engine="python-re"
    )


class Exposed(_Strict):
    """A number the customizer sets: ``expose`` is the parameter's name."""

    expose: str = Field(pattern=IDENT, max_length=64)
    value: float


Num = float | Exposed
Vec3 = tuple[Num, Num, Num]
Point = tuple[float, float, float]


def value(n: Num) -> float:
    return n.value if isinstance(n, Exposed) else n


class Transform(_Strict):
    """OpenSCAD's ``translate(translate) rotate(rotate) scale(scale)``; degrees."""

    translate: Vec3 = (0.0, 0.0, 0.0)
    rotate: Vec3 = (0.0, 0.0, 0.0)
    scale: Vec3 = (1.0, 1.0, 1.0)


class Part(_Strict):
    id: str = Field(pattern=PART_ID)
    mesh: str = Field(pattern=MESH_PATH)
    colour: str = Field(pattern=COLOUR)
    #: Painted in Bambu Studio; its codes are ``<mesh stem>.paint.json`` (spec §7).
    painted: bool = False
    #: A Bambu Studio negative part (``ColourPart.negative``, #2337): cut out of the
    #: object's other parts and never printed. A split's section has its hole taken out
    #: (`measure`'s ``holes``); nothing else measures, colours or pegs it.
    negative: bool = False


class Cut(_Strict):
    op: Literal["cut"] = "cut"
    point: Vec3
    normal: Point
    keep: Literal["above", "below"]


class Joint(_Strict):
    """A peg (added) or socket (subtracted) on a cut face. ``axis`` points from the cut
    face into the part the socket is in, for both halves, so both use one frame."""

    op: Literal["joint"] = "joint"
    at: Point
    axis: Point
    diameter: Num
    depth: Num
    clearance: Num
    key: Literal["none", "flat", "hex"] = "flat"
    role: Literal["peg", "socket"]
    #: A peg's colour: the part's the cut went through, so the peg prints with the
    #: piece it stands on. None: the object's first part's.
    colour: str | None = Field(default=None, pattern=COLOUR)


class Primitive(_Strict):
    """cube: ``size`` is x, y, z; cylinder: diameter ``size[0]``, height ``size[2]``;
    sphere: diameter ``size[0]``. Centred, then placed by ``transform``."""

    primitive: Literal["cube", "cylinder", "sphere"]
    size: Vec3
    transform: Transform = Transform()


class Add(Primitive):
    op: Literal["add"] = "add"
    #: Its colour: None is the object's first printed part's. Either way the shape takes
    #: over the space it fills from every other piece (the carrot's green anchored into
    #: the orange body, #2146), so no volume has two colours.
    colour: str | None = Field(default=None, pattern=COLOUR)


class Subtract(Primitive):
    op: Literal["subtract"] = "subtract"


class Custom(_Strict):
    """OpenSCAD given the geometry so far as ``children()``."""

    op: Literal["custom"] = "custom"
    scad: str = Field(min_length=1, max_length=20_000)


Op = Annotated[Cut | Joint | Add | Subtract | Custom, Field(discriminator="op")]


class SceneObject(_Strict):
    id: str = Field(pattern=OBJECT_ID)
    name: str = Field(min_length=1, max_length=MAX_NAME)
    parts: list[Part] = Field(min_length=1, max_length=MAX_PARTS)
    ops: list[Op] = Field(default_factory=list, max_length=MAX_OPS)
    transform: Transform = Transform()

    @model_validator(mode="after")
    def _few_cuts(self) -> SceneObject:
        cuts = sum(isinstance(op, Cut) for op in self.ops)
        if cuts > MAX_CUTS:
            raise ValueError(f"{self.id} has {cuts} cuts; at most {MAX_CUTS} are allowed")
        return self


class SceneSource(_Strict):
    kind: Literal["bambuddy_library", "upload", "output"]
    name: str = Field(min_length=1, max_length=MAX_SOURCE_NAME)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}\Z")
    file_id: int | None = None
    output_id: str | None = None
    licence: str | None = None


class Scene(_Strict):
    version: Literal[1] = 1
    #: sha256 of the ``model.scad`` text last compiled from this scene (spec §5).
    generated_sha256: str = ""
    source: SceneSource
    objects: list[SceneObject] = Field(min_length=1, max_length=MAX_OBJECTS)
    #: What reading the source dropped or could not carry (modifiers, paint).
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> Scene:
        ids = [o.id for o in self.objects]
        if len(ids) != len(set(ids)):
            raise ValueError("object ids repeat")
        exposed_values(self)  # raises for one name with two values
        return self


def _exposed(item: object) -> Iterator[Exposed]:
    if isinstance(item, Exposed):
        yield item
    elif isinstance(item, BaseModel):
        for name in type(item).model_fields:
            yield from _exposed(getattr(item, name))
    elif isinstance(item, list | tuple):
        for each in item:
            yield from _exposed(each)


def exposed_values(model: BaseModel) -> dict[str, float]:
    """Every exposed parameter, first seen first. A name may repeat (both halves of a
    split share their parent's), but only with one value."""
    found: dict[str, float] = {}
    for item in _exposed(model):
        if found.setdefault(item.expose, item.value) != item.value:
            raise ValueError(f"the parameter {item.expose!r} has two values")
    return found


def named(name: str, suffix: str = "") -> str:
    """``name`` then ``suffix``, the name cut short to keep the whole within
    :data:`MAX_NAME`: a split appends " (bottom)" or " (top)" each time."""
    return name[: MAX_NAME - len(suffix)] + suffix


def source_name(name: str) -> str:
    """``name`` cut short to :data:`MAX_SOURCE_NAME`, keeping its extension: a file a
    client uploads or Bambuddy lists may be named at any length (#2632)."""
    if len(name) <= MAX_SOURCE_NAME:
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot or len(extension) >= MAX_SOURCE_NAME // 2:
        return name[:MAX_SOURCE_NAME]
    return f"{stem[: MAX_SOURCE_NAME - len(extension) - 1]}.{extension}"


def mesh_files(scene: Scene) -> set[str]:
    """The files under :data:`MESHES_DIR` the scene uses: each part's mesh and, beside a
    painted one, its paint codes. Anything else there is left over from an edit."""
    used: set[str] = set()
    for obj in scene.objects:
        for part in obj.parts:
            used.add(part.mesh)
            if part.painted:
                used.add(part.mesh.removesuffix(".3mf") + ".paint.json")
    return used


def next_object_id(scene: Scene) -> str:
    return f"o{max(int(o.id[1:]) for o in scene.objects) + 1}"


def load_scene(text: str) -> Scene:
    return Scene.model_validate_json(text)


def dump_scene(scene: Scene) -> str:
    return scene.model_dump_json(indent=2) + "\n"


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
