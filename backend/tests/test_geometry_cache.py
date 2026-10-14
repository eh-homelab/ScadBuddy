"""Geometry first, colours second (`render/geometry_cache.py`): a mesh model's key, the
recolour, and the activities that render a piece once and recolour the next."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import trimesh
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.scene import (
    SCENE_NAME,
    Add,
    Exposed,
    Joint,
    Part,
    Scene,
    SceneObject,
    SceneSource,
    Transform,
    dump_scene,
    text_sha256,
)
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME
from scadbuddy.render.geometry_cache import (
    geometry_key,
    mesh_scene,
    recolour_layout,
    reusable,
    scene_classes,
)
from scadbuddy.render.glb import COLOUR_EXTRA
from scadbuddy.render.jobs import LAYOUT_NAME
from scadbuddy.render.schema import ParamValue
from scadbuddy.store import BlobRefs
from scadbuddy.store.geometry import GeometryIndex
from scadbuddy.workflows.activities import PIECE_NAME, RenderActivities
from scadbuddy.workflows.models import PieceRequest, piece_key
from tests.conftest import PgPool
from tests.support.activities import demo_paths, worker_deps

MESHES = ("meshes/o1-p1.3mf", "meshes/o1-p2.3mf")


def _scene(body: str = "#FF0000", trim: str = "#00FF00", *, width: float = 10) -> Scene:
    """A mesh model of two parts, ``width`` exposed, as `compile_scene` last wrote it."""
    scene = Scene(
        source=SceneSource(kind="upload", name="duck.3mf", sha256="0" * 64),
        objects=[
            SceneObject(
                id="o1",
                name="duck",
                parts=[
                    Part(id="p1", mesh=MESHES[0], colour=body),
                    Part(id="p2", mesh=MESHES[1], colour=trim),
                ],
                transform=Transform(translate=(Exposed(expose="width", value=width), 0, 0)),
            )
        ],
    )
    return scene.model_copy(update={"generated_sha256": text_sha256(compile_scene(scene))})


def _write_model(root: Path, scene: Scene) -> None:
    (root / "meshes").mkdir(parents=True, exist_ok=True)
    for index, mesh in enumerate(MESHES):
        if not (root / mesh).exists():
            (root / mesh).write_bytes(f"mesh {index}".encode())
    (root / SCENE_NAME).write_text(dump_scene(scene), encoding="utf-8")
    (root / "model.scad").write_text(compile_scene(scene), encoding="utf-8")


def _key(root: Path, scene: Scene, **params: ParamValue) -> str:
    return geometry_key("demo", "model.scad", root, scene, [], params)


# ── the key ────────────────────────────────────────────────────────────────────


def test_scenes_that_differ_only_in_colour_share_a_geometry_key(tmp_path: Path) -> None:
    _write_model(tmp_path, _scene())
    assert _key(tmp_path, _scene()) == _key(tmp_path, _scene("#0000FF", "#ABCDEF"))
    assert scene_classes(_scene("#0000FF", "#ABCDEF")) == ["#0000FF", "#ABCDEF"]


def test_parts_that_come_to_share_a_colour_are_another_geometry_key(tmp_path: Path) -> None:
    """They become one part, one solid and one extruder: different geometry."""
    _write_model(tmp_path, _scene())
    assert _key(tmp_path, _scene("#00FF00")) != _key(tmp_path, _scene("#0000FF"))
    assert scene_classes(_scene("#00FF00")) == ["#00FF00"]


def test_an_add_or_peg_colour_is_a_class_too(tmp_path: Path) -> None:
    scene = _scene()
    obj = scene.objects[0]
    added = obj.model_copy(
        update={"ops": [Add(primitive="cube", size=(1, 1, 1), colour="#0000FF")]}
    )
    assert scene_classes(scene.model_copy(update={"objects": [added]})) == [
        "#FF0000",
        "#00FF00",
        "#0000FF",
    ]


def _with(scene: Scene, *parts: Part, ops: list[Any]) -> Scene:
    obj = scene.objects[0]
    return scene.model_copy(
        update={"objects": [obj.model_copy(update={"parts": [*obj.parts, *parts], "ops": ops})]}
    )


def test_a_colour_that_prints_nothing_is_no_class_and_no_part_of_the_key(
    tmp_path: Path,
) -> None:
    """A negative part's and a socket's colour fill no volume: a key, whatever they are."""
    _write_model(tmp_path, _scene())
    (tmp_path / "meshes" / "o1-p3.3mf").write_bytes(b"cutter")

    def scene(cutter: str, socket: str) -> Scene:
        return _with(
            _scene(),
            Part(id="p3", mesh="meshes/o1-p3.3mf", colour=cutter, negative=True),
            ops=[
                Joint(
                    at=(0, 0, 0),
                    axis=(0, 0, 1),
                    diameter=4,
                    depth=3,
                    clearance=0.2,
                    role="socket",
                    colour=socket,
                )
            ],
        )

    first = scene("#123456", "#654321")
    assert scene_classes(first) == ["#FF0000", "#00FF00"]
    assert _key(tmp_path, first) == _key(tmp_path, scene("#ABCDEF", "#FEDCBA"))


def test_an_op_without_a_colour_is_in_its_objects_default_class(tmp_path: Path) -> None:
    """An uncoloured add prints in the object's first printed part's colour, so it is
    the same geometry as one that names that colour."""
    _write_model(tmp_path, _scene())
    plain = _with(_scene(), ops=[Add(primitive="cube", size=(1, 1, 1))])
    named = _with(_scene(), ops=[Add(primitive="cube", size=(1, 1, 1), colour="#FF0000")])
    assert scene_classes(plain) == scene_classes(named) == ["#FF0000", "#00FF00"]
    assert _key(tmp_path, plain) == _key(tmp_path, named)


def test_a_geometry_change_is_another_geometry_key(tmp_path: Path) -> None:
    _write_model(tmp_path, _scene())
    base = _key(tmp_path, _scene())
    assert base != _key(tmp_path, _scene(width=11))
    assert base != _key(tmp_path, _scene(), width=12)
    assert base != geometry_key("other", "model.scad", tmp_path, _scene(), [], {})
    library = ModelLibrary(
        name="BOSL2", url="https://github.com/x/BOSL2", ref="v1", commit="a" * 40
    )
    assert base != geometry_key("demo", "model.scad", tmp_path, _scene(), [library], {})
    (tmp_path / MESHES[0]).write_bytes(b"another mesh")
    assert base != _key(tmp_path, _scene())


def test_only_a_mesh_models_compiled_scene_has_a_scene(tmp_path: Path) -> None:
    (tmp_path / "model.scad").write_text('color("#FF0000") cube();\n', encoding="utf-8")
    assert mesh_scene(tmp_path, "model.scad") is None  # a .scad template
    _write_model(tmp_path, _scene())
    assert mesh_scene(tmp_path, "model.scad") == _scene()
    assert mesh_scene(tmp_path, "parts/a.scad") is None
    # Edited by hand: no longer what the scene compiled.
    with (tmp_path / "model.scad").open("a", encoding="utf-8") as source:
        source.write("cube();\n")
    assert mesh_scene(tmp_path, "model.scad") is None
    # A scene that no longer loads: rendered from model.scad, its geometry unshared.
    _write_model(tmp_path, _scene())
    (tmp_path / SCENE_NAME).write_text("{}", encoding="utf-8")
    assert mesh_scene(tmp_path, "model.scad") is None
    # Ejected: the scene is gone.
    _write_model(tmp_path, _scene())
    (tmp_path / SCENE_NAME).unlink()
    assert mesh_scene(tmp_path, "model.scad") is None


# ── the recolour ───────────────────────────────────────────────────────────────


def _layout(*colours: str, uncoloured: bool = False) -> dict[str, Any]:
    parts = [
        {"material_index": i, "name": f"Color {i}", "colour": c, "file": f"solid_{i}.3mf"}
        for i, c in enumerate(colours, start=1)
    ]
    if uncoloured:
        parts.append({"material_index": 0, "name": "Default", "colour": "#F9D72C", "file": "x"})
    return {
        "plates": [{"extruders": list(range(1, len(parts) + 1)), "parts": parts}],
        "colours": list(colours),
        "warnings": [f"{colours[0]}: no closed solid; used the split mesh"],
        "bbox": {},
    }


def test_a_layout_is_recoloured_in_one_pass() -> None:
    """Two classes trading colours do not both end up as one of them."""
    swapped = recolour_layout(
        _layout("#FF0000", "#00FF00"), {"#FF0000": "#00FF00", "#00FF00": "#FF0000"}
    )
    assert swapped["colours"] == ["#00FF00", "#FF0000"]
    assert [p["colour"] for p in swapped["plates"][0]["parts"]] == ["#00FF00", "#FF0000"]
    assert swapped["warnings"] == ["#00FF00: no closed solid; used the split mesh"]


def test_only_a_layout_every_colour_of_which_is_a_class_is_reusable() -> None:
    assert reusable(_layout("#FF0000", "#00FF00"), ["#FF0000", "#00FF00"])
    assert reusable(_layout("#ff0000"), ["#FF0000", "#00FF00"])
    # A literal, a library's colour or one computed from a parameter.
    assert not reusable(_layout("#FF0000", "#123456"), ["#FF0000", "#00FF00"])
    # Uncoloured geometry is drawn in no class's colour, and keeps its own.
    assert reusable(_layout("#FF0000", uncoloured=True), ["#FF0000"])


# ── the activities ─────────────────────────────────────────────────────────────

#: An openscad that exports the mesh model's one exposed number, and as its model one
#: box per distinct colour its ``model.scad`` names (only the `_sb_targets` colour in a
#: solid's wrapper render, which sits beside it), logging every 3MF export so a test
#: can count the renders.
FAKE = r"""#!/usr/bin/env python3
import json, pathlib, re, sys, zipfile
args = sys.argv[1:]
here = pathlib.Path(sys.argv[0]).parent
if "--version" in args:
    print("OpenSCAD version 2099.01.01", file=sys.stderr); raise SystemExit(0)
out = args[args.index("-o") + 1]
defines = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-D")
if out.endswith(".param"):
    pathlib.Path(out).write_text(json.dumps({"parameters": [
        {"name": "width", "type": "number", "initial": 10}]}))
    raise SystemExit(0)
with open(here / "exports.log", "a") as log:
    log.write(" ".join(args) + "\n")
width = float(defines.get("width", "10"))
source = next(pathlib.Path(a) for a in args if a.endswith(".scad"))
colours = []
for c in re.findall(r'color\("(#[0-9A-F]{6})"\)', (source.parent / "model.scad").read_text()):
    if c not in colours:
        colours.append(c)
targets = defines.get("_sb_targets")
if targets is not None:
    wanted = {t.upper() for t in json.loads(targets)}
    colours = [c for c in colours if c in wanted]
bases = ['<base name="Default" displaycolor="#F9D72C00" />']
vertices, triangles = [], []
for index, colour in enumerate(colours, start=1):
    bases.append(f'<base name="Color {index}" displaycolor="{colour}00" />')
    x = (index - 1) * (width + 5)
    corners = [(x + dx, dy, dz) for dx in (0, width) for dy in (0, 10) for dz in (0, 2)]
    offset = len(vertices)
    vertices += corners
    for a, b, c in ((0,1,3),(0,3,2),(4,6,7),(4,7,5),(0,4,5),(0,5,1),(2,3,7),(2,7,6),
                    (0,2,6),(0,6,4),(1,5,7),(1,7,3)):
        v = (a + offset, b + offset, c + offset)
        triangles.append(
            f'<triangle v1="{v[0]}" v2="{v[1]}" v3="{v[2]}" pid="1" p1="{index}" />'
        )
ns = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
model = ('<?xml version="1.0" encoding="utf-8"?>\n'
    f'<model xmlns="{ns}" unit="millimeter">'
    '<resources><basematerials id="1">' + "".join(bases) + '</basematerials>'
    '<object id="2" type="model" pid="1" pindex="0"><mesh><vertices>'
    + "".join(f'<vertex x="{v[0]}" y="{v[1]}" z="{v[2]}" />' for v in vertices)
    + '</vertices><triangles>' + "".join(triangles) + '</triangles></mesh></object>'
    '</resources><build><item objectid="2" /></build></model>')
with zipfile.ZipFile(out, "w") as archive:
    archive.writestr("3D/3dmodel.model", model)
"""


def _fake_openscad(directory: Path) -> str:
    directory.mkdir(parents=True)
    binary = directory / "fake-openscad"
    binary.write_text(FAKE, encoding="utf-8")
    binary.chmod(0o755)
    return str(binary)


def _exports(binary: str) -> int:
    log = Path(binary).parent / "exports.log"
    return len(log.read_text().splitlines()) if log.is_file() else 0


def _piece(
    acts: RenderActivities, edit: int, scene: Scene | None = None, **params: ParamValue
) -> PieceRequest:
    """A piece of the model after its ``edit``-th scene edit, a revision of its own
    (an export, as `source_directory` reads one): ``scene`` compiled, or with None a
    plain ``.scad`` template."""
    revision = f"{edit:07x}"
    root = acts.deps.paths.model_revision_dir("demo", revision)
    root.mkdir(parents=True, exist_ok=True)
    if scene is None:
        (root / "model.scad").write_text('color("#FF0000") cube();\n', encoding="utf-8")
    else:
        _write_model(root, scene)
    return PieceRequest(
        slug="demo",
        revision=revision,
        params=dict(params),
        piece_key=piece_key("demo", revision, "model.scad", params),
    )


def _filaments(model_3mf: Path) -> list[str]:
    with zipfile.ZipFile(model_3mf) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    return [colour.upper()[:7] for colour in settings["filament_colour"]]


@pytest.fixture
def geometry_acts(tmp_path: Path, pg_pool: PgPool) -> tuple[RenderActivities, str, GeometryIndex]:
    paths = demo_paths(tmp_path)
    binary = _fake_openscad(tmp_path / "geometry-bin")
    index = GeometryIndex(pg_pool)
    deps = worker_deps(tmp_path, paths, refs=BlobRefs(pg_pool))
    deps.config = Config(openscad=binary, data_dir=paths.root)
    deps.geometry = index
    return RenderActivities(deps), binary, index


async def _render(acts: RenderActivities, req: PieceRequest) -> Any:
    env = ActivityEnvironment()
    prepared = await env.run(acts.prepare, req)
    main = await env.run(acts.render_main, req, prepared)
    await env.run(acts.render_solids, req, prepared, main)
    return prepared, main, await env.run(acts.finish_piece, req, prepared, main)


@pytest.mark.requires_postgres
async def test_a_scene_in_other_colours_is_recoloured_without_rendering(
    geometry_acts: tuple[RenderActivities, str, GeometryIndex],
) -> None:
    acts, binary, index = geometry_acts
    env = ActivityEnvironment()
    first = _piece(acts, 1, _scene(), width=12)
    prepared, main, _ = await _render(acts, first)
    assert prepared.geometry_key is not None
    assert prepared.colours == ["#FF0000", "#00FF00"]
    row = index.get(prepared.geometry_key)
    assert row is not None and row.piece_key == first.piece_key
    rendered = _exports(binary)
    assert rendered == 3  # the model, then one solid per colour

    second = _piece(acts, 2, _scene("#0000FF"), width=12)
    assert await env.run(acts.cached_piece, second) is None
    again = await env.run(acts.prepare, second)
    assert again.geometry_key == prepared.geometry_key
    reused = await env.run(acts.reuse_geometry, second, again)
    assert reused is not None and reused == main
    unrecorded = again.model_copy(update={"geometry_key": None})  # as `RenderPiece` does
    piece = await env.run(acts.finish_piece, second, unrecorded, reused)
    assert _exports(binary) == rendered  # no openscad run

    work = acts.deps.blobs.dir_for(second.piece_key)
    assert (work / PIECE_NAME).is_file()
    assert piece.result.colors == ["#0000FF", "#00FF00"]
    assert [(p.colour, p.extruder) for p in piece.result.parts] == [
        ("#0000FF", 1),
        ("#00FF00", 2),
    ]
    assert _filaments(acts.deps.paths.root / piece.result.model_3mf) == ["#0000FF", "#00FF00"]
    layout = json.loads((work / LAYOUT_NAME).read_text())
    assert [p["colour"] for p in layout["plates"][0]["parts"]] == ["#0000FF", "#00FF00"]
    scene = trimesh.load(acts.deps.paths.root / piece.result.preview_glb)
    assert isinstance(scene, trimesh.Scene)
    assert sorted(g.metadata[COLOUR_EXTRA] for g in scene.geometry.values()) == [
        "#0000FF",
        "#00FF00",
    ]
    # The source piece is untouched, and its own answer still stands.
    source = await env.run(acts.cached_piece, first)
    assert source is not None and source.result.colors == ["#FF0000", "#00FF00"]
    # The row still names the rendered piece, whose raw render holds its colours.
    row = index.get(prepared.geometry_key)
    assert row is not None and row.piece_key == first.piece_key

    # A third colour edit is recoloured from the rendered piece, not the second.
    third = _piece(acts, 3, _scene("#FFFF00"), width=12)
    prepared3 = await env.run(acts.prepare, third)
    reused3 = await env.run(acts.reuse_geometry, third, prepared3)
    assert reused3 is not None
    unrecorded3 = prepared3.model_copy(update={"geometry_key": None})
    piece3 = await env.run(acts.finish_piece, third, unrecorded3, reused3)
    assert _exports(binary) == rendered
    scene3 = trimesh.load(acts.deps.paths.root / piece3.result.preview_glb)
    assert isinstance(scene3, trimesh.Scene)
    assert [g.metadata[COLOUR_EXTRA] for g in scene3.geometry.values()] == [
        "#FFFF00",
        "#00FF00",
    ]
    assert [(p.colour, p.extruder) for p in piece3.result.parts] == [
        ("#FFFF00", 1),
        ("#00FF00", 2),
    ]


@pytest.mark.requires_postgres
async def test_a_scene_with_other_geometry_is_not_recoloured(
    geometry_acts: tuple[RenderActivities, str, GeometryIndex],
) -> None:
    acts, _, _ = geometry_acts
    env = ActivityEnvironment()
    await _render(acts, _piece(acts, 1, _scene(), width=12))
    for edit, scene, width in (
        (2, _scene(), 13),
        # The body in the trim's colour: one part where the first had two.
        (3, _scene("#00FF00"), 12),
    ):
        req = _piece(acts, edit, scene, width=width)
        prepared = await env.run(acts.prepare, req)
        assert await env.run(acts.reuse_geometry, req, prepared) is None


@pytest.mark.requires_postgres
async def test_a_swept_source_piece_is_a_miss_and_forgotten(
    geometry_acts: tuple[RenderActivities, str, GeometryIndex],
) -> None:
    acts, _, index = geometry_acts
    env = ActivityEnvironment()
    first = _piece(acts, 1, _scene(), width=12)
    prepared, _, _ = await _render(acts, first)
    acts.deps.blobs.remove(first.piece_key)

    second = _piece(acts, 2, _scene("#0000FF"), width=12)
    again = await env.run(acts.prepare, second)
    assert await env.run(acts.reuse_geometry, second, again) is None
    assert prepared.geometry_key is not None and index.get(prepared.geometry_key) is None


@pytest.mark.requires_postgres
async def test_a_scad_template_never_shares_its_geometry(
    geometry_acts: tuple[RenderActivities, str, GeometryIndex],
) -> None:
    acts, _, _ = geometry_acts
    prepared = await ActivityEnvironment().run(acts.prepare, _piece(acts, 1, width=12))
    assert prepared.geometry_key is None


@pytest.mark.requires_postgres
def test_a_swept_pieces_rows_are_forgotten(pg_pool: PgPool) -> None:
    geometry = GeometryIndex(pg_pool)
    geometry.put("g1", "piece-a", ["#FF0000"], {})
    geometry.put("g2", "piece-b", ["#FF0000"], {})
    geometry.forget_pieces(["piece-a", "piece-c"])
    assert geometry.get("g1") is None
    assert geometry.get("g2") is not None
