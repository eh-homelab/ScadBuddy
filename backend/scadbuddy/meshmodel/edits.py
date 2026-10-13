"""Scene edits (spec 2026-10-11 §5): what callers send, applied in order to the scene
so far. Planes and points arrive in world coordinates and are stored in the object's
own frame (`frames.to_object_plane`). Measuring reads the part meshes through
``mesh_of``; everything it measures is stored in the ops, so compiling stays pure."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Literal, cast

import numpy as np
import trimesh
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.meshmodel.frames import (
    euler,
    frame,
    linear,
    normal_to_world,
    rotation,
    to_object_plane,
    turn_to_down,
    unit,
)
from scadbuddy.meshmodel.scene import (
    IDENT,
    MAX_CUTS,
    MAX_NAME,
    OBJECT_ID,
    PART_ID,
    Add,
    Custom,
    Cut,
    Exposed,
    Joint,
    Num,
    Op,
    Point,
    Scene,
    SceneObject,
    Subtract,
    Transform,
    Vec3,
    exposed_values,
    named,
    next_object_id,
    value,
)
from scadbuddy.meshmodel.section import OpenSectionError, cut_points, kept, measure

#: Two 0.4 mm walls around a socket (spec §5).
WALL = 0.8
MeshOf = Callable[[str], trimesh.Trimesh]


class SceneEditError(ValueError):
    """An edit the scene refuses; the route's 422."""


#: A colour as callers may send it: either case (`Part.colour` stores it upper case).
COLOUR_IN = r"^#[0-9A-Fa-f]{6}\Z"
#: An op's field, a tuple's element by ``.index``.
FIELD = r"^[a-z_]+(\.[0-2])?\Z"


class _Edit(BaseModel):
    # python-re, as `scene._Strict`: `IDENT`'s lookaheads, and ``\Z`` (scene.py).
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, regex_engine="python-re")
    object: str = Field(pattern=OBJECT_ID)


class JointSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    diameter: float | None = Field(default=None, gt=0, le=100)
    depth: float = Field(default=6.0, gt=0, le=100)
    clearance: float = Field(default=0.2, ge=0, le=2)
    key: Literal["none", "flat", "hex"] = "flat"
    peg_on: Literal["above", "below"] = "above"


class TransformEdit(_Edit):
    edit: Literal["transform"] = "transform"
    translate: Vec3 | None = None
    rotate: Vec3 | None = None
    scale: Vec3 | None = None


class SplitEdit(_Edit):
    edit: Literal["split_at_plane"] = "split_at_plane"
    point: Point
    normal: Point
    joint: JointSpec | None = None


class LayFlatEdit(_Edit):
    edit: Literal["lay_flat"] = "lay_flat"
    normal: Point | Literal["largest_flat"]


class _PrimitiveEdit(_Edit):
    primitive: Literal["cube", "cylinder", "sphere"]
    size: Vec3
    #: The primitive's centre in world coordinates; the object's origin when omitted.
    at: Point | None = None
    #: Its rotation in the object's own frame.
    rotate: Vec3 = (0.0, 0.0, 0.0)


class AddEdit(_PrimitiveEdit):
    edit: Literal["add"] = "add"
    #: See `Add.colour`.
    colour: str | None = Field(default=None, pattern=COLOUR_IN)


class SubtractEdit(_PrimitiveEdit):
    edit: Literal["subtract"] = "subtract"


class CustomEdit(_Edit):
    edit: Literal["custom"] = "custom"
    scad: str = Field(min_length=1, max_length=20_000)


class SetColourEdit(_Edit):
    edit: Literal["set_colour"] = "set_colour"
    part: str = Field(pattern=PART_ID)
    colour: str = Field(pattern=COLOUR_IN)


class DeleteEdit(_Edit):
    edit: Literal["delete"] = "delete"


class DuplicateEdit(_Edit):
    edit: Literal["duplicate"] = "duplicate"
    offset: Point = (20.0, 0.0, 0.0)


class RenameEdit(_Edit):
    edit: Literal["rename"] = "rename"
    name: str = Field(min_length=1, max_length=MAX_NAME)


class RemoveOpEdit(_Edit):
    edit: Literal["remove_op"] = "remove_op"
    op: int = Field(ge=0)


class ExposeEdit(_Edit):
    edit: Literal["expose"] = "expose"
    #: The op's index, or None for the object's transform.
    op: int | None = Field(ge=0)
    #: A field of it, a tuple's element by ``.index``: ``point.2``, ``diameter``.
    field: str = Field(pattern=FIELD)
    name: str = Field(pattern=IDENT, max_length=64)


class UnexposeEdit(_Edit):
    edit: Literal["unexpose"] = "unexpose"
    op: int | None = Field(ge=0)
    field: str = Field(pattern=FIELD)


Edit = Annotated[
    TransformEdit
    | SplitEdit
    | LayFlatEdit
    | AddEdit
    | SubtractEdit
    | CustomEdit
    | SetColourEdit
    | DeleteEdit
    | DuplicateEdit
    | RenameEdit
    | RemoveOpEdit
    | ExposeEdit
    | UnexposeEdit,
    Field(discriminator="edit"),
]


def _floats(v: Sequence[Num]) -> list[float]:
    return [value(x) for x in v]


def _cuts(
    obj: SceneObject,
) -> list[tuple[Sequence[float], Sequence[float], Literal["above", "below"]]]:
    return [(_floats(op.point), op.normal, op.keep) for op in obj.ops if isinstance(op, Cut)]


def object_mesh(obj: SceneObject, mesh_of: MeshOf) -> trimesh.Trimesh:
    """The object's printed parts as one mesh in its own frame, before its ops (its
    negative parts print nothing, so a section, a flat face or a drop never counts
    them)."""
    printed = [mesh_of(part.mesh) for part in obj.parts if not part.negative]
    if not printed:
        raise SceneEditError(f"{obj.id} has only negative parts, so nothing of it prints")
    merged = trimesh.util.concatenate(printed)
    assert isinstance(merged, trimesh.Trimesh)
    return merged


def transform_matrix(t: Transform) -> tuple[np.ndarray, np.ndarray]:
    return linear(_floats(t.rotate), _floats(t.scale)), np.asarray(_floats(t.translate))


def _needs_plane(obj: SceneObject, what: str) -> None:
    """A zero scale component flattens the object, so no plane maps into its frame."""
    if any(value(s) == 0 for s in obj.transform.scale):
        raise SceneEditError(f"{obj.id} has a zero scale, so it has no {what}")


def _find(scene: Scene, oid: str) -> int:
    for index, obj in enumerate(scene.objects):
        if obj.id == oid:
            return index
    raise SceneEditError(f"there is no object {oid} in the scene")


def _replace(scene: Scene, index: int, *objects: SceneObject) -> Scene:
    return scene.model_copy(
        update={"objects": [*scene.objects[:index], *objects, *scene.objects[index + 1 :]]}
    )


def _holes(obj: SceneObject, mesh_of: MeshOf) -> list[trimesh.Trimesh]:
    """The object's negative parts: printed nowhere, but holes in every section."""
    return [mesh_of(part.mesh) for part in obj.parts if part.negative]


def _section_area(
    obj: SceneObject, mesh: trimesh.Trimesh, point: np.ndarray, normal: np.ndarray
) -> float:
    """A part's section at the plane, where the object's earlier cuts keep it: a part cut
    away above the plane is no part a peg can stand on. Measured without the object's
    negative parts: a hole shifts every part's area alike, so it cannot change which
    part is widest; only the joint's own fit check must see it."""
    found = measure(mesh, point.tolist(), normal.tolist(), cuts=_cuts(obj))
    return 0.0 if found is None else found.area


def _split(scene: Scene, edit: SplitEdit, mesh_of: MeshOf) -> Scene:
    index = _find(scene, edit.object)
    obj = scene.objects[index]
    t = obj.transform
    _needs_plane(obj, "plane to cut")
    try:
        point, normal = to_object_plane(
            _floats(t.translate), _floats(t.rotate), _floats(t.scale), edit.point, edit.normal
        )
    except ValueError as error:
        raise SceneEditError(str(error)) from None
    if sum(isinstance(op, Cut) for op in obj.ops) >= MAX_CUTS:
        raise SceneEditError(f"{obj.id} already has {MAX_CUTS} cuts")
    holes = _holes(obj, mesh_of)
    section = measure(object_mesh(obj, mesh_of), point.tolist(), normal.tolist(), holes, _cuts(obj))
    if section is None:
        raise SceneEditError(f"the plane does not cut {obj.id}")
    p = (float(point[0]), float(point[1]), float(point[2]))
    n = (float(normal[0]), float(normal[1]), float(normal[2]))
    upper_id = next_object_id(scene)
    lower = obj.model_copy(
        update={
            "name": named(obj.name, " (bottom)"),
            "ops": [*obj.ops, Cut(point=p, normal=n, keep="below")],
        }
    )
    upper = obj.model_copy(
        update={
            "id": upper_id,
            "name": named(obj.name, " (top)"),
            "ops": [*obj.ops, Cut(point=p, normal=n, keep="above")],
        }
    )
    if edit.joint is not None:
        spec = edit.joint
        if any(isinstance(x, Exposed) for x in t.scale):
            raise SceneEditError(
                f"{obj.id}'s scale is a parameter; unexpose the scale before splitting with a joint"
            )
        if _floats(t.scale) != [1.0, 1.0, 1.0]:
            raise SceneEditError(
                f"{obj.id} is scaled, so a joint's clearance would scale with it: set its scale "
                "to 1 first, or split it without a joint"
            )
        if not section.inside:
            raise SceneEditError(
                f"the section of {obj.id} at that plane has its centre outside it (a ring); "
                "cut elsewhere"
            )
        diameter = spec.diameter if spec.diameter is not None else round(1.2 * section.room, 2)
        reach = diameter / 2 + spec.clearance
        # A hex key's corners stand out past its flats by 1 / cos 30 degrees.
        need = (reach / math.cos(math.pi / 6) if spec.key == "hex" else reach) + WALL
        if section.room < need:
            raise SceneEditError(
                f"a {diameter:g} mm peg with {spec.clearance:g} mm clearance needs "
                f"{need:.2f} mm of section around its centre, and {obj.id} has "
                f"{math.floor(section.room * 100) / 100:.2f} mm there; "
                "cut where it is wider, or give a smaller diameter"
            )
        axis = tuple(-x for x in n) if spec.peg_on == "above" else n
        at = (float(section.centroid[0]), float(section.centroid[1]), float(section.centroid[2]))
        # The peg prints with the part the cut goes through most (on the carrot, the green neck).
        printed = [part for part in obj.parts if not part.negative]
        widest = max(
            printed, key=lambda part: _section_area(obj, mesh_of(part.mesh), point, normal)
        )
        joint: dict[str, Any] = {
            "at": at,
            "axis": axis,
            "diameter": diameter,
            "depth": spec.depth,
            "clearance": spec.clearance,
            "key": spec.key,
            "colour": widest.colour,
        }
        peg, socket = Joint(role="peg", **joint), Joint(role="socket", **joint)
        if spec.peg_on == "above":
            upper = upper.model_copy(update={"ops": [*upper.ops, peg]})
            lower = lower.model_copy(update={"ops": [*lower.ops, socket]})
        else:
            lower = lower.model_copy(update={"ops": [*lower.ops, peg]})
            upper = upper.model_copy(update={"ops": [*upper.ops, socket]})
    return _replace(scene, index, lower, upper)


def _has_peg_on(obj: SceneObject, point: Sequence[float], normal: Sequence[float]) -> bool:
    n = unit(normal)
    return any(
        isinstance(op, Joint)
        and op.role == "peg"
        and abs(float(np.dot(np.asarray(op.at) - np.asarray(point), n))) < 1e-6
        for op in obj.ops
    )


def _largest_flat(obj: SceneObject, mesh_of: MeshOf) -> np.ndarray:
    """The object-frame outward normal of its largest flat face after its cuts. A cut
    face carrying a peg is no face to stand on: the peg would go into the bed."""
    mesh = object_mesh(obj, mesh_of)
    cuts = _cuts(obj)
    best_area, best = 0.0, None
    centres = mesh.triangles_center
    for faces, area, normal in zip(mesh.facets, mesh.facets_area, mesh.facets_normal, strict=True):
        if area > best_area and len(kept(centres[faces], cuts)) == len(faces):
            best_area, best = float(area), np.asarray(normal)
    for index, (point, normal, keep) in enumerate(cuts):
        if _has_peg_on(obj, point, normal):
            continue
        section = measure(mesh, point, normal, cuts=[*cuts[:index], *cuts[index + 1 :]])
        if section is not None and section.area > best_area:
            best_area, best = section.area, unit(normal) * (1 if keep == "below" else -1)
    if best is None:
        raise SceneEditError(f"{obj.id} has no flat face to lie on; give lay_flat a normal")
    return best


def _added_points(obj: SceneObject) -> list[np.ndarray]:
    """Points on what the ops add (pegs, ``add`` primitives' boxes), in the object's
    frame, so a drop to the bed counts them."""
    ring = np.array(
        [(math.cos(a), math.sin(a), 0.0) for a in np.linspace(0, 2 * math.pi, 16, endpoint=False)]
    )
    out: list[np.ndarray] = []
    for op in obj.ops:
        if isinstance(op, Joint) and op.role == "peg":
            turn = frame(op.axis)
            for z in (0.0, value(op.depth)):
                out.append(
                    (turn @ (ring * value(op.diameter) / 2 + (0.0, 0.0, z)).T).T + np.asarray(op.at)
                )
        elif isinstance(op, Add):
            size = _floats(op.size)
            half = (
                np.array(size) / 2
                if op.primitive == "cube"
                else (
                    np.array([size[0], size[0], size[2]]) / 2
                    if op.primitive == "cylinder"
                    else np.full(3, size[0] / 2)
                )
            )
            corners = np.array(list(itertools.product(*[(-h, h) for h in half])))
            m, translate = transform_matrix(op.transform)
            out.append((m @ corners.T).T + translate)
    return out


def _lay_flat(scene: Scene, edit: LayFlatEdit, mesh_of: MeshOf) -> Scene:
    index = _find(scene, edit.object)
    obj = scene.objects[index]
    t = obj.transform
    _needs_plane(obj, "orientation to lay flat")
    rotate, scale = _floats(t.rotate), _floats(t.scale)
    if edit.normal == "largest_flat":
        direction = normal_to_world(rotate, scale, _largest_flat(obj, mesh_of))
    else:
        try:
            direction = unit(edit.normal)
        except ValueError:
            raise SceneEditError("lay_flat's normal is zero") from None
    new_rotate = euler(turn_to_down(direction) @ rotation(rotate))
    kept_points = cut_points(object_mesh(obj, mesh_of), _cuts(obj))
    if len(kept_points) == 0:
        raise SceneEditError(f"nothing of {obj.id} is left after its cuts, so it cannot lie flat")
    points = np.concatenate([kept_points, *_added_points(obj)])
    world = (linear(new_rotate, scale) @ points.T).T
    translate = _floats(t.translate)
    transform = Transform(
        translate=(translate[0], translate[1], float(-world[:, 2].min())),
        rotate=new_rotate,
        scale=t.scale,
    )
    return _replace(scene, index, obj.model_copy(update={"transform": transform}))


def _primitive(scene: Scene, edit: AddEdit | SubtractEdit) -> Scene:
    index = _find(scene, edit.object)
    obj = scene.objects[index]
    centre: Point = (0.0, 0.0, 0.0)
    if edit.at is not None:
        _needs_plane(obj, "frame to place a shape in")
        m, translate = transform_matrix(obj.transform)
        local = np.linalg.solve(m, np.asarray(edit.at) - translate)
        centre = (round(float(local[0]), 6), round(float(local[1]), 6), round(float(local[2]), 6))
    shape: dict[str, Any] = {
        "primitive": edit.primitive,
        "size": edit.size,
        "transform": Transform(translate=centre, rotate=edit.rotate),
    }
    op = (
        Add(**shape, colour=edit.colour.upper() if edit.colour else None)
        if isinstance(edit, AddEdit)
        else Subtract(**shape)
    )
    return _replace(scene, index, obj.model_copy(update={"ops": [*obj.ops, op]}))


def _set_field(model: BaseModel, path: str, new: object) -> BaseModel:
    name, _, element = path.partition(".")
    if name not in type(model).model_fields:
        raise SceneEditError(f"there is no field {name!r} here")
    current = getattr(model, name)
    if element:
        if not isinstance(current, tuple):
            raise SceneEditError(f"{name!r} is not a vector")
        items = list(current)
        items[int(element)] = new(items[int(element)]) if callable(new) else new
        updated: object = tuple(items)
    else:
        updated = new(current) if callable(new) else new
    return model.model_copy(update={name: updated})


def _exposing(name: str | None) -> Callable[[object], object]:
    def change(current: object) -> object:
        if isinstance(current, Exposed):
            return current.value if name is None else Exposed(expose=name, value=current.value)
        if not isinstance(current, float | int) or isinstance(current, bool):
            raise SceneEditError("only a number can be a parameter")
        return float(current) if name is None else Exposed(expose=name, value=float(current))

    return change


def _jointed(obj: SceneObject) -> bool:
    return any(isinstance(op, Joint) for op in obj.ops)


def _refuse_breaking_a_joint(obj: SceneObject, edit: ExposeEdit) -> None:
    """A parameter that moves a jointed cut, sizes a joint or scales a jointed half would
    move one side without the other (the other half's cut, the joint's root are their
    own literal numbers) or skip the fit check `_split` made: refused (review of #2388)."""
    if edit.op is None:
        if edit.field.startswith("scale") and _jointed(obj):
            raise SceneEditError(
                f"{obj.id} is half of a jointed pair; scaling one half alone breaks the fit"
            )
        return
    op = obj.ops[edit.op]
    if isinstance(op, Joint):
        raise SceneEditError(
            "a joint's numbers are measured from the section when it is cut; "
            "split again to change them"
        )
    if isinstance(op, Cut) and any(
        isinstance(other, Joint)
        and abs(float(np.dot(np.asarray(other.at) - _floats(op.point), unit(op.normal)))) < 1e-6
        for other in obj.ops
    ):
        raise SceneEditError(
            "this cut carries a joint, and its other half and the joint would not move with it"
        )


def _expose(scene: Scene, edit: ExposeEdit | UnexposeEdit) -> Scene:
    index = _find(scene, edit.object)
    obj = scene.objects[index]
    if isinstance(edit, ExposeEdit) and (edit.op is None or edit.op < len(obj.ops)):
        _refuse_breaking_a_joint(obj, edit)
    change = _exposing(edit.name if isinstance(edit, ExposeEdit) else None)
    if edit.op is None:
        updated = obj.model_copy(
            update={"transform": _set_field(obj.transform, edit.field, change)}
        )
    else:
        if edit.op >= len(obj.ops):
            raise SceneEditError(f"{obj.id} has no op {edit.op}")
        ops = list(obj.ops)
        ops[edit.op] = cast(Op, _set_field(ops[edit.op], edit.field, change))
        updated = obj.model_copy(update={"ops": ops})
    return _replace(scene, index, updated)


def _one(scene: Scene, edit: BaseModel, mesh_of: MeshOf) -> Scene:
    if isinstance(edit, SplitEdit):
        return _split(scene, edit, mesh_of)
    if isinstance(edit, LayFlatEdit):
        return _lay_flat(scene, edit, mesh_of)
    if isinstance(edit, AddEdit | SubtractEdit):
        return _primitive(scene, edit)
    if isinstance(edit, ExposeEdit | UnexposeEdit):
        return _expose(scene, edit)
    assert isinstance(edit, _Edit)
    index = _find(scene, edit.object)
    obj = scene.objects[index]
    if isinstance(edit, TransformEdit):
        t = obj.transform
        if edit.scale is not None and any(value(x) == 0 for x in edit.scale):
            raise SceneEditError(f"{obj.id} cannot take a zero scale")
        if (
            edit.scale is not None
            and _jointed(obj)
            and (
                _floats(edit.scale) != _floats(t.scale)
                or any(isinstance(x, Exposed) for x in edit.scale)
            )
        ):
            raise SceneEditError(
                f"{obj.id} is half of a jointed pair; scale the object before splitting it"
            )
        transform = Transform(
            translate=edit.translate if edit.translate is not None else t.translate,
            rotate=edit.rotate if edit.rotate is not None else t.rotate,
            scale=edit.scale if edit.scale is not None else t.scale,
        )
        return _replace(scene, index, obj.model_copy(update={"transform": transform}))
    if isinstance(edit, CustomEdit):
        return _replace(
            scene, index, obj.model_copy(update={"ops": [*obj.ops, Custom(scad=edit.scad)]})
        )
    if isinstance(edit, SetColourEdit):
        if all(p.id != edit.part for p in obj.parts):
            raise SceneEditError(f"{obj.id} has no part {edit.part}")
        parts = [
            p.model_copy(update={"colour": edit.colour.upper()}) if p.id == edit.part else p
            for p in obj.parts
        ]
        return _replace(scene, index, obj.model_copy(update={"parts": parts}))
    if isinstance(edit, DeleteEdit):
        if len(scene.objects) == 1:
            raise SceneEditError("the last object cannot be deleted; delete the model instead")
        return _replace(scene, index)
    if isinstance(edit, DuplicateEdit):
        t = obj.transform
        moved = [value(a) + b for a, b in zip(t.translate, edit.offset, strict=True)]
        copy = obj.model_copy(
            update={
                "id": next_object_id(scene),
                "name": named(obj.name, " (copy)"),
                "transform": t.model_copy(update={"translate": tuple(moved)}),
            }
        )
        return _replace(scene, index, obj, copy)
    if isinstance(edit, RenameEdit):
        return _replace(scene, index, obj.model_copy(update={"name": edit.name}))
    assert isinstance(edit, RemoveOpEdit)
    if edit.op >= len(obj.ops):
        raise SceneEditError(f"{obj.id} has no op {edit.op}")
    return _replace(
        scene, index, obj.model_copy(update={"ops": [*obj.ops[: edit.op], *obj.ops[edit.op + 1 :]]})
    )


def apply_edits(scene: Scene, edits: Sequence[BaseModel], mesh_of: MeshOf) -> Scene:
    for edit in edits:
        try:
            scene = _one(scene, edit, mesh_of)
        except OpenSectionError:
            raise SceneEditError(
                f"{getattr(edit, 'object', 'the object')}'s mesh is not closed where the plane "
                "cuts it"
            ) from None
    try:
        return Scene.model_validate(scene.model_dump())
    except ValueError as error:
        raise SceneEditError(str(error)) from None


def removed_parameters(before: Scene, after: Scene) -> list[str]:
    kept_names = exposed_values(after)
    return [name for name in exposed_values(before) if name not in kept_names]
