# Mesh models, phases 1 and 2: open a mesh as a scene, and edit it — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Bambuddy library 3MF/STL, an uploaded 3MF/STL or an output opens as a ScadBuddy model whose `scene.json` compiles to `model.scad`, and the agent (and any caller) edits it with `split_at_plane` (keyed peg and socket), `lay_flat`, add/subtract, custom snippets, transforms and colours, each edit one revision, through library-queue operations on Temporal.

**Architecture:** A new feature package `backend/scadbuddy/meshmodel/` holds the scene model, pure math (frames, sections), the pure compiler, ingest and the edits; its `operations.py` exports three `OperationKind`s on the `library` queue (discovered like every feature's), and `api/mesh_models.py` is their routes (discovered like every route module). The catalogue gains two writes (create with extra files, a locked read-modify-write of several files). The agent gains four tools in `agent/src/tools/meshModels.ts`. A bundled template `models/mesh-joint-demo/` and a `requires_openscad` test prove the render.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, numpy, trimesh 5.1 (no shapely, no scipy: both are absent, so nothing here calls `slice_mesh_plane` or `section().to_planar()`), Temporal via the existing `OperationWorkflow`; TypeScript/zod for the agent tools.

**Spec:** `docs/superpowers/specs/2026-10-11-mesh-models-design.md` (phases 1 and 2 of §11). Paint transfer (§7), `weak_joints` (§9), the scene editor UI (§8) and the agent eval scenario (§10, which scores the carrot loop that `weak_joints` drives) are phase 3/4 plans.

## Global Constraints

- Every backend command from `CLAUDE.md`: `uv run --frozen ruff check .`, `uv run --frozen ruff format --check .`, `uv run --frozen mypy` (strict), `uv run --frozen pytest -n auto`; agent `pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
- `tests/api` tests need `SCADBUDDY_TEST_DATABASE_URL` and a Temporal (`temporal` on PATH or `SCADBUDDY_TEST_TEMPORAL_ADDRESS`); they skip without them. Run them before calling a task done.
- Put `tests/api/` paths together in one pytest command (CLAUDE.md: mixing loses `tests/api/conftest.py`).
- Binary files are written only under a model's `meshes/`; `PUT /models/{slug}/files/{name}` stays `.scad`-only.
- Part mesh files carry no `basematerials` and no colour (spec §3.3); the colour is only the scene's `color()`.
- Mesh-model writes are `OperationKind`s with `queue="library"`; no new workflow type, no new queue (spec §6).
- Nothing is written to Bambuddy (spec §1, §4).
- The upload and library download cap is `MAX_DOWNLOAD_BYTES` (`bambuddy/print_source.py`, 256 MiB).
- The hand-edit check compares `sha256(model.scad)` with `scene.json`'s `generated_sha256`, never a fresh compile (spec §5).
- A peg needs `room >= diameter / 2 + clearance + 0.8` mm (`room`: distance from the section's centroid to its nearest edge; 0.8 mm is two 0.4 mm walls); the default diameter is `1.2 * room` (60 % of the circle that fits around the centroid).
- No new Python dependency.
- Agent-authored docs cite sources (#259); a measured fact says where and how.
- Commit after every task; conventional-commit messages ending with the session's attribution lines.

## Measured for this plan (2026-10-11, `scadbuddy:ci-test`, OpenSCAD 2026.09.23)

- A core 3MF of one object, no materials, with `[Content_Types].xml` and `_rels/.rels`, imports in OpenSCAD inside `color("#00FF00")` and the output's `basematerials` carry `#00FF00`. Re-measured on the pinned image by Task 9.
- trimesh 5.1.1 in `backend/.venv`: `trimesh.intersections.mesh_plane(..., return_faces=True)` works (128 segments for a 64-sided cylinder at mid-height); `slice_mesh_plane` raises `ModuleNotFoundError: scipy`; `mesh.facets`/`facets_area`/`facets_normal` work (networkx is installed); the same 3MF reloads with `trimesh.load(..., file_type="3mf")` watertight.

## Review Focus

1. **A library file whose objects are many copies of one mesh** (the Carrot Garden file prints several carrots): every copy becomes its own scene object sharing one set of mesh files, and splitting one copy leaves the others untouched. Pinned in Task 5 (`test_copies_share_meshes`) and Task 7 (`test_split_touches_only_its_object`).
2. **An edit list where a later edit names an object an earlier one created** (`split_at_plane` then `lay_flat` on the new top half, in one request): edits apply in order against the scene so far. Pinned in Task 7 (`test_edits_apply_in_order`).
3. **A cut plane that misses the object** or **a ring-shaped section whose centroid is outside it**: a 422 naming the object, not a broken render. Pinned in Task 7 (`test_split_refuses_a_plane_that_misses`, `test_joint_refuses_a_centroid_outside_the_section`).
4. **A model whose `model.scad` was edited by hand**, then an edit: 409 with the eject hint, nothing written; and an older model compiled by an earlier compiler is still editable. Pinned in Task 8 (`test_a_hand_edited_model_refuses_scene_edits`) and Task 7 (`test_hash_check_ignores_compiler_changes`).
5. **Re-sending the same create or edit** (an answer that never arrived): the idempotency key replays the first answer and makes no second model or revision. Pinned in Task 8 (`test_a_repeated_edit_is_one_revision`).

---

## File map

| File | Responsibility |
|---|---|
| `backend/scadbuddy/meshmodel/__init__.py` | Package docstring only |
| `backend/scadbuddy/meshmodel/scene.py` | `Scene` and its parts, ops, `Exposed`; load/dump; exposed-parameter walk |
| `backend/scadbuddy/meshmodel/frames.py` | OpenSCAD rotations, world→object plane mapping, `frame`, `euler`, `turn_to_down` |
| `backend/scadbuddy/meshmodel/section.py` | Section of a mesh by a plane (area, centroid, room, inside); points an object's cuts keep |
| `backend/scadbuddy/meshmodel/compile.py` | `compile_scene(scene) -> str`, pure |
| `backend/scadbuddy/meshmodel/ingest.py` | `part_mesh_3mf`, `scene_from_objects` |
| `backend/scadbuddy/meshmodel/edits.py` | The edit models and `apply_edits` |
| `backend/scadbuddy/meshmodel/operations.py` | `OPERATION_KINDS`: `mesh_model_create`, `scene_edit`, `scene_eject` |
| `backend/scadbuddy/api/mesh_models.py` | Routes: create (JSON, upload), get scene, edits, eject |
| `backend/scadbuddy/api/limits.py` | `MESH_UPLOAD_PATH` |
| `backend/scadbuddy/main.py` | The upload's `RouteLimit` |
| `backend/scadbuddy/library/catalogue.py` | `ModelMeta.mesh_source`; `create(files=)`; `update_files` |
| `backend/tests/meshmodel/` | Unit tests per module |
| `backend/tests/api/test_mesh_models.py` | Operation and route tests |
| `backend/tests/test_mesh_models_openscad.py` | `requires_openscad` render test |
| `agent/src/tools/meshModels.ts`, `agent/src/tools/index.ts` | The four tools |
| `agent/test/meshModelTools.test.ts` | Tool tests |
| `models/mesh-joint-demo/` | Bundled demo template and its `verify.sh` |
| `plugins/scadbuddy/skills/authoring/SKILL.md` | Mesh-model section |
| `docs/superpowers/specs/2026-09-22-scadbuddy-design.md` | §3 gains the measured import fact (Task 9) |

---

### Task 1: The scene model

**Files:**
- Create: `backend/scadbuddy/meshmodel/__init__.py`, `backend/scadbuddy/meshmodel/scene.py`
- Test: `backend/tests/meshmodel/__init__.py` (empty), `backend/tests/meshmodel/test_scene.py`

**Interfaces:**
- Produces: `SCENE_NAME = "scene.json"`, `MESHES_DIR = "meshes"`, `Exposed`, `Num`, `Vec3`, `value(n) -> float`, `Transform`, `Part`, `Cut`, `Joint`, `Primitive`, `Add`, `Subtract`, `Custom`, `Op`, `SceneObject`, `SceneSource`, `Scene`, `exposed_values(model) -> dict[str, float]`, `next_object_id(scene) -> str`, `load_scene(text) -> Scene`, `dump_scene(scene) -> str`, `text_sha256(text) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/meshmodel/test_scene.py
"""The scene model (spec 2026-10-11 §3.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.meshmodel.scene import (
    Cut,
    Exposed,
    Joint,
    Part,
    Scene,
    SceneObject,
    SceneSource,
    Transform,
    dump_scene,
    exposed_values,
    load_scene,
    next_object_id,
)

SOURCE = SceneSource(kind="upload", name="carrot.3mf", sha256="ab" * 32)


def obj(oid: str = "o1", **update: object) -> SceneObject:
    base = SceneObject(
        id=oid, name="Carrot", parts=[Part(id="p1", mesh=f"meshes/{oid}-p1.3mf", colour="#F08A24")]
    )
    return base.model_copy(update=update)


def test_a_scene_round_trips_through_json() -> None:
    scene = Scene(
        source=SOURCE,
        objects=[obj(ops=[Cut(point=(0, 0, Exposed(expose="cut_height", value=32)), normal=(0, 0, 1), keep="below")])],
    )
    assert load_scene(dump_scene(scene)) == scene


def test_exposed_values_are_collected_once_in_order() -> None:
    cut = Cut(point=(0, 0, Exposed(expose="cut_height", value=32)), normal=(0, 0, 1), keep="below")
    scene = Scene(
        source=SOURCE,
        objects=[
            obj("o1", ops=[cut]),
            obj("o2", ops=[cut.model_copy(update={"keep": "above"})],
                transform=Transform(scale=(Exposed(expose="size", value=1.5), 1, 1))),
        ],
    )
    assert exposed_values(scene) == {"cut_height": 32.0, "size": 1.5}


def test_one_name_with_two_values_is_refused() -> None:
    with pytest.raises(ValidationError, match="cut_height"):
        Scene(
            source=SOURCE,
            objects=[
                obj("o1", ops=[Cut(point=(0, 0, Exposed(expose="cut_height", value=32)), normal=(0, 0, 1), keep="below")]),
                obj("o2", ops=[Cut(point=(0, 0, Exposed(expose="cut_height", value=40)), normal=(0, 0, 1), keep="below")]),
            ],
        )


@pytest.mark.parametrize("name", ["o1", "o12_parts", "sb_key", "1x", "with space", "$fn"])
def test_reserved_or_invalid_exposed_names_are_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        Exposed(expose=name, value=1)


def test_repeated_object_ids_are_refused() -> None:
    with pytest.raises(ValidationError, match="repeat"):
        Scene(source=SOURCE, objects=[obj("o1"), obj("o1")])


@pytest.mark.parametrize("mesh", ["../x.3mf", "meshes/../../etc.3mf", "meshes/o1-p1.stl", "/abs/o1-p1.3mf"])
def test_a_mesh_path_outside_meshes_is_refused(mesh: str) -> None:
    with pytest.raises(ValidationError):
        Part(id="p1", mesh=mesh, colour="#FFFFFF")


def test_non_finite_numbers_are_refused() -> None:
    with pytest.raises(ValidationError):
        Joint(at=(0, 0, 0), axis=(0, 0, -1), diameter=float("nan"), depth=6, clearance=0.2, role="peg")


def test_next_object_id_is_one_past_the_largest() -> None:
    scene = Scene(source=SOURCE, objects=[obj("o1"), obj("o7")])
    assert next_object_id(scene) == "o8"
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_scene.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'scadbuddy.meshmodel'`.

- [ ] **Step 3: Write the package and the model**

```python
# backend/scadbuddy/meshmodel/__init__.py
"""Mesh models (#2146, spec 2026-10-11): a 3MF or STL opened as a scene whose
``scene.json`` compiles to the model's ``model.scad``."""
```

```python
# backend/scadbuddy/meshmodel/scene.py
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
RESERVED = r"(?!o[0-9]+(?:_|$))(?!sb_)"
IDENT = rf"^{RESERVED}[A-Za-z_][A-Za-z0-9_]*$"
COLOUR = r"^#[0-9A-F]{6}$"
MESH_PATH = r"^meshes/o[0-9]+-p[0-9]+\.3mf$"
MAX_OBJECTS = 200
MAX_OPS = 100


class _Strict(BaseModel):
    # python-re: `IDENT`'s lookaheads, which the default Rust engine does not support.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True, regex_engine="python-re")


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
    id: str = Field(pattern=r"^p[0-9]+$")
    mesh: str = Field(pattern=MESH_PATH)
    colour: str = Field(pattern=COLOUR)
    #: Painted in Bambu Studio; its codes are ``<mesh stem>.paint.json`` (spec §7).
    painted: bool = False


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


class Primitive(_Strict):
    """cube: ``size`` is x, y, z; cylinder: diameter ``size[0]``, height ``size[2]``;
    sphere: diameter ``size[0]``. Centred, then placed by ``transform``."""

    primitive: Literal["cube", "cylinder", "sphere"]
    size: Vec3
    transform: Transform = Transform()


class Add(Primitive):
    op: Literal["add"] = "add"


class Subtract(Primitive):
    op: Literal["subtract"] = "subtract"


class Custom(_Strict):
    """OpenSCAD given the geometry so far as ``children()``."""

    op: Literal["custom"] = "custom"
    scad: str = Field(min_length=1, max_length=20_000)


Op = Annotated[Cut | Joint | Add | Subtract | Custom, Field(discriminator="op")]


class SceneObject(_Strict):
    id: str = Field(pattern=r"^o[0-9]+$")
    name: str = Field(min_length=1, max_length=200)
    parts: list[Part] = Field(min_length=1, max_length=64)
    ops: list[Op] = Field(default_factory=list, max_length=MAX_OPS)
    transform: Transform = Transform()


class SceneSource(_Strict):
    kind: Literal["bambuddy_library", "upload", "output"]
    name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
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


def next_object_id(scene: Scene) -> str:
    return f"o{max(int(o.id[1:]) for o in scene.objects) + 1}"


def load_scene(text: str) -> Scene:
    return Scene.model_validate_json(text)


def dump_scene(scene: Scene) -> str:
    return scene.model_dump_json(indent=2) + "\n"


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_scene.py -q`
Expected: PASS. If `test_a_scene_round_trips_through_json` fails because `Num` parsed `{"expose":…}` as a float, add `Field(union_mode="left_to_right")` is not needed: pydantic's smart union picks `Exposed` for an object; check the failure text before changing anything.

- [ ] **Step 5: Lint, type-check and commit**

```bash
cd backend && uv run --frozen ruff check scadbuddy/meshmodel tests/meshmodel && uv run --frozen ruff format scadbuddy/meshmodel tests/meshmodel && uv run --frozen mypy
git add backend/scadbuddy/meshmodel backend/tests/meshmodel
git commit -m "feat(meshmodel): the scene model (#2146)"
```

---

### Task 2: Frames

**Files:**
- Create: `backend/scadbuddy/meshmodel/frames.py`
- Test: `backend/tests/meshmodel/test_frames.py`

**Interfaces:**
- Produces: `unit(v) -> np.ndarray`, `rotation(degrees) -> np.ndarray (3x3)`, `linear(rotate, scale) -> np.ndarray`, `to_object_plane(translate, rotate, scale, point, normal) -> tuple[np.ndarray, np.ndarray]`, `normal_to_world(rotate, scale, normal) -> np.ndarray`, `frame(direction) -> np.ndarray`, `euler(r) -> tuple[float, float, float]`, `turn_to_down(direction) -> np.ndarray`. All arguments are plain floats (callers pass `value(...)`).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/meshmodel/test_frames.py
"""Rotations and plane mapping (spec 2026-10-11 §5, `split_at_plane`, `lay_flat`)."""

from __future__ import annotations

import numpy as np
import pytest

from scadbuddy.meshmodel.frames import (
    euler,
    frame,
    linear,
    normal_to_world,
    rotation,
    to_object_plane,
    turn_to_down,
)


def test_rotation_is_openscads_x_then_y_then_z() -> None:
    # rotate([90, 0, 0]) takes +Y to +Z; rotate([0, 0, 90]) takes +X to +Y.
    assert np.allclose(rotation((90, 0, 0)) @ [0, 1, 0], [0, 0, 1])
    assert np.allclose(rotation((0, 0, 90)) @ [1, 0, 0], [0, 1, 0])
    # X first, then Z: +Y -> +Z -> +Z.
    assert np.allclose(rotation((90, 0, 90)) @ [0, 1, 0], [0, 0, 1])


@pytest.mark.parametrize("angles", [(10, 20, 30), (-75, 44, 170), (0, 90, 0), (0, -90, 45), (180, 0, 0)])
def test_euler_inverts_rotation(angles: tuple[float, float, float]) -> None:
    assert np.allclose(rotation(euler(rotation(angles))), rotation(angles), atol=1e-9)


@pytest.mark.parametrize("d", [(0, 0, 1), (0, 0, -1), (1, 0, 0), (0.3, -0.4, 0.866)])
def test_frame_takes_z_onto_the_direction(d: tuple[float, float, float]) -> None:
    r = frame(d)
    assert np.allclose(r @ [0, 0, 1], np.asarray(d) / np.linalg.norm(d))
    assert np.allclose(r.T @ r, np.eye(3))
    assert np.isclose(np.linalg.det(r), 1.0)


def test_turn_to_down_takes_the_direction_to_minus_z() -> None:
    assert np.allclose(turn_to_down((1, 0, 0)) @ [1, 0, 0], [0, 0, -1])


def test_a_world_plane_maps_into_the_object_frame_under_non_uniform_scale() -> None:
    translate, rotate, scale = (10.0, 0.0, 0.0), (0.0, 0.0, 45.0), (2.0, 1.0, 1.0)
    point, normal = to_object_plane(translate, rotate, scale, (10.0, 0.0, 5.0), (1.0, 1.0, 0.0))
    m = linear(rotate, scale)
    # A point x on the object plane maps to a world point on the world plane.
    tangent = np.cross(normal, [0, 0, 1])
    x = point + 3.0 * tangent
    world = m @ x + translate
    assert np.isclose(np.dot(world - (10.0, 0.0, 5.0), np.asarray((1.0, 1.0, 0.0)) / np.sqrt(2)), 0.0)
    assert np.isclose(np.linalg.norm(normal), 1.0)


def test_normal_to_world_is_the_inverse_of_the_plane_mapping() -> None:
    rotate, scale = (0.0, 30.0, 0.0), (1.0, 3.0, 0.5)
    _, local = to_object_plane((0, 0, 0), rotate, scale, (0, 0, 0), (0.0, 0.6, 0.8))
    assert np.allclose(normal_to_world(rotate, scale, local), (0.0, 0.6, 0.8))


def test_a_zero_normal_is_refused() -> None:
    with pytest.raises(ValueError, match="zero"):
        to_object_plane((0, 0, 0), (0, 0, 0), (1, 1, 1), (0, 0, 0), (0, 0, 0))
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_frames.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Write `frames.py`**

```python
# backend/scadbuddy/meshmodel/frames.py
"""Rotations and frames for a scene (spec 2026-10-11 §3.1, §5).

An object is ``translate(t) rotate(r) scale(s)`` of its mesh, as OpenSCAD applies them:
the linear part is ``M = R·S`` with ``R = Rz·Ry·Rx``. A world plane (point ``p``,
normal ``n``) is the object-frame plane ``M⁻¹(p − t)``, ``Mᵀn`` renormalised: a
plane's normal goes by the inverse-transpose, which only differs once the scale is not
uniform. All arguments are plain floats."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

_Z = np.array([0.0, 0.0, 1.0])


def unit(v: Sequence[float] | np.ndarray) -> np.ndarray:
    array = np.asarray(v, dtype=float)
    length = float(np.linalg.norm(array))
    if length < 1e-12:
        raise ValueError("a direction of zero length has no orientation")
    return array / length


def _rx(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def _ry(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def rotation(degrees: Sequence[float]) -> np.ndarray:
    """OpenSCAD's ``rotate([x, y, z])``: about X, then Y, then Z."""
    x, y, z = (math.radians(d) for d in degrees)
    return _rz(z) @ _ry(y) @ _rx(x)


def linear(rotate: Sequence[float], scale: Sequence[float]) -> np.ndarray:
    return rotation(rotate) @ np.diag(np.asarray(scale, dtype=float))


def to_object_plane(
    translate: Sequence[float],
    rotate: Sequence[float],
    scale: Sequence[float],
    point: Sequence[float],
    normal: Sequence[float],
) -> tuple[np.ndarray, np.ndarray]:
    try:
        n = unit(normal)
    except ValueError:
        raise ValueError("the plane's normal is zero") from None
    m = linear(rotate, scale)
    local_point = np.linalg.solve(m, np.asarray(point, dtype=float) - np.asarray(translate, dtype=float))
    return local_point, unit(m.T @ n)


def normal_to_world(rotate: Sequence[float], scale: Sequence[float], normal: Sequence[float]) -> np.ndarray:
    """An object-frame face normal as it faces in the world."""
    return unit(np.linalg.inv(linear(rotate, scale)).T @ np.asarray(normal, dtype=float))


def frame(direction: Sequence[float] | np.ndarray) -> np.ndarray:
    """The rotation taking +Z onto ``direction``."""
    d = unit(direction)
    c = float(_Z @ d)
    if c > 1 - 1e-12:
        return np.eye(3)
    if c < -1 + 1e-12:
        return np.diag([1.0, -1.0, -1.0])  # a half turn about X
    v = np.cross(_Z, d)
    s = float(np.linalg.norm(v))
    k = v / s
    skew = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + s * skew + (1 - c) * (skew @ skew)


def turn_to_down(direction: Sequence[float] | np.ndarray) -> np.ndarray:
    """The rotation taking ``direction`` onto −Z."""
    return np.diag([1.0, -1.0, -1.0]) @ frame(direction).T


def euler(r: np.ndarray) -> tuple[float, float, float]:
    """The ``rotate([x, y, z])`` degrees whose rotation is ``r`` (Rz·Ry·Rx)."""
    y = math.asin(max(-1.0, min(1.0, -float(r[2, 0]))))
    if abs(math.cos(y)) > 1e-9:
        x = math.atan2(float(r[2, 1]), float(r[2, 2]))
        z = math.atan2(float(r[1, 0]), float(r[0, 0]))
    else:  # gimbal lock: only x + z (or x − z) is fixed; take x = 0
        x = 0.0
        z = math.atan2(-float(r[0, 1]), float(r[1, 1]))
    return (round(math.degrees(x), 9), round(math.degrees(y), 9), round(math.degrees(z), 9))
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_frames.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type-check and commit**

```bash
cd backend && uv run --frozen ruff check scadbuddy/meshmodel tests/meshmodel && uv run --frozen ruff format scadbuddy/meshmodel tests/meshmodel && uv run --frozen mypy
git add backend/scadbuddy/meshmodel/frames.py backend/tests/meshmodel/test_frames.py
git commit -m "feat(meshmodel): frames for transforms and planes (#2146)"
```

---

### Task 3: The compiler

**Files:**
- Create: `backend/scadbuddy/meshmodel/compile.py`
- Test: `backend/tests/meshmodel/test_compile.py`

**Interfaces:**
- Consumes: Task 1's models, `exposed_values`; Task 2's `frame`.
- Produces: `compile_scene(scene: Scene) -> str`, `HEADER: str`, `HALF: float`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/meshmodel/test_compile.py
"""`compile_scene` (spec 2026-10-11 §3.3)."""

from __future__ import annotations

from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.scene import (
    Add,
    Custom,
    Cut,
    Exposed,
    Joint,
    Part,
    Scene,
    SceneObject,
    SceneSource,
    Subtract,
    Transform,
)

SOURCE = SceneSource(kind="upload", name="carrot.3mf", sha256="ab" * 32)


def scene(*objects: SceneObject) -> Scene:
    return Scene(source=SOURCE, objects=list(objects))


def carrot(oid: str = "o1", ops: list = [], **update: object) -> SceneObject:  # noqa: B006 (never mutated)
    return SceneObject(
        id=oid,
        name="Carrot",
        parts=[
            Part(id="p1", mesh=f"meshes/{oid}-p1.3mf", colour="#F08A24"),
            Part(id="p2", mesh=f"meshes/{oid}-p2.3mf", colour="#3A7D2C"),
        ],
        ops=ops,
    ).model_copy(update=update)


def test_a_plain_object_compiles_to_imports_in_colour() -> None:
    plain = SceneObject(id="o1", name="Box", parts=[Part(id="p1", mesh="meshes/o1-p1.3mf", colour="#F08A24")])
    assert compile_scene(scene(plain)) == (
        "// Generated from scene.json by ScadBuddy; edit the scene, or eject (scene_eject) to edit this file\n"
        "\n"
        "module o1_parts() {\n"
        '  color("#F08A24") import("meshes/o1-p1.3mf");\n'
        "}\n"
        "module o1() {\n"
        "  o1_parts();\n"
        "}\n"
        "\n"
        "translate([0, 0, 0]) rotate([0, 0, 0]) scale([1, 1, 1]) o1();\n"
    )


def test_compiling_is_deterministic() -> None:
    s = scene(carrot(ops=[Cut(point=(0, 0, 30), normal=(0, 0, 1), keep="below")]))
    assert compile_scene(s) == compile_scene(s.model_copy(deep=True))


def test_a_cut_intersects_with_a_half_space_on_the_kept_side() -> None:
    below = compile_scene(scene(carrot(ops=[Cut(point=(0, 0, 30), normal=(0, 0, 1), keep="below")])))
    above = compile_scene(scene(carrot(ops=[Cut(point=(0, 0, 30), normal=(0, 0, 1), keep="above")])))
    assert "intersection() {" in below
    assert "translate([0, 0, 30]) multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])" in below
    assert "translate([-2000, -2000, -4000]) cube(4000);" in below
    assert "translate([-2000, -2000, 0]) cube(4000);" in above


def test_exposed_numbers_become_customizer_parameters_before_any_module() -> None:
    text = compile_scene(
        scene(carrot(ops=[Cut(point=(0, 0, Exposed(expose="cut_height", value=32.5)), normal=(0, 0, 1), keep="below")]))
    )
    lines = text.splitlines()
    assert lines[1] == "cut_height = 32.5;"
    assert lines.index("cut_height = 32.5;") < next(i for i, l in enumerate(lines) if l.startswith("module"))
    assert "translate([0, 0, cut_height])" in text


def test_a_joint_adds_a_peg_in_colour_and_cuts_a_grown_socket() -> None:
    peg = Joint(at=(0, 0, 30), axis=(0, 0, -1), diameter=4, depth=6, clearance=0.2, key="flat", role="peg")
    socket = peg.model_copy(update={"role": "socket"})
    text = compile_scene(scene(carrot("o1", ops=[socket]), carrot("o2", ops=[peg])))
    assert "module sb_key(d, h, key, grow = 0) {" in text
    assert 'color("#F08A24") translate([0, 0, 30]) multmatrix([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]]) sb_key(4, 6, "flat");' in text
    assert 'translate([0, 0, -0.01]) sb_key(4, 6 + 0.2 + 0.01, "flat", 0.2);' in text


def test_no_key_module_without_a_joint() -> None:
    assert "sb_key" not in compile_scene(scene(carrot()))


def test_add_and_subtract_place_centred_primitives() -> None:
    add = Add(primitive="cylinder", size=(3, 3, 8), transform=Transform(translate=(0, 0, 30)))
    sub = Subtract(primitive="cube", size=(2, 2, 2))
    text = compile_scene(scene(carrot(ops=[add, sub])))
    assert 'color("#F08A24") translate([0, 0, 30]) rotate([0, 0, 0]) scale([1, 1, 1]) cylinder(h = 8, d = 3, center = true, $fn = 64);' in text
    assert "difference() {" in text and "cube([2, 2, 2], center = true);" in text


def test_a_custom_op_is_a_module_given_the_geometry_as_children() -> None:
    text = compile_scene(scene(carrot(ops=[Custom(scad="minkowski() { children(); sphere(0.5); }")])))
    assert "module o1_op0() {\n  minkowski() { children(); sphere(0.5); }\n}" in text
    assert "o1_op0() {\n    o1_parts();\n  }" in text


def test_the_object_is_placed_by_its_transform() -> None:
    text = compile_scene(scene(carrot(transform=Transform(translate=(5, -2.25, 0), rotate=(90, 0, 0), scale=(1, 1, 2)))))
    assert text.endswith("translate([5, -2.25, 0]) rotate([90, 0, 0]) scale([1, 1, 2]) o1();\n")
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_compile.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Write `compile.py`**

```python
# backend/scadbuddy/meshmodel/compile.py
"""`compile_scene`: ``scene.json`` to ``model.scad``, pure and deterministic (spec
2026-10-11 §3.3). Exposed numbers become top-level assignments before any module, so
OpenSCAD's customizer lists them. Each part is ``color(c) import(mesh)``; the mesh file
carries no colour (§3.3), so ``render/solids.py``'s per-colour re-render still yields
one closed solid per colour. Geometry an op adds (a peg, an ``add``) is in the object's
first part's colour."""

from __future__ import annotations

import numpy as np

from scadbuddy.meshmodel.frames import frame
from scadbuddy.meshmodel.scene import (
    Add,
    Custom,
    Cut,
    Exposed,
    Joint,
    Num,
    Op,
    Primitive,
    Scene,
    SceneObject,
    Subtract,
    Transform,
    exposed_values,
)

HEADER = (
    "// Generated from scene.json by ScadBuddy; edit the scene, or eject (scene_eject) "
    "to edit this file"
)
#: Half the side of the cube a cut intersects with, mm: past any printer's bed.
HALF = 2000.0
KEY_MODULE = [
    "module sb_key(d, h, key, grow = 0) {",
    "  r = d / 2 + grow;",
    '  if (key == "hex") cylinder(h = h, r = r / cos(30), $fn = 6);',
    '  else if (key == "flat") intersection() {',
    "    cylinder(h = h, r = r, $fn = 48);",
    "    translate([-r, -r, 0]) cube([2 * r, r + 0.35 * d + grow, h]);",
    "  }",
    "  else cylinder(h = h, r = r, $fn = 48);",
    "}",
]


def _f(x: float) -> str:
    text = f"{x:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _n(v: Num) -> str:
    return v.expose if isinstance(v, Exposed) else _f(v)


def _vec(v: tuple[Num, Num, Num]) -> str:
    return "[" + ", ".join(_n(x) for x in v) + "]"


def _rot(r: np.ndarray) -> str:
    rows = [[*map(float, r[i]), 0.0] for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
    return "[" + ", ".join("[" + ", ".join(_f(x) for x in row) + "]" for row in rows) + "]"


def _placed(t: Transform) -> str:
    return f"translate({_vec(t.translate)}) rotate({_vec(t.rotate)}) scale({_vec(t.scale)})"


def _primitive(p: Primitive) -> str:
    if p.primitive == "cube":
        return f"cube({_vec(p.size)}, center = true);"
    if p.primitive == "cylinder":
        return f"cylinder(h = {_n(p.size[2])}, d = {_n(p.size[0])}, center = true, $fn = 64);"
    return f"sphere(d = {_n(p.size[0])}, $fn = 64);"


def _indent(lines: list[str]) -> list[str]:
    return ["  " + line for line in lines]


def _wrap(obj: SceneObject, index: int, op: Op, inner: list[str]) -> list[str]:
    nested = _indent(inner)
    colour = obj.parts[0].colour
    if isinstance(op, Cut):
        floor = "0" if op.keep == "above" else _f(-2 * HALF)
        tool = (
            f"translate({_vec(op.point)}) multmatrix({_rot(frame(op.normal))}) "
            f"translate([{_f(-HALF)}, {_f(-HALF)}, {floor}]) cube({_f(2 * HALF)});"
        )
        return ["intersection() {", *nested, f"  {tool}", "}"]
    if isinstance(op, Joint):
        place = f"translate({_vec(op.at)}) multmatrix({_rot(frame(op.axis))})"
        d, h, c = _n(op.diameter), _n(op.depth), _n(op.clearance)
        if op.role == "peg":
            return ["union() {", *nested, f'  color("{colour}") {place} sb_key({d}, {h}, "{op.key}");', "}"]
        return [
            "difference() {",
            *nested,
            f'  {place} translate([0, 0, -0.01]) sb_key({d}, {h} + {c} + 0.01, "{op.key}", {c});',
            "}",
        ]
    if isinstance(op, Add):
        return ["union() {", *nested, f'  color("{colour}") {_placed(op.transform)} {_primitive(op)}', "}"]
    if isinstance(op, Subtract):
        return ["difference() {", *nested, f"  {_placed(op.transform)} {_primitive(op)}", "}"]
    assert isinstance(op, Custom)
    return [f"{obj.id}_op{index}() {{", *nested, "}"]


def _modules(obj: SceneObject) -> list[str]:
    out = [f"module {obj.id}_parts() {{"]
    out += [f'  color("{p.colour}") import("{p.mesh}");' for p in obj.parts]
    out.append("}")
    for index, op in enumerate(obj.ops):
        if isinstance(op, Custom):
            out += [f"module {obj.id}_op{index}() {{", *_indent(op.scad.splitlines()), "}"]
    body = [f"{obj.id}_parts();"]
    for index, op in enumerate(obj.ops):
        body = _wrap(obj, index, op, body)
    return [*out, f"module {obj.id}() {{", *_indent(body), "}"]


def compile_scene(scene: Scene) -> str:
    lines = [HEADER]
    lines += [f"{name} = {_f(v)};" for name, v in exposed_values(scene).items()]
    lines.append("")
    if any(isinstance(op, Joint) for obj in scene.objects for op in obj.ops):
        lines += [*KEY_MODULE, ""]
    for obj in scene.objects:
        lines += [*_modules(obj), ""]
    lines += [f"{_placed(obj.transform)} {obj.id}();" for obj in scene.objects]
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_compile.py -q`
Expected: PASS. In `test_a_joint_adds_a_peg_in_colour_and_cuts_a_grown_socket` the expected matrix is `frame((0, 0, -1))`, the half turn about X `diag(1, -1, -1)`.

- [ ] **Step 5: Lint, type-check and commit**

```bash
cd backend && uv run --frozen ruff check scadbuddy/meshmodel tests/meshmodel && uv run --frozen ruff format scadbuddy/meshmodel tests/meshmodel && uv run --frozen mypy
git add backend/scadbuddy/meshmodel/compile.py backend/tests/meshmodel/test_compile.py
git commit -m "feat(meshmodel): compile a scene to model.scad (#2146)"
```

---

### Task 4: Sections and kept points

**Files:**
- Create: `backend/scadbuddy/meshmodel/section.py`
- Test: `backend/tests/meshmodel/test_section.py`

**Interfaces:**
- Consumes: Task 2's `unit`.
- Produces: `Section(area: float, centroid: np.ndarray, room: float, inside: bool)`, `measure(mesh, point, normal) -> Section | None`, `kept(points, cuts) -> np.ndarray` (rows of `points` on the kept side of every `(point, normal, keep)` in `cuts`), `cut_points(mesh, cuts) -> np.ndarray` (the vertices an object's cuts keep plus each cut's section points the other cuts keep: what the cut mesh's vertices would be).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/meshmodel/test_section.py
"""Sections of a mesh by a plane (spec 2026-10-11 §5, peg sizing), numpy only."""

from __future__ import annotations

import math

import numpy as np
import pytest
import trimesh

from scadbuddy.meshmodel.section import cut_points, measure


def neck() -> trimesh.Trimesh:
    """A carrot-ish solid, one closed mesh (OpenSCAD imports it as one volume): body
    r=10 z 0..30, neck r=3 z 30..35, tuft r=6 z 35..45, 64-sided."""
    profile = np.array([[0, 0], [10, 0], [10, 30], [3, 30], [3, 35], [6, 35], [6, 45], [0, 45]], dtype=float)
    return trimesh.creation.revolve(profile, sections=64)


def test_a_cylinder_section_has_its_area_centroid_and_room() -> None:
    cylinder = trimesh.creation.cylinder(radius=5, height=10, sections=64)
    section = measure(cylinder, (0, 0, 0), (0, 0, 1))
    assert section is not None
    assert section.area == pytest.approx(0.5 * 64 * 25 * math.sin(2 * math.pi / 64), rel=1e-6)
    assert np.allclose(section.centroid, (0, 0, 0), atol=1e-9)
    assert section.room == pytest.approx(5 * math.cos(math.pi / 64), rel=1e-6)
    assert section.inside


def test_a_tilted_plane_measures_in_its_own_plane() -> None:
    box = trimesh.creation.box((10, 10, 10))
    section = measure(box, (0, 0, 0), (0, 1, 1))
    assert section is not None
    assert section.area == pytest.approx(10 * 10 * math.sqrt(2), rel=1e-6)


def test_the_neck_is_measured_where_the_plane_is() -> None:
    section = measure(neck(), (0, 0, 32), (0, 0, 1))
    assert section is not None
    assert section.room == pytest.approx(3 * math.cos(math.pi / 64), rel=1e-6)


def test_a_plane_that_misses_measures_nothing() -> None:
    assert measure(neck(), (0, 0, 100), (0, 0, 1)) is None


def test_a_ring_has_its_centroid_outside() -> None:
    ring = trimesh.creation.annulus(r_min=4, r_max=5, height=10, sections=64)
    section = measure(ring, (0, 0, 0), (0, 0, 1))
    assert section is not None
    assert not section.inside


def test_cut_points_are_the_kept_vertices_and_the_cut_rim() -> None:
    points = cut_points(neck(), [((0, 0, 32), (0, 0, 1), "above")])
    assert points[:, 2].min() == pytest.approx(32)
    assert points[:, 2].max() == pytest.approx(45)
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_section.py -q`
Expected: FAIL, `ModuleNotFoundError`. (`trimesh.creation.revolve`, `cone` and `annulus` work without shapely or scipy in trimesh 5.1.1: measured for this plan.)

- [ ] **Step 3: Write `section.py`**

```python
# backend/scadbuddy/meshmodel/section.py
"""Where a plane cuts a mesh (spec 2026-10-11 §5): the section's area, centroid, and
``room`` (distance from the centroid to the nearest edge), for sizing a peg; and the
points an object's cuts keep, for `lay_flat`'s drop to the bed.

numpy only. trimesh's own section path needs shapely and its slicing needs scipy,
neither installed (measured 2026-10-11, plan 2026-10-11-mesh-models-phase-1-2). The
section's segments come from ``mesh_plane``, each oriented with the interior on its
left by its face's outward normal, so Green's theorem gives area and centroid without
assembling loops."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import trimesh

from scadbuddy.meshmodel.frames import unit

CutSpec = tuple[Sequence[float], Sequence[float], Literal["above", "below"]]


@dataclass(frozen=True)
class Section:
    area: float
    centroid: np.ndarray
    room: float
    inside: bool


def _basis(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    helper = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = unit(np.cross(n, helper))
    return u, np.cross(n, u)


def _segments(mesh: trimesh.Trimesh, point: np.ndarray, n: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    lines, faces = trimesh.intersections.mesh_plane(mesh, n, point, return_faces=True)
    if len(lines) == 0:
        return None
    start, end = lines[:, 0], lines[:, 1]
    left = np.cross(n, end - start)
    flip = np.einsum("ij,ij->i", left, mesh.face_normals[faces]) > 0
    return np.where(flip[:, None], end, start), np.where(flip[:, None], start, end)


def measure(mesh: trimesh.Trimesh, point: Sequence[float], normal: Sequence[float]) -> Section | None:
    n = unit(normal)
    origin = np.asarray(point, dtype=float)
    found = _segments(mesh, origin, n)
    if found is None:
        return None
    start, end = found
    u, v = _basis(n)
    a = np.stack([(start - origin) @ u, (start - origin) @ v], axis=1)
    b = np.stack([(end - origin) @ u, (end - origin) @ v], axis=1)
    cross = a[:, 0] * b[:, 1] - b[:, 0] * a[:, 1]
    area = float(cross.sum() / 2)
    if area <= 1e-9:
        return None
    c = ((a + b) * cross[:, None]).sum(axis=0) / (6 * area)
    ab = b - a
    t = np.clip(((c - a) * ab).sum(axis=1) / np.maximum((ab * ab).sum(axis=1), 1e-18), 0.0, 1.0)
    room = float(np.linalg.norm(c - (a + t[:, None] * ab), axis=1).min())
    straddles = (a[:, 1] > c[1]) != (b[:, 1] > c[1])
    with np.errstate(divide="ignore", invalid="ignore"):
        x = a[:, 0] + (c[1] - a[:, 1]) * ab[:, 0] / ab[:, 1]
    inside = bool(np.count_nonzero(straddles & (x > c[0])) % 2)
    return Section(area, origin + c[0] * u + c[1] * v, room, inside)


def kept(points: np.ndarray, cuts: Sequence[CutSpec]) -> np.ndarray:
    mask = np.ones(len(points), dtype=bool)
    for point, normal, keep in cuts:
        side = (points - np.asarray(point, dtype=float)) @ unit(normal)
        mask &= side >= -1e-9 if keep == "above" else side <= 1e-9
    return points[mask]


def cut_points(mesh: trimesh.Trimesh, cuts: Sequence[CutSpec]) -> np.ndarray:
    points = [kept(np.asarray(mesh.vertices, dtype=float), cuts)]
    for index, (point, normal, _) in enumerate(cuts):
        found = _segments(mesh, np.asarray(point, dtype=float), unit(normal))
        if found is not None:
            others = [c for i, c in enumerate(cuts) if i != index]
            points.append(kept(np.concatenate(found), others))
    return np.concatenate(points)
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_section.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type-check and commit**

```bash
cd backend && uv run --frozen ruff check scadbuddy/meshmodel tests/meshmodel && uv run --frozen ruff format scadbuddy/meshmodel tests/meshmodel && uv run --frozen mypy
git add backend/scadbuddy/meshmodel/section.py backend/tests/meshmodel/test_section.py
git commit -m "feat(meshmodel): measure a section and the points cuts keep (#2146)"
```

---

### Task 5: Ingest

**Files:**
- Create: `backend/scadbuddy/meshmodel/ingest.py`
- Test: `backend/tests/meshmodel/test_ingest.py`

**Interfaces:**
- Consumes: `render.objects3mf.ReadObject` (`name`, `count`, `parts: list[ColourPart]`, `notes`), `render.split.ColourPart` (`material_index`, `name`, `colour`, `mesh`, `paint: Paint | None`), `render.split.Paint` (`codes`, `colours`), `render.bambu3mf.CORE_NS`, `MODEL_RELATIONSHIP`, `MODEL_CONTENT_TYPE`, `RELS_CONTENT_TYPE`, `ROOT_MODEL_NAME`, `ZIP_TIMESTAMP`; Task 1's models.
- Produces: `part_mesh_3mf(mesh) -> bytes`, `scene_from_objects(read: list[ReadObject], source: SceneSource, *, gap: float = 5.0) -> tuple[Scene, dict[str, bytes]]` (the scene without `generated_sha256`, and the files by path relative to the model directory).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/meshmodel/test_ingest.py
"""Reading objects into a scene (spec 2026-10-11 §4)."""

from __future__ import annotations

import io
import json
import zipfile

import numpy as np
import trimesh

from scadbuddy.meshmodel.ingest import part_mesh_3mf, scene_from_objects
from scadbuddy.meshmodel.scene import SceneSource
from scadbuddy.render.objects3mf import ReadObject
from scadbuddy.render.split import ColourPart, Paint

SOURCE = SceneSource(kind="upload", name="garden.3mf", sha256="cd" * 32)


def part(index: int, colour: str, mesh: trimesh.Trimesh, paint: Paint | None = None) -> ColourPart:
    return ColourPart(index, f"Part {index}", colour, mesh, paint)


def box(x: float, size: float = 10.0) -> trimesh.Trimesh:
    mesh = trimesh.creation.box((size, size, size))
    mesh.apply_translation((x, 7.0, 20.0))  # anywhere: ingest recentres
    return mesh


def test_a_part_file_is_a_colourless_closed_3mf() -> None:
    data = part_mesh_3mf(trimesh.creation.box((2, 3, 4)))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"} <= set(archive.namelist())
        model = archive.read("3D/3dmodel.model").decode()
    assert "basematerials" not in model and "pid=" not in model
    back = trimesh.load(io.BytesIO(data), file_type="3mf", force="mesh")
    assert back.is_watertight and np.allclose(back.extents, (2, 3, 4))


def test_the_same_mesh_gives_the_same_bytes() -> None:
    assert part_mesh_3mf(trimesh.creation.box((1, 1, 1))) == part_mesh_3mf(trimesh.creation.box((1, 1, 1)))


def test_objects_become_a_row_resting_on_the_bed() -> None:
    read = [
        ReadObject(name="Carrot", count=1, parts=[part(0, "#F08A24", box(0)), part(1, "#3A7D2C", box(0, 4))]),
        ReadObject(name="Sign", count=1, parts=[part(0, "#FFFFFF", box(50))]),
    ]
    scene, files = scene_from_objects(read, SOURCE)
    assert [o.id for o in scene.objects] == ["o1", "o2"]
    assert [p.mesh for p in scene.objects[0].parts] == ["meshes/o1-p1.3mf", "meshes/o1-p2.3mf"]
    assert set(files) == {"meshes/o1-p1.3mf", "meshes/o1-p2.3mf", "meshes/o2-p1.3mf"}
    first = trimesh.load(io.BytesIO(files["meshes/o1-p1.3mf"]), file_type="3mf", force="mesh")
    assert first.bounds[0][2] == 0.0  # resting on z = 0
    assert np.allclose(first.bounds.mean(axis=0)[:2], (0, 0))  # centred in x and y
    xs = [o.transform.translate[0] for o in scene.objects]
    # Two 10 mm objects 5 mm apart, centred on the origin: at −7.5 and 7.5.
    assert np.allclose(xs, (-7.5, 7.5))


def test_copies_share_meshes() -> None:
    read = [ReadObject(name="Carrot", count=3, parts=[part(0, "#F08A24", box(0))])]
    scene, files = scene_from_objects(read, SOURCE)
    assert [o.id for o in scene.objects] == ["o1", "o2", "o3"]
    assert {o.parts[0].mesh for o in scene.objects} == {"meshes/o1-p1.3mf"}
    assert list(files) == ["meshes/o1-p1.3mf"]
    assert len({o.transform.translate[0] for o in scene.objects}) == 3


def test_a_painted_part_keeps_its_codes_beside_its_mesh() -> None:
    mesh = trimesh.creation.box((1, 1, 1))
    paint = Paint(codes=("",) * (len(mesh.faces) - 1) + ("4",), colours=("#F08A24", "#3A7D2C"))
    scene, files = scene_from_objects(
        [ReadObject(name="Carrot", count=1, parts=[part(0, "#F08A24", mesh, paint)])], SOURCE
    )
    assert scene.objects[0].parts[0].painted
    assert json.loads(files["meshes/o1-p1.paint.json"])["codes"][-1] == "4"
    assert any("paint" in note for note in scene.notes)


def test_reading_notes_are_kept() -> None:
    scene, _ = scene_from_objects(
        [ReadObject(name="Carrot", count=1, parts=[part(0, "#F08A24", box(0))], notes=["dropped a modifier"])],
        SOURCE,
    )
    assert "Carrot: dropped a modifier" in scene.notes
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_ingest.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Write `ingest.py`**

```python
# backend/scadbuddy/meshmodel/ingest.py
"""A file's objects as a scene (spec 2026-10-11 §4): one mesh file per (object, colour
part), with no colour of its own (§3.3), recentred so the object sits on z = 0 about
its own x/y centre; an object read with ``count`` copies becomes that many scene objects
sharing its meshes; the objects start in a row along X, ``gap`` apart, centred on the
origin. A painted part's codes are kept beside its mesh for paint transfer (§7)."""

from __future__ import annotations

import dataclasses
import io
import json
import zipfile

import numpy as np
import trimesh

from scadbuddy.meshmodel.scene import Part, Scene, SceneObject, SceneSource, Transform
from scadbuddy.render.bambu3mf import (
    CORE_NS,
    MODEL_CONTENT_TYPE,
    MODEL_RELATIONSHIP,
    RELS_CONTENT_TYPE,
    ROOT_MODEL_NAME,
    ZIP_TIMESTAMP,
)
from scadbuddy.render.objects3mf import ReadObject

_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    f'<Default Extension="rels" ContentType="{RELS_CONTENT_TYPE}"/>'
    f'<Default Extension="model" ContentType="{MODEL_CONTENT_TYPE}"/></Types>'
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Target="/{ROOT_MODEL_NAME}" Id="rel0" Type="{MODEL_RELATIONSHIP}"/>'
    "</Relationships>"
)


def part_mesh_3mf(mesh: trimesh.Trimesh) -> bytes:
    vertices = "".join(f'<vertex x="{x!r}" y="{y!r}" z="{z!r}"/>' for x, y, z in mesh.vertices.tolist())
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces.tolist())
    model = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{CORE_NS}">'
        f'<resources><object id="1" type="model"><mesh><vertices>{vertices}</vertices>'
        f"<triangles>{triangles}</triangles></mesh></object></resources>"
        '<build><item objectid="1"/></build></model>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in (("[Content_Types].xml", _TYPES), ("_rels/.rels", _RELS), (ROOT_MODEL_NAME, model)):
            archive.writestr(zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP), text)
    return buffer.getvalue()


def scene_from_objects(
    read: list[ReadObject], source: SceneSource, *, gap: float = 5.0
) -> tuple[Scene, dict[str, bytes]]:
    files: dict[str, bytes] = {}
    objects: list[SceneObject] = []
    widths: list[float] = []
    notes: list[str] = []
    for obj in read:
        # A read object's files are named by the id of its first scene object.
        stem = f"meshes/o{len(objects) + 1}"
        vertices = np.concatenate([np.asarray(p.mesh.vertices) for p in obj.parts])
        low, high = vertices.min(axis=0), vertices.max(axis=0)
        offset = -np.array([(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]])
        parts: list[Part] = []
        for number, colour_part in enumerate(obj.parts, start=1):
            mesh = colour_part.mesh.copy()
            mesh.apply_translation(offset)
            path = f"{stem}-p{number}.3mf"
            files[path] = part_mesh_3mf(mesh)
            painted = colour_part.paint is not None
            if painted:
                assert colour_part.paint is not None
                files[f"{stem}-p{number}.paint.json"] = json.dumps(dataclasses.asdict(colour_part.paint)).encode()
            parts.append(Part(id=f"p{number}", mesh=path, colour=colour_part.colour.upper(), painted=painted))
        if any(p.painted for p in parts):
            notes.append(f"{obj.name}: painted in Bambu Studio; the paint is kept for printing, not yet shown in renders")
        notes += [f"{obj.name}: {note}" for note in obj.notes]
        for _ in range(obj.count):
            objects.append(SceneObject(id=f"o{len(objects) + 1}", name=obj.name, parts=parts))
            widths.append(float(high[0] - low[0]))
    total = sum(widths) + gap * (len(widths) - 1)
    x = -total / 2
    placed = []
    for obj, width in zip(objects, widths, strict=True):
        placed.append(obj.model_copy(update={"transform": Transform(translate=(x + width / 2, 0.0, 0.0))}))
        x += width + gap
    return Scene(source=source, objects=placed, notes=notes), files
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_ingest.py -q`
Expected: PASS.

- [ ] **Step 5: Lint, type-check and commit**

```bash
cd backend && uv run --frozen ruff check scadbuddy/meshmodel tests/meshmodel && uv run --frozen ruff format scadbuddy/meshmodel tests/meshmodel && uv run --frozen mypy
git add backend/scadbuddy/meshmodel/ingest.py backend/tests/meshmodel/test_ingest.py
git commit -m "feat(meshmodel): read a file's objects into a scene (#2146)"
```

---

### Task 6: Catalogue writes

**Files:**
- Modify: `backend/scadbuddy/library/catalogue.py` (`ModelMeta` near line 328; `create` near line 1076; a new `update_files` after `write_file`, near line 2196)
- Test: `backend/tests/test_catalogue_mesh_files.py`

**Interfaces:**
- Consumes: Task 1's `SceneSource`.
- Produces: `ModelMeta.mesh_source: SceneSource | None = None`; `Catalogue.create(..., files: Mapping[str, bytes] | None = None)`; `Catalogue.update_files(slug: str, compute: Callable[[Path], Mapping[str, bytes | None]], *, message: str) -> ModelRecord`; `UnsafeModelPathError(ValueError)`.

- [ ] **Step 1: Write the failing tests**

Use the catalogue fixture the existing catalogue tests use (`grep -n "def catalogue" backend/tests/conftest.py backend/tests/test_catalogue*.py` and reuse it; it gives a `Catalogue` with a git history on a temp data directory).

```python
# backend/tests/test_catalogue_mesh_files.py
"""The catalogue's writes for mesh models (#2146): a create with files beside the
source, and a locked read-modify-write of several files as one revision."""

from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.library.catalogue import Catalogue, ModelMeta, UnsafeModelPathError
from scadbuddy.meshmodel.scene import SceneSource

pytestmark = pytest.mark.requires_git

SOURCE = SceneSource(kind="upload", name="carrot.3mf", sha256="ef" * 32)


def test_a_create_writes_its_files_in_the_same_revision(catalogue: Catalogue) -> None:
    record = catalogue.create(
        "carrot",
        "o1();\n",
        ModelMeta(name="Carrot", mesh_source=SOURCE),
        files={"meshes/o1-p1.3mf": b"PK\x03\x04binary", "scene.json": b"{}\n"},
    )
    directory = catalogue.paths.model_dir("carrot")
    assert (directory / "meshes" / "o1-p1.3mf").read_bytes() == b"PK\x03\x04binary"
    assert catalogue.read_raw_meta("carrot")["mesh_source"]["name"] == "carrot.3mf"
    assert record.version is not None
    assert len(catalogue.history.log("carrot")) == 1  # one revision


@pytest.mark.parametrize("name", ["../escape", "/abs", "meshes/../../x", ""])
def test_a_create_refuses_a_path_outside_the_model(catalogue: Catalogue, name: str) -> None:
    with pytest.raises(UnsafeModelPathError):
        catalogue.create("carrot", "o1();\n", ModelMeta(name="Carrot"), files={name: b"x"})
    assert not catalogue.exists("carrot")


def test_update_files_writes_and_removes_as_one_revision(catalogue: Catalogue) -> None:
    catalogue.create("carrot", "a\n", ModelMeta(name="Carrot"), files={"scene.json": b"1"})
    seen: list[bytes] = []

    def compute(directory: Path) -> dict[str, bytes | None]:
        seen.append((directory / "scene.json").read_bytes())
        return {"scene.json": None, "model.scad": b"b\n"}

    before = len(catalogue.history.log("carrot"))
    catalogue.update_files("carrot", compute, message="Edit the scene")
    directory = catalogue.paths.model_dir("carrot")
    assert seen == [b"1"]
    assert not (directory / "scene.json").exists()
    assert (directory / "model.scad").read_text() == "b\n"
    log = catalogue.history.log("carrot")
    assert len(log) == before + 1 and log[0].message == "Edit the scene"


def test_update_files_writes_nothing_when_compute_raises(catalogue: Catalogue) -> None:
    catalogue.create("carrot", "a\n", ModelMeta(name="Carrot"))
    before = len(catalogue.history.log("carrot"))

    def compute(directory: Path) -> dict[str, bytes | None]:
        raise ValueError("refused")

    with pytest.raises(ValueError, match="refused"):
        catalogue.update_files("carrot", compute, message="never")
    assert len(catalogue.history.log("carrot")) == before
```

If no `catalogue` fixture exists with history, build one in this file the way `backend/tests/test_model_history.py` (or the nearest catalogue+history test) does; copy its fixture body verbatim rather than inventing setup.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_catalogue_mesh_files.py -q`
Expected: FAIL, `ImportError: cannot import name 'UnsafeModelPathError'`.

- [ ] **Step 3: Implement**

In `catalogue.py`, beside the other errors:

```python
class UnsafeModelPathError(ValueError):
    """A file named outside the model's directory."""


def _inside(directory: Path, name: str) -> Path:
    """``name`` under ``directory``, or :class:`UnsafeModelPathError`."""
    if not name or name.startswith("/") or "\\" in name:
        raise UnsafeModelPathError(name)
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()) or path == directory.resolve():
        raise UnsafeModelPathError(name)
    return path
```

In `ModelMeta`, after `upstream`:

```python
    #: Where a mesh model's meshes came from (#2146): kept after an eject, so the
    #: origin and licence stay recorded. Not in `ModelPatch`: it records a fact.
    mesh_source: SceneSource | None = None
```

with `from scadbuddy.meshmodel.scene import SceneSource` at the top (scene.py imports only pydantic, so there is no cycle).

In `create`, add the keyword `files: Mapping[str, bytes] | None = None` and, inside the `try` after the README is written:

```python
            for name, data in (files or {}).items():
                path = _inside(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
```

Check `meta.model_dump(exclude=excluded)` keeps `mesh_source` (it does: only `media`/`print_settings` are excluded) and that a `None` `mesh_source` is dropped the way other `None` fields are (look at what `write_raw_meta` does with `None`; if it writes `"mesh_source": null` into every new model.json, add `"mesh_source"` to `excluded` when it is `None`, as `print_settings` is).

After `write_file`:

```python
    def update_files(
        self,
        slug: str,
        compute: Callable[[Path], Mapping[str, bytes | None]],
        *,
        message: str,
    ) -> ModelRecord:
        """A read-modify-write of several of the model's files as one revision (#2146):
        ``compute`` reads the directory and answers each path (relative to it) to write,
        or None to remove. Both run under the history's write lock
        (:meth:`_commit_change`), so no other edit lands between the read and the write.
        ``compute`` raising writes nothing. The cached schema is dropped, as a source
        edit drops it."""
        self._require(slug)
        directory = self.paths.model_dir(slug)

        def change() -> None:
            writes = {_inside(directory, name): data for name, data in compute(directory).items()}
            for path, data in writes.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_atomic(path, data)
            self.paths.model_schema_cache(slug).unlink(missing_ok=True)

        self._commit_change(message, change, slug)
        return self.record(slug)
```

(`write_atomic` is `scadbuddy.core.files.write_atomic`, already used by `write_file`.)

- [ ] **Step 4: Run the tests, and the catalogue's existing ones**

Run: `cd backend && uv run --frozen pytest tests/test_catalogue_mesh_files.py tests/test_catalogue.py -q`
Expected: PASS (if `tests/test_catalogue.py` is named differently, run `ls tests | grep catalogue` and include them all).

- [ ] **Step 5: Lint, type-check and commit**

```bash
cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy
git add backend/scadbuddy/library/catalogue.py backend/tests/test_catalogue_mesh_files.py
git commit -m "feat(library): create with files, and update several files as one revision (#2146)"
```

---

### Task 7: Edits

**Files:**
- Create: `backend/scadbuddy/meshmodel/edits.py`
- Test: `backend/tests/meshmodel/test_edits.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `WALL = 0.8`, `SceneEditError(ValueError)`, `JointSpec`, the edit models (`TransformEdit`, `SplitEdit`, `LayFlatEdit`, `AddEdit`, `SubtractEdit`, `CustomEdit`, `SetColourEdit`, `DeleteEdit`, `DuplicateEdit`, `RenameEdit`, `RemoveOpEdit`, `ExposeEdit`, `UnexposeEdit`), `Edit` (discriminated on `edit`), `MeshOf = Callable[[str], trimesh.Trimesh]` (a part's mesh path to its mesh), `apply_edits(scene, edits, mesh_of) -> Scene`, `removed_parameters(before, after) -> list[str]`, `object_mesh(obj, mesh_of) -> trimesh.Trimesh`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/meshmodel/test_edits.py
"""Scene edits (spec 2026-10-11 §5)."""

from __future__ import annotations

import math

import numpy as np
import pytest
import trimesh
from pydantic import TypeAdapter

from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.edits import (
    Edit,
    SceneEditError,
    apply_edits,
    removed_parameters,
)
from scadbuddy.meshmodel.frames import rotation
from scadbuddy.meshmodel.scene import Cut, Exposed, Joint, Part, Scene, SceneObject, SceneSource, Transform, text_sha256, value
from tests.meshmodel.test_section import neck

SOURCE = SceneSource(kind="upload", name="garden.3mf", sha256="ab" * 32)
EDITS = TypeAdapter(list[Edit])


def mesh_of(path: str) -> trimesh.Trimesh:
    return neck()


def garden(n: int = 2) -> Scene:
    return Scene(
        source=SOURCE,
        objects=[
            SceneObject(
                id=f"o{i}",
                name="Carrot",
                parts=[Part(id="p1", mesh="meshes/o1-p1.3mf", colour="#F08A24")],
                transform=Transform(translate=(30.0 * i, 0, 0)),
            )
            for i in range(1, n + 1)
        ],
    )


def edits(*raw: dict) -> list:
    return EDITS.validate_python(list(raw))


def split(oid: str = "o1", z: float = 32.0, x: float = 30.0, **joint: object) -> dict:
    body: dict = {"edit": "split_at_plane", "object": oid, "point": [x, 0, z], "normal": [0, 0, 1]}
    if joint:
        body["joint"] = joint
    return body


def test_split_makes_two_objects_cut_on_each_side() -> None:
    result = apply_edits(garden(), edits(split()), mesh_of)
    lower, upper = result.objects[0], result.objects[1]
    assert (lower.id, upper.id) == ("o1", "o3")
    assert lower.ops[-1] == Cut(point=(0.0, 0.0, 32.0), normal=(0.0, 0.0, 1.0), keep="below")
    assert isinstance(upper.ops[-1], Cut) and upper.ops[-1].keep == "above"
    assert upper.transform == lower.transform


def test_split_touches_only_its_object() -> None:
    result = apply_edits(garden(), edits(split()), mesh_of)
    assert result.objects[2] == garden().objects[1]


def test_split_maps_the_world_plane_through_the_transform() -> None:
    scene = garden(1).model_copy(
        update={"objects": [garden(1).objects[0].model_copy(update={"transform": Transform(translate=(30, 0, 0), scale=(1, 1, 2))})]}
    )
    result = apply_edits(scene, edits(split(z=64.0)), mesh_of)
    cut = result.objects[0].ops[-1]
    assert isinstance(cut, Cut)
    assert value(cut.point[2]) == pytest.approx(32.0)


def test_a_joint_puts_a_peg_on_the_top_and_a_socket_in_the_bottom() -> None:
    result = apply_edits(garden(1), edits(split(depth=5, clearance=0.2, key="hex")), mesh_of)
    lower, upper = result.objects
    socket, peg = lower.ops[-1], upper.ops[-1]
    assert isinstance(socket, Joint) and socket.role == "socket"
    assert isinstance(peg, Joint) and peg.role == "peg"
    assert socket.axis == peg.axis == (0.0, 0.0, -1.0)
    assert np.allclose(peg.at, (0, 0, 32), atol=1e-6)
    room = 3 * math.cos(math.pi / 64)
    assert value(peg.diameter) == pytest.approx(round(1.2 * room, 2))
    assert (value(peg.depth), value(peg.clearance), peg.key) == (5, 0.2, "hex")


def test_a_peg_that_does_not_fit_is_refused_with_the_room_there_is() -> None:
    with pytest.raises(SceneEditError, match=r"needs 3\.70 mm.*has 2\.99 mm"):
        apply_edits(garden(1), edits(split(diameter=5.8, clearance=0.0)), mesh_of)


def test_split_refuses_a_plane_that_misses() -> None:
    with pytest.raises(SceneEditError, match="does not cut o1"):
        apply_edits(garden(1), edits(split(z=500)), mesh_of)


def test_joint_refuses_a_centroid_outside_the_section() -> None:
    ring = trimesh.creation.annulus(r_min=4, r_max=5, height=10, sections=64)
    with pytest.raises(SceneEditError, match="centre"):
        apply_edits(garden(1), edits(split(z=0.0, depth=4)), lambda _: ring)


def test_lay_flat_turns_the_direction_down_and_drops_to_the_bed() -> None:
    result = apply_edits(garden(1), edits({"edit": "lay_flat", "object": "o1", "normal": [1, 0, 0]}), mesh_of)
    t = result.objects[0].transform
    r = rotation([value(a) for a in t.rotate])
    assert np.allclose(r @ [1, 0, 0], [0, 0, -1], atol=1e-9)
    # Lying on its side, the body's radius 10 is its lowest point: lifted by 10.
    assert value(t.translate[2]) == pytest.approx(10.0, abs=1e-6)


def test_lay_flat_largest_flat_after_a_split_lays_the_cut_face_down() -> None:
    # A cone's top above z = 5 has no flat face but the cut (its base is cut away).
    cone = trimesh.creation.cone(radius=10, height=30, sections=64)
    result = apply_edits(
        garden(1),
        edits(split(z=5.0), {"edit": "lay_flat", "object": "o2", "normal": "largest_flat"}),
        lambda _: cone,
    )
    top = result.objects[1]
    # The cut face already faces down (−Z): no turn, and the part drops 5 mm to the bed.
    assert np.allclose(rotation([value(a) for a in top.transform.rotate]), np.eye(3), atol=1e-9)
    assert value(top.transform.translate[2]) == pytest.approx(-5.0)


def test_edits_apply_in_order() -> None:
    result = apply_edits(
        garden(1), edits(split(), {"edit": "rename", "object": "o2", "name": "Leaves"}), mesh_of
    )
    assert [o.name for o in result.objects] == ["Carrot (bottom)", "Leaves"]


def test_an_unknown_object_is_refused() -> None:
    with pytest.raises(SceneEditError, match="no object o9"):
        apply_edits(garden(1), edits({"edit": "rename", "object": "o9", "name": "x"}), mesh_of)


def test_the_last_object_cannot_be_deleted() -> None:
    with pytest.raises(SceneEditError, match="last object"):
        apply_edits(garden(1), edits({"edit": "delete", "object": "o1"}), mesh_of)


def test_set_colour_add_subtract_custom_duplicate_remove_op() -> None:
    result = apply_edits(
        garden(1),
        edits(
            {"edit": "set_colour", "object": "o1", "part": "p1", "colour": "#3a7d2c"},
            {"edit": "add", "object": "o1", "primitive": "cylinder", "size": [4, 4, 6], "at": [30, 0, 33]},
            {"edit": "subtract", "object": "o1", "primitive": "cube", "size": [2, 2, 2]},
            {"edit": "custom", "object": "o1", "scad": "children();"},
            {"edit": "remove_op", "object": "o1", "op": 2},
            {"edit": "duplicate", "object": "o1"},
        ),
        mesh_of,
    )
    first, copy = result.objects
    assert first.parts[0].colour == "#3A7D2C"
    assert [op.op for op in first.ops] == ["add", "subtract"]
    assert first.ops[0].transform.translate == (0.0, 0.0, 33.0)  # world (30, 0, 33) in the object's frame
    assert copy.id == "o2" and copy.ops == first.ops
    assert value(copy.transform.translate[0]) == pytest.approx(50.0)


def test_expose_and_unexpose() -> None:
    exposed = apply_edits(
        garden(1),
        edits(split(), {"edit": "expose", "object": "o1", "op": 0, "field": "point.2", "name": "cut_height"}),
        mesh_of,
    )
    assert "cut_height = 32;" in compile_scene(exposed)
    back = apply_edits(exposed, edits({"edit": "unexpose", "object": "o1", "op": 0, "field": "point.2"}), mesh_of)
    assert removed_parameters(exposed, back) == ["cut_height"]


def test_expose_on_the_transform() -> None:
    result = apply_edits(
        garden(1), edits({"edit": "expose", "object": "o1", "op": None, "field": "scale.2", "name": "height_scale"}), mesh_of
    )
    assert result.objects[0].transform.scale[2] == Exposed(expose="height_scale", value=1.0)


def test_a_delete_reports_the_parameters_it_removes() -> None:
    scene = garden(2)
    first = scene.objects[0].model_copy(update={"transform": Transform(scale=(Exposed(expose="size", value=1), 1, 1))})
    scene = scene.model_copy(update={"objects": [first, scene.objects[1]]})
    after = apply_edits(scene, edits({"edit": "delete", "object": "o1"}), mesh_of)
    assert removed_parameters(scene, after) == ["size"]


def test_hash_check_ignores_compiler_changes() -> None:
    # The recorded hash is of the text last written, so an edit recompiles and records
    # a new one; apply_edits itself never compares hashes (the kind's check does).
    scene = garden(1).model_copy(update={"generated_sha256": text_sha256("an older compiler's text")})
    result = apply_edits(scene, edits({"edit": "rename", "object": "o1", "name": "x"}), mesh_of)
    assert result.generated_sha256 == scene.generated_sha256  # the kind records the new one
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_edits.py -q`
Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Write `edits.py`**

```python
# backend/scadbuddy/meshmodel/edits.py
"""Scene edits (spec 2026-10-11 §5): what callers send, applied in order to the scene
so far. Planes and points arrive in world coordinates and are stored in the object's
own frame (`frames.to_object_plane`). Measuring reads the part meshes through
``mesh_of``; everything it measures is stored in the ops, so compiling stays pure."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Literal

import numpy as np
import trimesh
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.meshmodel.frames import euler, linear, normal_to_world, rotation, to_object_plane, turn_to_down, unit
from scadbuddy.meshmodel.scene import (
    COLOUR,
    Add,
    Custom,
    Cut,
    Exposed,
    Joint,
    Num,
    Point,
    Scene,
    SceneObject,
    Subtract,
    Transform,
    Vec3,
    exposed_values,
    next_object_id,
    value,
)
from scadbuddy.meshmodel.section import cut_points, kept, measure

#: Two 0.4 mm walls around a socket (spec §5).
WALL = 0.8
MeshOf = Callable[[str], trimesh.Trimesh]


class SceneEditError(ValueError):
    """An edit the scene refuses; the route's 422."""


class _Edit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    object: str = Field(pattern=r"^o[0-9]+$")


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


class SubtractEdit(_PrimitiveEdit):
    edit: Literal["subtract"] = "subtract"


class CustomEdit(_Edit):
    edit: Literal["custom"] = "custom"
    scad: str = Field(min_length=1, max_length=20_000)


class SetColourEdit(_Edit):
    edit: Literal["set_colour"] = "set_colour"
    part: str = Field(pattern=r"^p[0-9]+$")
    colour: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class DeleteEdit(_Edit):
    edit: Literal["delete"] = "delete"


class DuplicateEdit(_Edit):
    edit: Literal["duplicate"] = "duplicate"
    offset: Point = (20.0, 0.0, 0.0)


class RenameEdit(_Edit):
    edit: Literal["rename"] = "rename"
    name: str = Field(min_length=1, max_length=200)


class RemoveOpEdit(_Edit):
    edit: Literal["remove_op"] = "remove_op"
    op: int = Field(ge=0)


class ExposeEdit(_Edit):
    edit: Literal["expose"] = "expose"
    #: The op's index, or None for the object's transform.
    op: int | None = Field(ge=0)
    #: A field of it, a tuple's element by ``.index``: ``point.2``, ``diameter``.
    field: str = Field(pattern=r"^[a-z_]+(\.[0-2])?$")
    name: str


class UnexposeEdit(_Edit):
    edit: Literal["unexpose"] = "unexpose"
    op: int | None = Field(ge=0)
    field: str = Field(pattern=r"^[a-z_]+(\.[0-2])?$")


Edit = Annotated[
    TransformEdit | SplitEdit | LayFlatEdit | AddEdit | SubtractEdit | CustomEdit | SetColourEdit
    | DeleteEdit | DuplicateEdit | RenameEdit | RemoveOpEdit | ExposeEdit | UnexposeEdit,
    Field(discriminator="edit"),
]


def _floats(v: Sequence[Num]) -> list[float]:
    return [value(x) for x in v]


def _cuts(obj: SceneObject) -> list[tuple[Sequence[float], Sequence[float], Literal["above", "below"]]]:
    return [(_floats(op.point), op.normal, op.keep) for op in obj.ops if isinstance(op, Cut)]


def object_mesh(obj: SceneObject, mesh_of: MeshOf) -> trimesh.Trimesh:
    """The object's parts as one mesh in its own frame, before its ops."""
    return trimesh.util.concatenate([mesh_of(part.mesh) for part in obj.parts])


def _matrix(t: Transform) -> tuple[np.ndarray, np.ndarray]:
    return linear(_floats(t.rotate), _floats(t.scale)), np.asarray(_floats(t.translate))


def _find(scene: Scene, oid: str) -> int:
    for index, obj in enumerate(scene.objects):
        if obj.id == oid:
            return index
    raise SceneEditError(f"there is no object {oid} in the scene")


def _replace(scene: Scene, index: int, *objects: SceneObject) -> Scene:
    return scene.model_copy(update={"objects": [*scene.objects[:index], *objects, *scene.objects[index + 1 :]]})


def _split(scene: Scene, edit: SplitEdit, mesh_of: MeshOf) -> Scene:
    index = _find(scene, edit.object)
    obj = scene.objects[index]
    t = obj.transform
    try:
        point, normal = to_object_plane(_floats(t.translate), _floats(t.rotate), _floats(t.scale), edit.point, edit.normal)
    except ValueError as error:
        raise SceneEditError(str(error)) from None
    section = measure(object_mesh(obj, mesh_of), point, normal)
    if section is None:
        raise SceneEditError(f"the plane does not cut {obj.id}")
    p = (float(point[0]), float(point[1]), float(point[2]))
    n = (float(normal[0]), float(normal[1]), float(normal[2]))
    upper_id = next_object_id(scene)
    lower = obj.model_copy(update={"name": f"{obj.name} (bottom)", "ops": [*obj.ops, Cut(point=p, normal=n, keep="below")]})
    upper = obj.model_copy(update={"id": upper_id, "name": f"{obj.name} (top)", "ops": [*obj.ops, Cut(point=p, normal=n, keep="above")]})
    if edit.joint is not None:
        spec = edit.joint
        if not section.inside:
            raise SceneEditError(f"the section of {obj.id} at that plane has its centre outside it (a ring); cut elsewhere")
        diameter = spec.diameter if spec.diameter is not None else round(1.2 * section.room, 2)
        need = diameter / 2 + spec.clearance + WALL
        if section.room < need:
            raise SceneEditError(
                f"a {diameter:g} mm peg with {spec.clearance:g} mm clearance needs {need:.2f} mm of "
                f"section around its centre, and {obj.id} has {section.room:.2f} mm there; cut where "
                "it is wider, or give a smaller diameter"
            )
        axis = tuple(-x for x in n) if spec.peg_on == "above" else n
        at = (float(section.centroid[0]), float(section.centroid[1]), float(section.centroid[2]))
        joint = {"at": at, "axis": axis, "diameter": diameter, "depth": spec.depth, "clearance": spec.clearance, "key": spec.key}
        peg, socket = Joint(role="peg", **joint), Joint(role="socket", **joint)
        if spec.peg_on == "above":
            upper = upper.model_copy(update={"ops": [*upper.ops, peg]})
            lower = lower.model_copy(update={"ops": [*lower.ops, socket]})
        else:
            lower = lower.model_copy(update={"ops": [*lower.ops, peg]})
            upper = upper.model_copy(update={"ops": [*upper.ops, socket]})
    return _replace(scene, index, lower, upper)


def _largest_flat(obj: SceneObject, mesh_of: MeshOf) -> np.ndarray:
    """The object-frame outward normal of its largest flat face after its cuts."""
    mesh = object_mesh(obj, mesh_of)
    cuts = _cuts(obj)
    best_area, best = 0.0, np.array([0.0, 0.0, -1.0])
    centres = mesh.triangles_center
    for faces, area, normal in zip(mesh.facets, mesh.facets_area, mesh.facets_normal, strict=True):
        if area > best_area and len(kept(centres[faces], cuts)) == len(faces):
            best_area, best = float(area), np.asarray(normal)
    for point, normal, keep in cuts:
        section = measure(mesh, point, normal)
        if section is not None and section.area > best_area:
            best_area, best = section.area, unit(normal) * (1 if keep == "below" else -1)
    return best


def _lay_flat(scene: Scene, edit: LayFlatEdit, mesh_of: MeshOf) -> Scene:
    index = _find(scene, edit.object)
    obj = scene.objects[index]
    t = obj.transform
    rotate, scale = _floats(t.rotate), _floats(t.scale)
    if edit.normal == "largest_flat":
        direction = normal_to_world(rotate, scale, _largest_flat(obj, mesh_of))
    else:
        try:
            direction = unit(edit.normal)
        except ValueError:
            raise SceneEditError("lay_flat's normal is zero") from None
    new_rotate = euler(turn_to_down(direction) @ rotation(rotate))
    points = cut_points(object_mesh(obj, mesh_of), _cuts(obj))
    world = (linear(new_rotate, scale) @ points.T).T
    translate = _floats(t.translate)
    transform = Transform(translate=(translate[0], translate[1], float(-world[:, 2].min())), rotate=new_rotate, scale=t.scale)
    return _replace(scene, index, obj.model_copy(update={"transform": transform}))


def _primitive(scene: Scene, edit: AddEdit | SubtractEdit) -> Scene:
    index = _find(scene, edit.object)
    obj = scene.objects[index]
    centre: Point = (0.0, 0.0, 0.0)
    if edit.at is not None:
        m, translate = _matrix(obj.transform)
        local = np.linalg.solve(m, np.asarray(edit.at) - translate)
        centre = (round(float(local[0]), 6), round(float(local[1]), 6), round(float(local[2]), 6))
    shape = {"primitive": edit.primitive, "size": edit.size, "transform": Transform(translate=centre, rotate=edit.rotate)}
    op = Add(**shape) if isinstance(edit, AddEdit) else Subtract(**shape)
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


def _expose(scene: Scene, edit: ExposeEdit | UnexposeEdit) -> Scene:
    index = _find(scene, edit.object)
    obj = scene.objects[index]
    change = _exposing(edit.name if isinstance(edit, ExposeEdit) else None)
    if edit.op is None:
        updated = obj.model_copy(update={"transform": _set_field(obj.transform, edit.field, change)})
    else:
        if edit.op >= len(obj.ops):
            raise SceneEditError(f"{obj.id} has no op {edit.op}")
        ops = list(obj.ops)
        ops[edit.op] = _set_field(ops[edit.op], edit.field, change)  # type: ignore[assignment]
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
        transform = Transform(
            translate=edit.translate if edit.translate is not None else t.translate,
            rotate=edit.rotate if edit.rotate is not None else t.rotate,
            scale=edit.scale if edit.scale is not None else t.scale,
        )
        return _replace(scene, index, obj.model_copy(update={"transform": transform}))
    if isinstance(edit, CustomEdit):
        return _replace(scene, index, obj.model_copy(update={"ops": [*obj.ops, Custom(scad=edit.scad)]}))
    if isinstance(edit, SetColourEdit):
        if all(p.id != edit.part for p in obj.parts):
            raise SceneEditError(f"{obj.id} has no part {edit.part}")
        parts = [p.model_copy(update={"colour": edit.colour.upper()}) if p.id == edit.part else p for p in obj.parts]
        return _replace(scene, index, obj.model_copy(update={"parts": parts}))
    if isinstance(edit, DeleteEdit):
        if len(scene.objects) == 1:
            raise SceneEditError("the last object cannot be deleted; delete the model instead")
        return _replace(scene, index)
    if isinstance(edit, DuplicateEdit):
        t = obj.transform
        moved = [value(a) + b for a, b in zip(t.translate, edit.offset, strict=True)]
        copy = obj.model_copy(
            update={"id": next_object_id(scene), "name": f"{obj.name} (copy)", "transform": t.model_copy(update={"translate": tuple(moved)})}
        )
        return _replace(scene, index, obj, copy)
    if isinstance(edit, RenameEdit):
        return _replace(scene, index, obj.model_copy(update={"name": edit.name}))
    assert isinstance(edit, RemoveOpEdit)
    if edit.op >= len(obj.ops):
        raise SceneEditError(f"{obj.id} has no op {edit.op}")
    return _replace(scene, index, obj.model_copy(update={"ops": [*obj.ops[: edit.op], *obj.ops[edit.op + 1 :]]}))


def apply_edits(scene: Scene, edits: Sequence[BaseModel], mesh_of: MeshOf) -> Scene:
    for edit in edits:
        scene = _one(scene, edit, mesh_of)
    try:
        return Scene.model_validate(scene.model_dump())
    except ValueError as error:
        raise SceneEditError(str(error)) from None


def removed_parameters(before: Scene, after: Scene) -> list[str]:
    kept_names = exposed_values(after)
    return [name for name in exposed_values(before) if name not in kept_names]
```

`_largest_flat` counts a facet only when a cut keeps every one of its faces' centres, and a cut face by its measured section.

`_split` names the halves `(bottom)`/`(top)`; the test `test_edits_apply_in_order` expects `"Carrot (bottom)"` and a renamed `"Leaves"` on `o2`. Note the new object's id is `next_object_id`, so in a one-object scene the top half is `o2`; in `garden()` (two objects) it is `o3`, as `test_split_makes_two_objects_cut_on_each_side` expects.

In `test_a_peg_that_does_not_fit_is_refused_with_the_room_there_is`, `diameter=5.8, clearance=0` needs `2.9 + 0 + 0.8 = 3.70` and the neck has `3·cos(π/64) = 2.99`.

In `test_lay_flat_turns_the_direction_down_and_drops_to_the_bed`, the body's radius 10 is the farthest along +X, so it is lowest once +X points down.

In `test_set_colour_add_subtract_custom_duplicate_remove_op`, the duplicate's x is the original's 30 plus the default offset 20 = 50.

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/meshmodel -q`
Expected: PASS.

- [ ] **Step 5: Lint, type-check and commit**

```bash
cd backend && uv run --frozen ruff check scadbuddy/meshmodel tests/meshmodel && uv run --frozen ruff format scadbuddy/meshmodel tests/meshmodel && uv run --frozen mypy
git add backend/scadbuddy/meshmodel/edits.py backend/tests/meshmodel/test_edits.py
git commit -m "feat(meshmodel): scene edits — split with a keyed joint, lay flat, add, subtract, custom, expose (#2146)"
```

---

### Task 8: Operation kinds and routes

**Files:**
- Create: `backend/scadbuddy/meshmodel/operations.py`, `backend/scadbuddy/api/mesh_models.py`
- Modify: `backend/scadbuddy/api/limits.py` (add `MESH_UPLOAD_PATH` after `MEDIA_UPLOAD_PATH`), `backend/scadbuddy/main.py` (a `RouteLimit` beside the media upload's, near line 1128)
- Test: `backend/tests/api/test_mesh_models.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 5, 6, 7; `render.objects3mf.read_objects`, `UnreadableObjectsError`; `render.read_budget.ReadBudget`; `bambuddy.print_source.LibrarySource`, `printable`, `SLICED_TYPE`, `MAX_DOWNLOAD_BYTES`; `bambuddy.client.client_for`; `render.bambu3mf.stl_3mf`; `state.outputs.model_3mf(output_id)`; `state.presets.saved_presets(slug)`, `.template_presets(slug)`, `.update(slug, id, ParamPresetUpdate(params=…))`; `api.models._require_new`, `_slug_from_name`, `require_mine`, `require_model_exists`, `announce_source_change`; `api.media._receive`; `operations.claims.ClaimStore`, `Held`; `api.operations.run_operation`, `recorded`, `operation_answer`, `Claimed`; `core.events.emit`, `ModelEvent`; `core.problems.ApiError`.
- Produces: kinds `mesh_model_create`, `scene_edit`, `scene_eject` (queue `library`); routes `POST /api/v1/models/from-mesh` (JSON `{name, library_file_id?, output_id?, description?}` → 201 `ModelRecord`), `POST /api/v1/models/from-mesh/upload` (multipart `file`, `name`), `GET /api/v1/models/{slug}/scene` (→ `SceneView {scene, notes, hand_edited}`), `POST /api/v1/models/{slug}/scene/edits` (`{edits, drop_from_presets=false}` → `ModelRecord`), `POST /api/v1/models/{slug}/scene/eject` (→ `ModelRecord`).

Read the imports you need from `api/models.py` and `api/media.py` before writing (`grep -n "^from\|^import" backend/scadbuddy/api/media.py`), and `backend/scadbuddy/api/operations.py` for `run_operation`'s and `recorded`'s exact keywords; the code below uses them as `api/models.py` `_create_command` and `api/media.py` `upload_media` do.

- [ ] **Step 1: Write the failing API tests**

```python
# backend/tests/api/test_mesh_models.py
"""Mesh models through the API (#2146, spec 2026-10-11 §4–§6): create from a library
file, an upload or an output; read the scene; edit and eject, each one revision."""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
import respx
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.meshmodel.scene import load_scene, text_sha256
from scadbuddy.render.bambu3mf import stl_3mf
from tests.api.test_print_library import library_file
from tests.api.test_send import configure
from tests.support.operations import press

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]


def stl(mesh: trimesh.Trimesh) -> bytes:
    return mesh.export(file_type="stl")


def carrot() -> trimesh.Trimesh:
    from tests.meshmodel.test_section import neck

    return neck()


def upload(client: TestClient, name: str = "Carrot", data: bytes | None = None, filename: str = "carrot.stl") -> httpx.Response:
    return client.post(
        "/api/v1/models/from-mesh/upload",
        files={"file": (filename, data if data is not None else stl(carrot()), "application/octet-stream")},
        data={"name": name},
        headers=press(),
    )


def edit(client: TestClient, slug: str, *edits: dict[str, Any], drop: bool = False, key: dict[str, str] | None = None) -> httpx.Response:
    return client.post(
        f"/api/v1/models/{slug}/scene/edits",
        json={"edits": list(edits), "drop_from_presets": drop},
        headers=key or press(),
    )


def test_an_uploaded_stl_opens_as_a_scene_model(client: TestClient, paths: DataPaths) -> None:
    created = upload(client)
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    directory = paths.model_dir(slug)
    scene = load_scene((directory / "scene.json").read_text())
    assert [o.id for o in scene.objects] == ["o1"]
    assert (directory / "meshes" / "o1-p1.3mf").is_file()
    scad = (directory / "model.scad").read_text()
    assert scene.generated_sha256 == text_sha256(scad)
    assert client.get(f"/api/v1/models/{slug}").json()["version"]


def test_a_library_file_opens_as_a_scene_model_and_bambuddy_is_not_written(client: TestClient) -> None:
    configure(client)
    with respx.mock:
        library_file(688, content=stl_3mf(stl(carrot()), model_name="carrot"), file_type="3mf")
        created = client.post(
            "/api/v1/models/from-mesh", json={"name": "Carrot Garden", "library_file_id": 688}, headers=press()
        )
        writes = [c for c in respx.calls if c.request.method != "GET"]
    assert created.status_code == 201, created.text
    assert writes == []
    scene = client.get(f"/api/v1/models/{created.json()['slug']}/scene").json()
    assert scene["scene"]["source"]["kind"] == "bambuddy_library"
    assert scene["scene"]["source"]["file_id"] == 688


def test_a_sliced_library_file_is_refused(client: TestClient) -> None:
    configure(client)
    with respx.mock:
        respx.get(url__regex=r".*/library/files/77$").mock(
            return_value=httpx.Response(200, json={"id": 77, "filename": "x.gcode.3mf", "file_type": "gcode.3mf"})
        )
        refused = client.post("/api/v1/models/from-mesh", json={"name": "X", "library_file_id": 77}, headers=press())
    assert refused.status_code == 422, refused.text


def test_a_create_names_exactly_one_source(client: TestClient) -> None:
    both = client.post(
        "/api/v1/models/from-mesh", json={"name": "X", "library_file_id": 1, "output_id": "abc"}, headers=press()
    )
    assert both.status_code == 422


def test_an_unreadable_upload_is_refused(client: TestClient) -> None:
    refused = upload(client, data=b"not a mesh", filename="x.3mf")
    assert refused.status_code == 422, refused.text


def test_an_obj_upload_is_refused(client: TestClient) -> None:
    refused = upload(client, data=b"v 0 0 0\n", filename="x.obj")
    assert refused.status_code == 415, refused.text


def test_split_with_a_joint_is_one_revision(client: TestClient, paths: DataPaths) -> None:
    slug = upload(client).json()["slug"]
    before = len(client.get(f"/api/v1/models/{slug}/versions").json())
    done = edit(client, slug, {"edit": "split_at_plane", "object": "o1", "point": [0, 0, 32], "normal": [0, 0, 1], "joint": {"depth": 5}})
    assert done.status_code == 200, done.text
    assert len(client.get(f"/api/v1/models/{slug}/versions").json()) == before + 1
    scene = load_scene((paths.model_dir(slug) / "scene.json").read_text())
    assert [o.id for o in scene.objects] == ["o1", "o2"]
    assert scene.generated_sha256 == text_sha256((paths.model_dir(slug) / "model.scad").read_text())


def test_a_repeated_edit_is_one_revision(client: TestClient) -> None:
    slug = upload(client).json()["slug"]
    key = press()
    body = {"edit": "rename", "object": "o1", "name": "Body"}
    first = edit(client, slug, body, key=key)
    again = edit(client, slug, body, key=key)
    assert first.status_code == again.status_code == 200
    assert first.json()["version"] == again.json()["version"]


def test_a_refused_edit_is_a_422_naming_why(client: TestClient) -> None:
    slug = upload(client).json()["slug"]
    refused = edit(client, slug, {"edit": "split_at_plane", "object": "o1", "point": [0, 0, 500], "normal": [0, 0, 1]})
    assert refused.status_code == 422
    assert "does not cut o1" in refused.json()["detail"]


def test_a_hand_edited_model_refuses_scene_edits(client: TestClient, paths: DataPaths) -> None:
    slug = upload(client).json()["slug"]
    put = client.put(f"/api/v1/models/{slug}/source", json={"source": "cube(1);\n", "force": True}, headers=press())
    assert put.status_code == 200, put.text
    refused = edit(client, slug, {"edit": "rename", "object": "o1", "name": "x"})
    assert refused.status_code == 409
    assert "eject" in refused.json()["detail"]
    assert client.get(f"/api/v1/models/{slug}/scene").json()["hand_edited"] is True


def test_removing_a_parameter_a_saved_preset_names_is_refused_unless_dropped(client: TestClient) -> None:
    slug = upload(client).json()["slug"]
    assert edit(
        client, slug, {"edit": "expose", "object": "o1", "op": None, "field": "scale.2", "name": "height_scale"}
    ).status_code == 200
    saved = client.post(
        f"/api/v1/models/{slug}/presets", json={"name": "Tall", "params": {"height_scale": 2}}, headers=press()
    )
    assert saved.status_code in (200, 201), saved.text
    unexpose = {"edit": "unexpose", "object": "o1", "op": None, "field": "scale.2"}
    refused = edit(client, slug, unexpose)
    assert refused.status_code == 409 and "Tall" in refused.json()["detail"]
    assert edit(client, slug, unexpose, drop=True).status_code == 200
    presets = client.get(f"/api/v1/models/{slug}/presets").json()
    assert "height_scale" not in next(p for p in presets if p["name"] == "Tall")["params"]


def test_eject_leaves_a_plain_template_that_keeps_its_provenance(client: TestClient, paths: DataPaths) -> None:
    slug = upload(client).json()["slug"]
    ejected = client.post(f"/api/v1/models/{slug}/scene/eject", headers=press())
    assert ejected.status_code == 200, ejected.text
    assert not (paths.model_dir(slug) / "scene.json").exists()
    assert (paths.model_dir(slug) / "meshes" / "o1-p1.3mf").is_file()
    assert client.get(f"/api/v1/models/{slug}/scene").status_code == 404
    assert client.get(f"/api/v1/models/{slug}").json()["meta"]["mesh_source"]["kind"] == "upload"
```

Check each request path in these tests against the real routes before running (`/models/{slug}/source` PUT body, `/models/{slug}/presets` POST body, `/models/{slug}/versions`, and where `ModelRecord` puts the meta: `grep -n "class ModelRecord" -A30 backend/scadbuddy/library/catalogue.py`); fix the test's paths and field names to match, not the routes.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_mesh_models.py -q`
Expected: FAIL with 404s (no routes). Without `SCADBUDDY_TEST_DATABASE_URL` and a Temporal they skip: start them (CLAUDE.md "Commands") before going on.

- [ ] **Step 3: Write the kinds**

```python
# backend/scadbuddy/meshmodel/operations.py
"""Mesh models' operations (#2146, spec 2026-10-11 §6): ``mesh_model_create``,
``scene_edit`` and ``scene_eject``, on the ``library`` queue, whose worker runs in the
API process with the data volume and the Bambuddy client. The source's bytes arrive by
claim (an upload) or are read in the run (a library file, an output); a scene edit reads
the scene under the history's write lock (`Catalogue.update_files`)."""

from __future__ import annotations

import asyncio
import hashlib
import io
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import trimesh
from fastapi import status
from pydantic import TypeAdapter

from scadbuddy.api import models as models_api
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.print_source import SLICED_TYPE, LibrarySource, printable
from scadbuddy.core.events import ModelEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import ModelExistsError, ModelMeta
from scadbuddy.library.operations import answered_as_routes
from scadbuddy.library.presets import ParamPresetUpdate
from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.edits import Edit, SceneEditError, apply_edits, removed_parameters
from scadbuddy.meshmodel.ingest import scene_from_objects
from scadbuddy.meshmodel.scene import SCENE_NAME, Scene, SceneSource, dump_scene, load_scene, text_sha256
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import KindsBuild, OperationKind
from scadbuddy.render.bambu3mf import stl_3mf
from scadbuddy.render.objects3mf import UnreadableObjectsError, read_objects
from scadbuddy.render.read_budget import ReadBudget

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState
    from scadbuddy.core.components import Components, Core

EDITS = TypeAdapter(list[Edit])
HAND_EDITED = (
    "model.scad was edited by hand since the scene last wrote it; eject the scene "
    "(scene_eject) to keep editing it as source"
)


def compiled(scene: Scene) -> tuple[Scene, str]:
    text = compile_scene(scene)
    return scene.model_copy(update={"generated_sha256": text_sha256(text)}), text


def hand_edited(directory: Path, scene: Scene) -> bool:
    return text_sha256((directory / "model.scad").read_text(encoding="utf-8")) != scene.generated_sha256


def mesh_loader(directory: Path) -> Any:
    cache: dict[str, trimesh.Trimesh] = {}

    def mesh_of(path: str) -> trimesh.Trimesh:
        if path not in cache:
            cache[path] = trimesh.load(io.BytesIO((directory / path).read_bytes()), file_type="3mf", force="mesh")
        return cache[path]

    return mesh_of


def mesh_kinds(state: AppState) -> list[OperationKind]:
    async def source_bytes(request: dict[str, Any]) -> tuple[bytes, SceneSource]:
        source = request["source"]
        if source["kind"] == "upload":
            try:
                data = await asyncio.to_thread(ClaimStore(state.paths.claims).get, source["claim"])
            except LookupError:
                raise ApiError(status.HTTP_409_CONFLICT, "this upload is no longer held; send it again") from None
            name = source["name"]
            if name.lower().endswith(".stl"):
                data = await asyncio.to_thread(stl_3mf, data, model_name=Path(name).stem or "model")
        elif source["kind"] == "output":
            found = await state.outputs.model_3mf(source["output_id"])
            if found is None:
                raise ApiError(status.HTTP_404_NOT_FOUND, f"output {source['output_id']!r} has no 3MF")
            data, name = found, f"{source['output_id']}.3mf"
        else:
            async with client_for(state.settings_store.load()) as client:
                file = await client.library_file(source["file_id"])
                kind = (file.file_type or "").lower()
                library = LibrarySource(file_id=file.id, colours=[], plates=[], filename=file.filename, file_type=kind)
                payload = await library.fetch_3mf(client)
            if payload is None:
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{file.filename} is too large to read, or holds no mesh")
            data, name = payload, file.filename
        return data, SceneSource(
            kind=source["kind"],
            name=name,
            sha256=hashlib.sha256(data).hexdigest(),
            file_id=source.get("file_id"),
            output_id=source.get("output_id"),
        )

    async def create_check(request: dict[str, Any]) -> dict[str, Any]:
        models_api._require_new(state.catalogue, request["slug"])
        source = request["source"]
        if source["kind"] == "bambuddy_library":
            async with client_for(state.settings_store.load()) as client:
                file = await client.library_file(source["file_id"])
            kind = (file.file_type or "").lower()
            if kind == SLICED_TYPE or not printable(kind):
                what = "sliced" if kind == SLICED_TYPE else f"a {kind or 'file of unknown type'}"
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"{file.filename} is {what}; only an unsliced 3MF or an STL opens as a mesh model",
                )
        return {}

    async def create_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        data, source = await source_bytes(request)
        try:
            read = await asyncio.to_thread(read_objects, data, ReadBudget())
        except UnreadableObjectsError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        scene, files = scene_from_objects(read, source)
        scene, text = compiled(scene)
        files[SCENE_NAME] = dump_scene(scene).encode()
        meta = ModelMeta(name=request["name"], description=request.get("description") or "", mesh_source=source)
        try:
            record = await asyncio.to_thread(partial(state.catalogue.create, request["slug"], text, meta, files=files))
        except ModelExistsError:
            raise ApiError(status.HTTP_409_CONFLICT, f"a model named {request['slug']!r} already exists") from None
        emit(state.events, ModelEvent(kind="model.created", slug=request["slug"]))
        dumped: dict[str, Any] = record.model_dump(mode="json")
        return dumped

    def scene_of(slug: str) -> tuple[Path, Scene]:
        directory = state.catalogue.paths.model_dir(slug)
        path = directory / SCENE_NAME
        if not path.is_file():
            raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} is not a mesh model (it has no scene)")
        return directory, load_scene(path.read_text(encoding="utf-8"))

    async def edit_check(request: dict[str, Any]) -> dict[str, Any]:
        models_api.require_model_exists(state.catalogue, request["slug"])
        directory, scene = await asyncio.to_thread(scene_of, request["slug"])
        if await asyncio.to_thread(hand_edited, directory, scene):
            raise ApiError(status.HTTP_409_CONFLICT, HAND_EDITED)
        EDITS.validate_python(request["edits"])
        return {}

    async def edit_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]
        edits = EDITS.validate_python(request["edits"])
        dropped: list[str] = []
        removed_names: list[str] = []

        def compute(directory: Path) -> dict[str, bytes | None]:
            _, scene = scene_of(slug)
            if hand_edited(directory, scene):
                raise ApiError(status.HTTP_409_CONFLICT, HAND_EDITED)
            try:
                after = apply_edits(scene, edits, mesh_loader(directory))
            except SceneEditError as error:
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
            removed = removed_parameters(scene, after)
            if removed:
                own = [p.name for p in state.presets.template_presets(slug) if set(removed) & set(p.params)]
                if own:
                    raise ApiError(
                        status.HTTP_409_CONFLICT,
                        f"the model's own presets {', '.join(own)} set {', '.join(removed)}; edit them first",
                    )
                saved = [p for p in state.presets.saved_presets(slug) if set(removed) & set(p.params)]
                if saved and not request["drop_from_presets"]:
                    raise ApiError(
                        status.HTTP_409_CONFLICT,
                        f"the saved presets {', '.join(p.name for p in saved)} set {', '.join(removed)}; "
                        "send drop_from_presets to remove it from them",
                    )
                dropped.extend(p.id for p in saved)
                removed_names.extend(removed)
            after, text = compiled(after)
            return {SCENE_NAME: dump_scene(after).encode(), "model.scad": text.encode()}

        record = await asyncio.to_thread(partial(state.catalogue.update_files, slug, compute, message=request["message"]))
        # After the revision, so no render between them sees a preset naming a parameter
        # the template no longer has for longer than this takes.
        for preset in await asyncio.to_thread(state.presets.saved_presets, slug):
            if preset.id in dropped:
                params = {k: v for k, v in preset.params.items() if k not in removed_names}
                await asyncio.to_thread(state.presets.update, slug, preset.id, ParamPresetUpdate(params=params))
        models_api.announce_source_change(state.events, slug)
        dumped: dict[str, Any] = record.model_dump(mode="json")
        return dumped

    async def eject_check(request: dict[str, Any]) -> dict[str, Any]:
        models_api.require_model_exists(state.catalogue, request["slug"])
        await asyncio.to_thread(scene_of, request["slug"])
        return {}

    async def eject_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        record = await asyncio.to_thread(
            partial(state.catalogue.update_files, request["slug"], lambda _: {SCENE_NAME: None}, message="Eject the scene")
        )
        models_api.announce_source_change(state.events, request["slug"])
        dumped: dict[str, Any] = record.model_dump(mode="json")
        return dumped

    def kind(name: str, check: Any, run: Any) -> OperationKind:
        return OperationKind(name, answered_as_routes(check), answered_as_routes(run), queue="library")

    return [
        kind("mesh_model_create", create_check, create_run),
        kind("scene_edit", edit_check, edit_run),
        kind("scene_eject", eject_check, eject_run),
    ]


def _kinds(core: Core, components: Components) -> list[OperationKind]:
    from scadbuddy.api.deps import AppState

    if not isinstance(core, AppState):
        raise TypeError(f"the mesh model kinds need the API's AppState, not a {type(core).__name__}")
    return mesh_kinds(core)


OPERATION_KINDS: KindsBuild = _kinds
```

An `ApiError` raised inside `compute` (in a thread, under the history's write lock) reaches the operation as the route's answer: `_commit_change` re-raises anything `change` raises before committing, and `answered_as_routes` passes `ApiError` through. Confirm both by the 422 and 409 tests rather than assuming.

A request's `message` comes from the route (below), default `"Edit the scene"`.

- [ ] **Step 4: Write the routes**

```python
# backend/scadbuddy/api/mesh_models.py
"""Mesh models (#2146, spec 2026-10-11): open a 3MF or STL as a model whose scene
compiles to its source, read the scene, edit it, eject it. Every write is an operation
(`meshmodel/operations.py`)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Literal

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from scadbuddy.api.deps import CatalogueDep
from scadbuddy.api.media import _receive
from scadbuddy.api.models import _require_new, _slug_from_name, require_mine, require_model_exists
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    recorded,
    run_operation,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import ModelRecord
from scadbuddy.meshmodel.edits import Edit
from scadbuddy.meshmodel.operations import hand_edited
from scadbuddy.meshmodel.scene import SCENE_NAME, Scene, load_scene
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationsDep

router = APIRouter(tags=["mesh models"])

ACCEPTED_SUFFIXES = (".3mf", ".stl")


class MeshModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    library_file_id: int | None = Field(default=None, ge=1)
    output_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _one_source(self) -> MeshModelCreate:
        if (self.library_file_id is None) == (self.output_id is None):
            raise ValueError("name exactly one of library_file_id and output_id")
        return self


class SceneEdits(BaseModel):
    edits: list[Edit] = Field(min_length=1, max_length=50)
    drop_from_presets: bool = False
    message: str | None = Field(default=None, max_length=200)


class SceneView(BaseModel):
    scene: Scene
    notes: list[str]
    #: model.scad no longer matches what the scene compiled; edits are refused.
    hand_edited: bool


async def _create(
    ops: Any, response: Response, catalogue: Any, key: str | None, *, name: str, description: str, source: dict[str, Any], held: list[Held]
) -> ModelRecord | JSONResponse:
    slug = _slug_from_name(name)
    request = {"slug": slug, "name": name, "description": description, "source": source}
    kind = ops.kinds["mesh_model_create"]
    result = await run_operation(
        ops, response, kind=kind, subject=slug, request=request, idempotency_key=key,
        claimed=Claimed(ClaimStore(catalogue.paths.claims), held),
    )
    return operation_answer(result, ModelRecord)


@router.post(
    "/models/from-mesh",
    response_model=ModelRecord,
    status_code=status.HTTP_201_CREATED,
    responses=OPERATION_RESPONSES,
    summary="Open a library file or an output as an editable mesh model",
    description=(
        "Reads the 3MF (or STL) of a Bambuddy library file or an output, writes one mesh "
        "per object and colour into the new model's `meshes/`, and a `scene.json` that "
        "compiles to its `model.scad` (spec 2026-10-11). Nothing is written to Bambuddy. "
        "A sliced file is a 422."
    ),
)
async def create_from_mesh(
    body: MeshModelCreate, response: Response, ops: OperationsDep, catalogue: CatalogueDep, idempotency_key: IdempotencyKey = None
) -> ModelRecord | JSONResponse:
    _require_new(catalogue, _slug_from_name(body.name))
    source: dict[str, Any] = (
        {"kind": "bambuddy_library", "file_id": body.library_file_id}
        if body.library_file_id is not None
        else {"kind": "output", "output_id": body.output_id}
    )
    return await _create(ops, response, catalogue, idempotency_key, name=body.name, description=body.description, source=source, held=[])


@router.post(
    "/models/from-mesh/upload",
    response_model=ModelRecord,
    status_code=status.HTTP_201_CREATED,
    responses={415: {"description": "Not a .3mf or .stl"}, **OPERATION_RESPONSES},
    summary="Open an uploaded 3MF or STL as an editable mesh model",
)
async def create_from_upload(
    request: Request, response: Response, ops: OperationsDep, catalogue: CatalogueDep, idempotency_key: IdempotencyKey = None
) -> ModelRecord | JSONResponse:
    received = await _receive(request, catalogue.paths.cache)
    claims = ClaimStore(catalogue.paths.claims)
    held: list[Held] = []
    try:
        path = received.files.get("file")
        filename = received.filenames.get("file", "") if hasattr(received, "filenames") else ""
        name = (received.fields.get("name") or "").strip() if hasattr(received, "fields") else ""
        if path is None or received.sizes["file"] == 0:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "send the mesh as the `file` part")
        if not filename.lower().endswith(ACCEPTED_SUFFIXES):
            raise ApiError(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "a mesh model opens a .3mf or an .stl")
        if not name:
            name = filename.rsplit(".", 1)[0]
        _require_new(catalogue, _slug_from_name(name))
        digest = received.hashes["file"].hexdigest()
        source = {"kind": "upload", "claim": digest, "name": filename}
        held.append(await asyncio.to_thread(claims.hold_file, path, digest))
    finally:
        received.discard()
    return await _create(ops, response, catalogue, idempotency_key, name=name, description="", source=source, held=held)


@router.get("/models/{slug}/scene", response_model=SceneView, summary="A mesh model's scene")
async def get_scene(slug: str, catalogue: CatalogueDep) -> SceneView:
    require_model_exists(catalogue, slug)
    directory = catalogue.paths.model_dir(slug)

    def read() -> SceneView:
        path = directory / SCENE_NAME
        if not path.is_file():
            raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} is not a mesh model (it has no scene)")
        scene = load_scene(path.read_text(encoding="utf-8"))
        return SceneView(scene=scene, notes=scene.notes, hand_edited=hand_edited(directory, scene))

    return await asyncio.to_thread(read)


@router.post(
    "/models/{slug}/scene/edits",
    response_model=ModelRecord,
    responses=OPERATION_RESPONSES,
    summary="Edit a mesh model's scene as one revision",
    description=(
        "Applies the edits in order (spec 2026-10-11 §5). 409 when model.scad was edited "
        "by hand, or when the edits remove a parameter a preset sets (unless "
        "`drop_from_presets`); 422 for an edit the scene refuses."
    ),
)
async def edit_scene(
    slug: str, body: SceneEdits, response: Response, ops: OperationsDep, catalogue: CatalogueDep, idempotency_key: IdempotencyKey = None
) -> ModelRecord | JSONResponse:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    request = {
        "slug": slug,
        "edits": [e.model_dump(mode="json") for e in body.edits],
        "drop_from_presets": body.drop_from_presets,
        "message": body.message or "Edit the scene",
    }
    result = await run_operation(ops, response, kind=ops.kinds["scene_edit"], subject=slug, request=request, idempotency_key=idempotency_key)
    return operation_answer(result, ModelRecord)


@router.post(
    "/models/{slug}/scene/eject",
    response_model=ModelRecord,
    responses=OPERATION_RESPONSES,
    summary="Stop editing a mesh model as a scene",
    description="Removes scene.json; model.scad stays and is edited as source from then on.",
)
async def eject_scene(
    slug: str, response: Response, ops: OperationsDep, catalogue: CatalogueDep, idempotency_key: IdempotencyKey = None
) -> ModelRecord | JSONResponse:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    result = await run_operation(ops, response, kind=ops.kinds["scene_eject"], subject=slug, request={"slug": slug}, idempotency_key=idempotency_key)
    return operation_answer(result, ModelRecord)
```

Match these to the real helpers before running: read `_Received` in `api/media.py` for how it exposes a part's file name and a text field (the code above guards with `hasattr`; replace that with the real attribute names, and if `_Received` keeps no file names, add a `filenames: dict[str, str]` filled in `_Receiver.on_headers_finished` from the part's `Content-Disposition`, with a test in `tests/api/test_media.py`); read `api/operations.py` for the real names of `OPERATION_RESPONSES`, `IdempotencyKey`, `Claimed`, `operation_answer`, `run_operation` (`api/models.py` imports them; copy its import lines). Use the `SlugPath` annotation `api/models.py` uses for `slug`. Drop `hashlib` and `Literal` if unused.

- [ ] **Step 5: Gate the upload's body size**

In `api/limits.py`, after `MEDIA_UPLOAD_PATH`:

```python
#: A mesh model's upload (#2146): a 3MF up to the library download cap, streamed like
#: a media upload, so it has its own limit in place of the multipart cap.
MESH_UPLOAD_PATH = re.compile(r"^/api/v1/models/from-mesh/upload$")
```

In `main.py`, in the `BodySizeGate` routes list after the media upload's:

```python
            RouteLimit(
                "POST",
                MESH_UPLOAD_PATH,
                MAX_DOWNLOAD_BYTES + 64 * 1024,  # the file plus the form's framing
                "a mesh upload",
                "the library download cap (bambuddy/print_source.py MAX_DOWNLOAD_BYTES)",
            ),
```

with the imports `from scadbuddy.api.limits import MESH_UPLOAD_PATH` (beside `MEDIA_UPLOAD_PATH`'s) and `from scadbuddy.bambuddy.print_source import MAX_DOWNLOAD_BYTES`.

- [ ] **Step 6: Run the API tests and the route-uniqueness test**

Run: `cd backend && uv run --frozen pytest tests/api/test_mesh_models.py tests/api/test_routes.py -q`
Expected: PASS. `tests/api/test_routes.py` fails if `/models/from-mesh` collides with a `POST /models/{slug}` route; if it does, move both create routes to `/mesh-models` and `/mesh-models/upload` and update the tests and the agent tools in Task 10.

- [ ] **Step 7: The whole backend suite, lint, types; commit**

```bash
cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest -n auto -q
git add backend/scadbuddy/meshmodel/operations.py backend/scadbuddy/api/mesh_models.py backend/scadbuddy/api/limits.py backend/scadbuddy/main.py backend/tests/api/test_mesh_models.py
git commit -m "feat(api): mesh models — create from a library file, an upload or an output; edit and eject the scene (#2146)"
```

If an OpenAPI or operation-kind registry test fails (a kind list snapshot, `test_operation_kinds`), update its expectation to include the three kinds.

---

### Task 9: Render it for real (`requires_openscad`)

**Files:**
- Create: `backend/tests/test_mesh_models_openscad.py`
- Modify: `docs/superpowers/specs/2026-09-22-scadbuddy-design.md` §3 (one bullet, after the test passes in the image)

**Interfaces:**
- Consumes: Tasks 1, 3, 5, 7; `tests/conftest.py` `openscad_binary()`; `render.split.split_by_material`.

- [ ] **Step 1: Write the test**

```python
# backend/tests/test_mesh_models_openscad.py
"""A mesh model rendered by the real OpenSCAD (#2146, spec 2026-10-11 §2, §10): the
import of a colourless part 3MF in `color()`, `set_colour`, and a split with a joint
whose socket is the peg grown by the clearance. Runs only where `openscad` is on PATH:
the image (`docker build --target test`)."""

from __future__ import annotations

import math
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import trimesh
from pydantic import TypeAdapter

from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.edits import Edit, apply_edits
from scadbuddy.meshmodel.ingest import scene_from_objects
from scadbuddy.meshmodel.scene import Joint, Scene, SceneSource, value
from scadbuddy.meshmodel.section import measure
from scadbuddy.render.objects3mf import ReadObject
from scadbuddy.render.split import ColourPart, split_by_material
from tests.conftest import openscad_binary
from tests.meshmodel.test_section import neck

pytestmark = pytest.mark.requires_openscad

SOURCE = SceneSource(kind="upload", name="neck.stl", sha256="ab" * 32)
EDITS = TypeAdapter(list[Edit])


def written(directory: Path) -> Scene:
    read = [ReadObject(name="Carrot", count=1, parts=[ColourPart(0, "Body", "#F08A24", neck())])]
    scene, files = scene_from_objects(read, SOURCE)
    for name, data in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
    return scene


def mesh_of(directory: Path) -> Callable[[str], trimesh.Trimesh]:
    return lambda path: trimesh.load(directory / path, file_type="3mf", force="mesh")


def render(directory: Path, scene: Scene) -> list[ColourPart]:
    (directory / "model.scad").write_text(compile_scene(scene))
    binary = openscad_binary()
    assert binary is not None
    subprocess.run(
        [binary, "--backend=Manifold", "-o", str(directory / "out.3mf"), str(directory / "model.scad")],
        check=True,
        capture_output=True,
        timeout=300,
    )
    return [part for part in split_by_material(directory / "out.3mf") if len(part.mesh.faces)]


def polygon_area(sides: int, radius: float) -> float:
    return 0.5 * sides * radius**2 * math.sin(2 * math.pi / sides)


def test_an_imported_part_renders_in_the_scenes_colour(tmp_path: Path) -> None:
    assert [p.colour for p in render(tmp_path, written(tmp_path))] == ["#F08A24"]


def test_set_colour_changes_the_render(tmp_path: Path) -> None:
    scene = written(tmp_path)
    recoloured = apply_edits(
        scene,
        EDITS.validate_python([{"edit": "set_colour", "object": "o1", "part": "p1", "colour": "#00FF00"}]),
        mesh_of(tmp_path),
    )
    assert [p.colour for p in render(tmp_path, recoloured)] == ["#00FF00"]


def test_a_joint_renders_two_closed_parts_and_a_socket_grown_by_the_clearance(tmp_path: Path) -> None:
    # One object rests on z = 0 centred on the origin, so world and object frames agree.
    split = apply_edits(
        written(tmp_path),
        EDITS.validate_python([{
            "edit": "split_at_plane", "object": "o1", "point": [0, 0, 32], "normal": [0, 0, 1],
            "joint": {"depth": 2, "clearance": 0.2, "key": "none"},
        }]),
        mesh_of(tmp_path),
    )
    bodies = {}
    for obj in split.objects:
        parts = render(tmp_path, split.model_copy(update={"objects": [obj]}))
        bodies[obj.id] = trimesh.util.concatenate([p.mesh for p in parts])
        assert bodies[obj.id].is_watertight, obj.id
    socket = split.objects[0].ops[-1]
    assert isinstance(socket, Joint) and socket.role == "socket"
    # 1 mm below the cut, the lower part is the neck (a 64-gon of radius 3) less the
    # socket (a 48-gon of radius d/2 + clearance).
    section = measure(bodies["o1"], (0, 0, 31), (0, 0, 1))
    assert section is not None
    hole = value(socket.diameter) / 2 + value(socket.clearance)
    assert section.area == pytest.approx(polygon_area(64, 3) - polygon_area(48, hole), rel=0.02)
```

- [ ] **Step 2: Run it in the test image**

```bash
docker build --target test -t scadbuddy:test .
docker run --rm scadbuddy:test uv run --frozen --no-sync pytest tests/test_mesh_models_openscad.py -q
```

Expected: PASS (it skips on a host without `openscad`).

- [ ] **Step 3: Record the fact in spec 2026-09-22 §3**

Add one bullet under §3, stating: OpenSCAD `dev.2026-10-05` (the pinned image) imports a core 3MF with no materials inside `color(c)`, renders it in `c`, and cuts it with `intersection()`/`difference()` (measured by `backend/tests/test_mesh_models_openscad.py` in `scadbuddy:test`, with the date of that run); and update spec 2026-10-11 §2's first bullet to say it was re-measured on the pinned image.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_mesh_models_openscad.py docs/superpowers/specs/2026-09-22-scadbuddy-design.md docs/superpowers/specs/2026-10-11-mesh-models-design.md
git commit -m "test(meshmodel): render a split, jointed and recoloured scene in the real OpenSCAD (#2146)"
```

---

### Task 10: Agent tools

**Files:**
- Create: `agent/src/tools/meshModels.ts`, `agent/test/meshModelTools.test.ts`
- Modify: `agent/src/tools/index.ts` (add `...meshModelTools` to `ALL_TOOLS`)

**Interfaces:**
- Consumes: Task 8's routes (through the generated client, `pnpm gen:api`); `command`, `json`, `ok`, `slug`, `decodeBase64`, `fileForm`, `defineTool`.
- Produces: tools `create_model_from_mesh`, `get_scene`, `edit_scene`, `eject_scene`.

- [ ] **Step 1: Write the failing test**

Model it on `agent/test/fileTools.test.ts`: a fake backend through `createBackendClient(BACKEND, handler)`, the tool run with `runTool`, the requests recorded.

```ts
// agent/test/meshModelTools.test.ts
import { describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #2146: mesh models. Every write is a command (an Idempotency-Key, 202 followed).

type Seen = { method: string; path: string; body: unknown; key: string | null }

function backend() {
  const seen: Seen[] = []
  const client = createBackendClient(BACKEND, async (request) => {
    const r = request as Request
    const url = new URL(r.url)
    const type = r.headers.get('content-type') ?? ''
    const body = type.includes('json') ? await r.json() : type.includes('multipart') ? 'multipart' : undefined
    seen.push({ method: r.method, path: url.pathname, body, key: r.headers.get('Idempotency-Key') })
    if (r.method === 'GET' && url.pathname === '/api/v1/models/carrot/scene')
      return Response.json({ scene: { version: 1, objects: [{ id: 'o1' }] }, notes: [], hand_edited: false })
    return Response.json({ slug: 'carrot', name: 'Carrot', version: 'a'.repeat(40) }, { status: r.method === 'POST' && url.pathname.endsWith('from-mesh') ? 201 : 200 })
  })
  return { seen, client }
}

function tool(name: string) {
  const found = ALL_TOOLS.find((t) => t.name === name)
  if (!found) throw new Error(`no tool ${name}`)
  return found
}

async function call(name: string, args: unknown, client: ReturnType<typeof backend>['client']) {
  const ctx = { ...services(), backend: client, tiers: tiersUpTo('write') } as unknown as ToolContext
  return runTool(tool(name), args, ctx)
}

describe('mesh model tools', () => {
  it('creates from a library file as a command', async () => {
    const { seen, client } = backend()
    await call('create_model_from_mesh', { name: 'Carrot', library_file_id: 688 }, client)
    expect(seen).toEqual([
      expect.objectContaining({ method: 'POST', path: '/api/v1/models/from-mesh', body: { name: 'Carrot', library_file_id: 688, description: '' } }),
    ])
    expect(seen[0]!.key).toBeTruthy()
  })

  it('uploads base64 as multipart', async () => {
    const { seen, client } = backend()
    await call('create_model_from_mesh', { name: 'Carrot', filename: 'carrot.stl', content_base64: Buffer.from('solid x').toString('base64') }, client)
    expect(seen[0]).toEqual(expect.objectContaining({ path: '/api/v1/models/from-mesh/upload', body: 'multipart' }))
  })

  it('refuses no source or two sources', async () => {
    const { client } = backend()
    await expect(call('create_model_from_mesh', { name: 'X' }, client)).rejects.toThrow()
    await expect(call('create_model_from_mesh', { name: 'X', library_file_id: 1, output_id: 'o' }, client)).rejects.toThrow()
  })

  it('reads the scene', async () => {
    const { client } = backend()
    const result = await call('get_scene', { slug: 'carrot' }, client)
    expect(firstText(result)).toContain('"hand_edited": false')
  })

  it('sends edits in order as one command', async () => {
    const { seen, client } = backend()
    const edits = [
      { edit: 'split_at_plane', object: 'o1', point: [0, 0, 32], normal: [0, 0, 1], joint: { depth: 5 } },
      { edit: 'lay_flat', object: 'o2', normal: 'largest_flat' },
    ]
    await call('edit_scene', { slug: 'carrot', edits }, client)
    expect(seen[0]).toEqual(expect.objectContaining({ method: 'POST', path: '/api/v1/models/carrot/scene/edits', body: { edits, drop_from_presets: false } }))
  })

  it('ejects', async () => {
    const { seen, client } = backend()
    await call('eject_scene', { slug: 'carrot' }, client)
    expect(seen[0]).toEqual(expect.objectContaining({ method: 'POST', path: '/api/v1/models/carrot/scene/eject' }))
  })

  it('no mesh tool is outward: nothing is sent to Bambuddy', () => {
    for (const name of ['create_model_from_mesh', 'get_scene', 'edit_scene', 'eject_scene']) expect(tool(name).risk).not.toBe('outward')
  })
})
```

Adapt `call` to `runTool`'s real signature and the `ToolContext` shape (read `registry.ts` `runTool` and how `fileTools.test.ts` builds its context; copy that). Adapt `firstText`'s use to how it reads a result.

- [ ] **Step 2: Run it to see it fail**

Run: `cd agent && pnpm test -- meshModelTools`
Expected: FAIL, `no tool create_model_from_mesh`.

- [ ] **Step 3: Write the tools**

```ts
// agent/src/tools/meshModels.ts
import { z } from 'zod'
import { command } from './command.js'
import { ok } from './call.js'
import { decodeBase64, fileForm, slug } from './common.js'
import { defineTool, json, type Tool } from './registry.js'

// Mesh models (#2146, spec docs/superpowers/specs/2026-10-11-mesh-models-design.md):
// a 3MF/STL opened as a model whose scene.json compiles to its model.scad. Routes:
// backend/scadbuddy/api/mesh_models.py. Nothing here writes to Bambuddy.

const vec3 = z.tuple([z.number(), z.number(), z.number()])
const num = z.union([z.number(), z.object({ expose: z.string(), value: z.number() })])
const nvec3 = z.tuple([num, num, num])
const object = z.string().regex(/^o[0-9]+$/)

const edit = z.discriminatedUnion('edit', [
  z.object({ edit: z.literal('transform'), object, translate: nvec3.optional(), rotate: nvec3.optional(), scale: nvec3.optional() }),
  z.object({
    edit: z.literal('split_at_plane'),
    object,
    point: vec3.describe('A point on the cutting plane, world mm'),
    normal: vec3.describe('The plane normal, world; the new object is the side it points to'),
    joint: z
      .object({
        diameter: z.number().positive().optional().describe('Peg diameter; default 60% of the room at the section centre'),
        depth: z.number().positive().default(6),
        clearance: z.number().min(0).max(2).default(0.2),
        key: z.enum(['none', 'flat', 'hex']).default('flat'),
        peg_on: z.enum(['above', 'below']).default('above'),
      })
      .optional(),
  }),
  z.object({ edit: z.literal('lay_flat'), object, normal: z.union([vec3, z.literal('largest_flat')]) }),
  z.object({ edit: z.literal('add'), object, primitive: z.enum(['cube', 'cylinder', 'sphere']), size: nvec3, at: vec3.optional(), rotate: nvec3.optional() }),
  z.object({ edit: z.literal('subtract'), object, primitive: z.enum(['cube', 'cylinder', 'sphere']), size: nvec3, at: vec3.optional(), rotate: nvec3.optional() }),
  z.object({ edit: z.literal('custom'), object, scad: z.string().min(1).max(20_000) }),
  z.object({ edit: z.literal('set_colour'), object, part: z.string().regex(/^p[0-9]+$/), colour: z.string().regex(/^#[0-9A-Fa-f]{6}$/) }),
  z.object({ edit: z.literal('delete'), object }),
  z.object({ edit: z.literal('duplicate'), object, offset: vec3.optional() }),
  z.object({ edit: z.literal('rename'), object, name: z.string().min(1).max(200) }),
  z.object({ edit: z.literal('remove_op'), object, op: z.number().int().min(0) }),
  z.object({ edit: z.literal('expose'), object, op: z.number().int().min(0).nullable(), field: z.string(), name: z.string() }),
  z.object({ edit: z.literal('unexpose'), object, op: z.number().int().min(0).nullable(), field: z.string() }),
])

export const meshModelTools: Tool[] = [
  defineTool({
    name: 'create_model_from_mesh',
    description:
      'Open a mesh as an editable model: a Bambuddy library file (library_file_id, from list_library), an output ' +
      '(output_id), or a 3MF/STL you have (filename + content_base64). Each object becomes a scene object you can ' +
      'cut, join with a peg and socket, lay flat, add to or subtract from with edit_scene, then render and print ' +
      'like any template. Nothing is written to Bambuddy.',
    input: z
      .object({
        name: z.string().min(1).max(200),
        description: z.string().max(10_000).optional(),
        library_file_id: z.number().int().positive().optional(),
        output_id: z.string().optional(),
        filename: z.string().regex(/\.(3mf|stl)$/i).optional(),
        content_base64: z.string().min(1).optional(),
      })
      .refine(
        (a) => [a.library_file_id !== undefined, a.output_id !== undefined, a.content_base64 !== undefined].filter(Boolean).length === 1,
        'give exactly one of library_file_id, output_id, content_base64',
      ),
    risk: 'write',
    routes: ['POST /api/v1/models/from-mesh', 'POST /api/v1/models/from-mesh/upload'],
    handler: async ({ name, description, library_file_id, output_id, filename, content_base64 }, ctx) => {
      if (content_base64 !== undefined) {
        const form = fileForm(decodeBase64(content_base64, 'content_base64'), filename ?? 'mesh.3mf', 'application/octet-stream')
        form.append('name', name)
        return json(
          await command(ctx, `open ${filename}`, (headers) =>
            ctx.backend.POST('/api/v1/models/from-mesh/upload', { body: {} as never, bodySerializer: () => form, headers }),
          ),
        )
      }
      const body = library_file_id !== undefined
        ? { name, description: description ?? '', library_file_id }
        : { name, description: description ?? '', output_id: output_id! }
      return json(await command(ctx, `open ${name}`, (headers) => ctx.backend.POST('/api/v1/models/from-mesh', { body, headers })))
    },
  }),

  defineTool({
    name: 'get_scene',
    description:
      "A mesh model's scene: its objects (ids o1, o2…), each object's colour parts, its ops in order (cut, joint, " +
      'add, subtract, custom) in the object\'s own frame, and its transform (translate, rotate in degrees X then Y ' +
      'then Z, scale). hand_edited true means model.scad was edited directly; edit_scene is refused until eject_scene.',
    input: z.object({ slug }),
    risk: 'read',
    source: 'scene data derived from a mesh file the user supplied or Bambuddy holds',
    routes: ['GET /api/v1/models/{slug}/scene'],
    handler: async ({ slug }, { backend }) =>
      json(await ok(backend.GET('/api/v1/models/{slug}/scene', { params: { path: { slug } } }), `get scene ${slug}`)),
  }),

  defineTool({
    name: 'edit_scene',
    description:
      'Edit a mesh model as one revision (undo with restore_version); edits apply in order, so a later edit may name an ' +
      'object an earlier one made (split_at_plane makes the top half the next id). Planes and `at` are world mm. ' +
      'split_at_plane with joint adds a keyed peg to one half and a socket with clearance to the other, sized from ' +
      'the section and refused when it would not fit. lay_flat turns a world direction (or the largest flat face, often ' +
      'the cut face) onto the bed. expose turns a number into a customizer parameter; removing one a saved preset sets ' +
      'needs drop_from_presets. Render after editing to check.',
    input: z.object({ slug, edits: z.array(edit).min(1).max(50), drop_from_presets: z.boolean().default(false) }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/scene/edits'],
    handler: async ({ slug, edits, drop_from_presets }, ctx) =>
      json(
        await command(ctx, `edit scene ${slug}`, (headers) =>
          ctx.backend.POST('/api/v1/models/{slug}/scene/edits', { params: { path: { slug } }, body: { edits, drop_from_presets }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'eject_scene',
    description:
      "Stop editing a mesh model as a scene: scene.json is removed and model.scad (which imports the meshes) is the " +
      'source from then on, edited with update_source. One way; restore_version undoes it.',
    input: z.object({ slug }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/scene/eject'],
    handler: async ({ slug }, ctx) =>
      json(
        await command(ctx, `eject scene ${slug}`, (headers) =>
          ctx.backend.POST('/api/v1/models/{slug}/scene/eject', { params: { path: { slug } }, headers }),
        ),
      ),
  }),
]
```

Match `defineTool`'s required fields to `registry.ts` (`title`, `source` for read tools, how `routes` must name every route the handler calls; `test/coverage.test.ts` checks routes) and the multipart call to `upload_asset`'s in `customizer.ts`. Register in `index.ts`: `import { meshModelTools } from './meshModels.js'` and `...meshModelTools,` after `...catalogueTools,`.

- [ ] **Step 4: Run the agent checks**

Run: `cd agent && pnpm lint && pnpm typecheck && pnpm test && pnpm build`
Expected: PASS, including `test/coverage.test.ts` (every new `/api/v1` operation has a tool).

- [ ] **Step 5: Commit**

```bash
git add agent/src/tools/meshModels.ts agent/src/tools/index.ts agent/test/meshModelTools.test.ts
git commit -m "feat(agent): mesh model tools — create from a mesh, read, edit and eject the scene (#2146)"
```

---

### Task 11: The bundled demo template

**Files:**
- Create: `models/mesh-joint-demo/model.json`, `models/mesh-joint-demo/README.md`, `models/mesh-joint-demo/scene.json`, `models/mesh-joint-demo/model.scad`, `models/mesh-joint-demo/meshes/o1-p1.3mf`, `models/mesh-joint-demo/meshes/o1-p2.3mf`, `models/mesh-joint-demo/make.py`, `models/mesh-joint-demo/verify.sh`

**Interfaces:**
- Consumes: Tasks 1–7.

`make.py` builds the demo from scratch and is how its files are made and re-made: a two-colour "carrot" (orange body cylinder r=10 h=30, green neck-and-tuft: neck r=3 h=5 plus tuft box 8×8×10) made with trimesh, read into a scene with `scene_from_objects`, split at z=32 with a flat-keyed joint (depth 4, clearance 0.2), the top half laid flat with `largest_flat`, and `cut_height` exposed on the split's lower cut. It writes `scene.json`, `model.scad` and `meshes/` beside itself. Run it with the backend's environment: `cd backend && uv run --frozen python ../models/mesh-joint-demo/make.py`.

- [ ] **Step 1: Write `make.py`**

```python
# models/mesh-joint-demo/make.py
"""Build this template from scratch (#2146): a two-colour carrot made with trimesh, read
into a scene, split at the neck with a keyed peg and socket, the top laid flat on its
cut face, the cut height exposed. Run from backend/:
`uv run --frozen python ../models/mesh-joint-demo/make.py`."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import trimesh
from pydantic import TypeAdapter

from scadbuddy.meshmodel.edits import Edit, apply_edits
from scadbuddy.meshmodel.ingest import scene_from_objects
from scadbuddy.meshmodel.operations import compiled
from scadbuddy.meshmodel.scene import SCENE_NAME, SceneSource, dump_scene
from scadbuddy.render.objects3mf import ReadObject
from scadbuddy.render.split import ColourPart

HERE = Path(__file__).resolve().parent


def revolved(profile: list[tuple[float, float]]) -> trimesh.Trimesh:
    return trimesh.creation.revolve(np.array(profile, dtype=float), sections=64)


def main() -> None:
    body = revolved([(0, 0), (10, 0), (10, 30), (3, 30), (0, 30)])
    leaves = revolved([(0, 30), (3, 30), (3, 35), (6, 35), (6, 45), (0, 45)])
    read = [ReadObject(name="Carrot", count=1, parts=[
        ColourPart(0, "Body", "#F08A24", body), ColourPart(1, "Leaves", "#3A7D2C", leaves),
    ])]
    source = SceneSource(kind="upload", name="carrot (made by make.py)", sha256="0" * 64)
    scene, files = scene_from_objects(read, source)
    for name, data in files.items():
        (HERE / name).parent.mkdir(parents=True, exist_ok=True)
        (HERE / name).write_bytes(data)

    def mesh_of(path: str) -> trimesh.Trimesh:
        return trimesh.load(io.BytesIO((HERE / path).read_bytes()), file_type="3mf", force="mesh")

    edits = TypeAdapter(list[Edit])
    scene = apply_edits(scene, edits.validate_python([
        {"edit": "split_at_plane", "object": "o1", "point": [0, 0, 32], "normal": [0, 0, 1],
         "joint": {"depth": 4, "clearance": 0.2, "key": "flat"}},
        {"edit": "lay_flat", "object": "o2", "normal": "largest_flat"},
    ]), mesh_of)
    # Beside the body, keeping the height lay_flat dropped it to.
    dropped = scene.objects[1].transform.translate[2]
    scene = apply_edits(scene, edits.validate_python([
        {"edit": "transform", "object": "o2", "translate": [25, 0, dropped]},
        {"edit": "expose", "object": "o1", "op": 0, "field": "point.2", "name": "cut_height"},
    ]), mesh_of)
    scene, text = compiled(scene)
    (HERE / SCENE_NAME).write_text(dump_scene(scene))
    (HERE / "model.scad").write_text(text)


if __name__ == "__main__":
    main()
```

Exposing `cut_height` on `o1`'s cut alone makes the bottom's cut a parameter while the top's stays at 32: that is the demo's point (the customizer moves where the bottom ends); say so in the README.

- [ ] **Step 2: Run it and commit its output**, plus `model.json` (`{"name": "Mesh joint demo", "description": "A mesh opened as a scene: cut at the neck, joined with a keyed peg and socket, the top laid flat (#2146).", "tags": ["mesh", "demo"]}`) and a README saying what the template shows and that `make.py` regenerates it.

- [ ] **Step 3: Write `verify.sh`**

Follow `models/battery-crate/verify.sh`'s frame (`set -euo pipefail`, `cd "$(dirname "$0")"`, `BASE_IMAGE`, every `docker run` with `--label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}"` on the same line). It:
1. renders `model.scad` with the defaults and with `-D cut_height=31` in `$BASE_IMAGE` (`openscad --backend=Manifold -o out.3mf model.scad`), failing on any `WARNING:` or `ERROR:` in stderr;
2. checks on the host (Python, as battery-crate's checker does, reading the 3MF's XML) that the output has the two colours `#F08A24` and `#3A7D2C`;
3. when `SCADBUDDY_PIPELINE_IMAGE` is set, runs `python -m scadbuddy.meshmodel.check models/mesh-joint-demo` in it to assert `sha256(model.scad) == scene.generated_sha256` and `compile_scene(scene) == model.scad`; otherwise prints `pipeline check: skipped`.

For step 3 add `backend/scadbuddy/meshmodel/check.py`:

```python
# backend/scadbuddy/meshmodel/check.py
"""`python -m scadbuddy.meshmodel.check <model dir>`: a bundled mesh model's model.scad
is what its scene compiles to, and the scene's recorded hash is that text's (#2146).
Exit 1 naming the first differing line."""

from __future__ import annotations

import sys
from pathlib import Path

from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.scene import SCENE_NAME, load_scene, text_sha256


def problems(directory: Path) -> list[str]:
    scene = load_scene((directory / SCENE_NAME).read_text(encoding="utf-8"))
    text = (directory / "model.scad").read_text(encoding="utf-8")
    found = []
    if text_sha256(text) != scene.generated_sha256:
        found.append("scene.json's generated_sha256 is not model.scad's")
    expected = compile_scene(scene)
    if text != expected:
        for number, (have, want) in enumerate(zip(text.splitlines(), expected.splitlines(), strict=False), 1):
            if have != want:
                found.append(f"model.scad line {number}: {have!r}, the scene compiles {want!r}")
                break
        else:
            found.append("model.scad and the compiled scene differ in length")
    return found


def main(argv: list[str]) -> int:
    found = problems(Path(argv[1]))
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

and `backend/tests/meshmodel/test_check.py`:

```python
from pathlib import Path

from scadbuddy.meshmodel.check import problems
from scadbuddy.meshmodel.operations import compiled
from scadbuddy.meshmodel.scene import Part, Scene, SceneObject, SceneSource, dump_scene


def write(directory: Path) -> None:
    scene, text = compiled(Scene(
        source=SceneSource(kind="upload", name="x.3mf", sha256="ab" * 32),
        objects=[SceneObject(id="o1", name="X", parts=[Part(id="p1", mesh="meshes/o1-p1.3mf", colour="#FFFFFF")])],
    ))
    (directory / "scene.json").write_text(dump_scene(scene))
    (directory / "model.scad").write_text(text)


def test_a_freshly_compiled_model_has_no_problems(tmp_path: Path) -> None:
    write(tmp_path)
    assert problems(tmp_path) == []


def test_an_edited_model_scad_is_named(tmp_path: Path) -> None:
    write(tmp_path)
    (tmp_path / "model.scad").write_text((tmp_path / "model.scad").read_text().replace("o1();", "o1(); cube(1);"))
    assert len(problems(tmp_path)) == 2
```

- [ ] **Step 4: Run it**

```bash
docker build --target base -t scadbuddy-verify:ci .
docker build --target test -t scadbuddy:test .
SCADBUDDY_OPENSCAD_IMAGE=scadbuddy-verify:ci SCADBUDDY_PIPELINE_IMAGE=scadbuddy:test bash models/mesh-joint-demo/verify.sh
shellcheck models/mesh-joint-demo/verify.sh && .github/scripts/lint-verify-labels.sh
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add models/mesh-joint-demo backend/scadbuddy/meshmodel/check.py backend/tests/meshmodel/test_check.py
git commit -m "feat(models): mesh-joint-demo, a mesh scene cut, jointed and laid flat (#2146)"
```

---

### Task 12: The authoring skill

**Files:**
- Modify: `plugins/scadbuddy/skills/authoring/SKILL.md`

- [ ] **Step 1: Add a "Mesh models" section** saying, with citations to `docs/superpowers/specs/2026-10-11-mesh-models-design.md` (§3, §5) and `agent/src/tools/meshModels.ts`:
  - open a mesh with `create_model_from_mesh` (library file, output, or a file);
  - read it with `get_scene` (object ids, parts, ops, transforms);
  - edit with `edit_scene`, edits in order; world planes; `split_at_plane` with `joint` for a weak neck (peg on the part printed separately, `lay_flat` that part with `largest_flat`);
  - render, inspect with `analyze_geometry`, and iterate; undo with `restore_version`;
  - `hand_edited` means eject first; `eject_scene` is one way.

- [ ] **Step 2: Run the plugin checks** (the `PostToolUse` hook bumps the plugin version and runs them on the first edit; run them again by hand):

```bash
.github/scripts/lint-plugin.sh && claude plugin validate plugins/scadbuddy
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add plugins/scadbuddy agent/plugins/scadbuddy
git commit -m "docs(plugin): authoring mesh models (#2146)"
```

---

## Self-review

- **Spec coverage (phases 1–2):** §3 representation → Tasks 1, 3; §3.3 colourless meshes → Task 5 (`test_a_part_file_is_a_colourless_closed_3mf`), Task 9; §4 sources (library, upload, output), cap, provenance, nothing to Bambuddy → Tasks 5, 6, 8; OBJ refused → Task 8 (`test_an_obj_upload_is_refused`); §5 edits, hash check, presets → Tasks 7, 8; §6 kinds on `library`, tools, flows (`tool(name, args)` needs nothing new) → Tasks 8, 10; §10 tests for phases 1–2 → Tasks 1–11; §11 phases 1–2 acceptance → Tasks 8, 9, 11. Deferred, stated in the header: §7, §8, §9, the eval scenario.
- **Deviation from the spec, stated:** §4 says the library read reuses `library_objects.py`. Its pieces are `piece_mesh` 3MFs with `basematerials`, which §3.3 forbids in a part file, so `mesh_model_create` reuses only its fetch (`LibrarySource.fetch_3mf`), its reader (`read_objects`) and its refusals. Model-level `scene: true` is not written into `model.json`: a model is a mesh model when it has `scene.json`; `model.json` keeps `mesh_source` so the origin survives an eject. `drop_from_presets` drops from saved presets; the model's own `model.json` presets are refused with "edit them first".
- **Type consistency:** `compiled(scene) -> tuple[Scene, str]` (Task 8) is used by Task 11's `make.py`; `mesh_of(path) -> trimesh.Trimesh` everywhere; `Exposed(expose, value)` in Tasks 1, 3, 7, 10 (zod `num`).
