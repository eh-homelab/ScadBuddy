"""Geometry first, colours second (`render/geometry_cache.py`): the key, the recolour,
and the activities that render a piece once and recolour the next."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import trimesh
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME
from scadbuddy.render.geometry_cache import (
    class_order,
    colour_classes,
    geometry_key,
    has_literal_colour,
    recolour_layout,
    reusable,
)
from scadbuddy.render.glb import COLOUR_EXTRA
from scadbuddy.render.jobs import LAYOUT_NAME, extruder_order
from scadbuddy.render.schema import CustomizerSchema, Parameter, ParamValue
from scadbuddy.render.split import ColourPart
from scadbuddy.store import BlobRefs
from scadbuddy.store.geometry import GeometryIndex
from scadbuddy.workflows.activities import PIECE_NAME, RenderActivities
from scadbuddy.workflows.models import PieceRequest, piece_key
from tests.conftest import PgPool
from tests.support.activities import REVISION, demo_paths, worker_deps

SCHEMA = CustomizerSchema(
    parameters=[
        Parameter(name="width", type="number", initial=10),
        Parameter(name="body", type="color", initial="#FF0000"),
        Parameter(name="trim", type="color", initial="#00FF00"),
    ]
)


def _key(**params: ParamValue) -> str | None:
    return geometry_key("demo", REVISION, "model.scad", SCHEMA, params)


# ── the key ────────────────────────────────────────────────────────────────────


def test_pieces_that_differ_only_in_colour_share_a_geometry_key() -> None:
    assert _key(width=12, body="#0000FF") == _key(width=12, body="#123456", trim="#ABCDEF")
    # A CSS name is the colour OpenSCAD renders it as.
    assert _key(width=12, body="blue") == _key(width=12, body="#0000FF")


def test_a_geometry_change_is_another_geometry_key() -> None:
    assert _key(width=12) != _key(width=13)
    assert _key(width=12) != _key(width=12, extra=1)


def test_colour_parameters_that_come_to_share_a_value_are_another_geometry_key() -> None:
    """They become one part, one solid and one extruder: different geometry."""
    assert _key(body="#00FF00") != _key(body="#0000FF")
    assert colour_classes(SCHEMA, {"body": "#00FF00"}) == ["#00FF00"]
    assert colour_classes(SCHEMA, {"body": "#0000FF"}) == ["#0000FF", "#00FF00"]


def test_a_colour_parameter_whose_value_is_not_a_colour_keeps_it_in_the_key() -> None:
    assert _key(body="none") != _key(body="off")
    assert _key(body="none") != _key(body="#0000FF")


def test_a_template_without_colour_parameters_has_no_geometry_key() -> None:
    schema = CustomizerSchema(parameters=[Parameter(name="width", type="number", initial=1)])
    assert geometry_key("demo", REVISION, "model.scad", schema, {"width": 2}) is None


def test_the_key_is_per_template_revision_and_file() -> None:
    params: dict[str, ParamValue] = {"width": 1}
    base = geometry_key("demo", REVISION, "model.scad", SCHEMA, params)
    assert base != geometry_key("other", REVISION, "model.scad", SCHEMA, params)
    assert base != geometry_key("demo", "f00d000", "model.scad", SCHEMA, params)
    assert base != geometry_key("demo", REVISION, "parts/a.scad", SCHEMA, params)


def test_a_literal_colour_in_the_templates_source_is_found(tmp_path: Path) -> None:
    (tmp_path / "model.scad").write_text('body = "#FF0000"; // color\ncolor(body) cube();\n')
    assert not has_literal_colour(tmp_path)
    (tmp_path / "parts").mkdir()
    (tmp_path / "parts" / "a.scad").write_text('color( "red") cube();\n')
    assert has_literal_colour(tmp_path)
    (tmp_path / "parts" / "a.scad").write_text("color(c = [1, 0, 0]) cube();\n")
    assert has_literal_colour(tmp_path)


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


def test_class_order_is_extruder_order() -> None:
    box = trimesh.creation.box()
    parts = [
        ColourPart(0, "Default", "#F9D72C", box),
        ColourPart(1, "Color 1", "#00FF00", box),
        ColourPart(2, "Color 2", "#FF0000", box),
        ColourPart(3, "Color 3", "#ABCDEF", box),
    ]
    params: dict[str, ParamValue] = {"body": "#FF0000"}
    expected = extruder_order(parts, SCHEMA, params)
    assert class_order(parts, colour_classes(SCHEMA, params)) == expected
    assert [p.colour for p in expected] == ["#FF0000", "#00FF00", "#F9D72C", "#ABCDEF"]


# ── the activities ─────────────────────────────────────────────────────────────

#: A template with two colour parameters, as the fake openscad draws it.
SOURCE = 'width = 10;\nbody = "#FF0000"; // color\ntrim = "#00FF00"; // color\n'

#: An openscad that exports `SOURCE`'s schema, and as its model one box per colour
#: parameter in that colour (only the `_sb_targets` colour in a solid's wrapper
#: render), logging every 3MF export so a test can count the renders.
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
        {"name": "width", "type": "number", "initial": 10},
        {"name": "body", "type": "string", "initial": "#FF0000"},
        {"name": "trim", "type": "string", "initial": "#00FF00"}]}))
    raise SystemExit(0)
with open(here / "exports.log", "a") as log:
    log.write(" ".join(args) + "\n")
width = float(defines.get("width", "10"))
colours = []
for name, default in (("body", "#FF0000"), ("trim", "#00FF00")):
    c = json.loads(defines[name]) if name in defines else default
    if c.upper() not in colours:
        colours.append(c.upper())
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


def _request(**params: ParamValue) -> PieceRequest:
    return PieceRequest(
        slug="demo",
        revision=REVISION,
        params=dict(params),
        piece_key=piece_key("demo", REVISION, "model.scad", params),
    )


def _filaments(model_3mf: Path) -> list[str]:
    with zipfile.ZipFile(model_3mf) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    return [colour.upper()[:7] for colour in settings["filament_colour"]]


@pytest.fixture
def geometry_acts(tmp_path: Path, pg_pool: PgPool) -> tuple[RenderActivities, str, GeometryIndex]:
    paths = demo_paths(tmp_path, SOURCE)
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
async def test_a_piece_in_other_colours_is_recoloured_without_rendering(
    geometry_acts: tuple[RenderActivities, str, GeometryIndex],
) -> None:
    acts, binary, index = geometry_acts
    env = ActivityEnvironment()
    first = _request(width=12, body="#FF0000")
    prepared, main, _ = await _render(acts, first)
    assert prepared.geometry_key is not None
    assert prepared.colours == ["#FF0000", "#00FF00"]
    row = index.get(prepared.geometry_key)
    assert row is not None and row.piece_key == first.piece_key
    rendered = _exports(binary)
    assert rendered == 3  # the model, then one solid per colour

    second = _request(width=12, body="#0000FF")
    assert await env.run(acts.cached_piece, second) is None
    again = await env.run(acts.prepare, second)
    assert again.geometry_key == prepared.geometry_key
    reused = await env.run(acts.reuse_geometry, second, again)
    assert reused is not None and reused == main
    piece = await env.run(acts.finish_piece, second, again, reused)
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
    # And the recoloured piece is now the newest one with that geometry.
    row = index.get(prepared.geometry_key)
    assert row is not None and row.piece_key == second.piece_key


@pytest.mark.requires_postgres
async def test_a_piece_with_other_geometry_is_not_recoloured(
    geometry_acts: tuple[RenderActivities, str, GeometryIndex],
) -> None:
    acts, _, _ = geometry_acts
    env = ActivityEnvironment()
    await _render(acts, _request(width=12, body="#FF0000"))
    for req in (
        _request(width=13, body="#FF0000"),
        # The same colour as `trim`: one part where the first had two.
        _request(width=12, body="#00FF00"),
    ):
        prepared = await env.run(acts.prepare, req)
        assert await env.run(acts.reuse_geometry, req, prepared) is None


@pytest.mark.requires_postgres
async def test_a_swept_source_piece_is_a_miss_and_forgotten(
    geometry_acts: tuple[RenderActivities, str, GeometryIndex],
) -> None:
    acts, _, index = geometry_acts
    env = ActivityEnvironment()
    first = _request(width=12, body="#FF0000")
    prepared, _, _ = await _render(acts, first)
    acts.deps.blobs.remove(first.piece_key)

    second = _request(width=12, body="#0000FF")
    again = await env.run(acts.prepare, second)
    assert await env.run(acts.reuse_geometry, second, again) is None
    assert prepared.geometry_key is not None and index.get(prepared.geometry_key) is None


@pytest.mark.requires_postgres
async def test_a_template_with_a_literal_colour_never_shares_its_geometry(
    geometry_acts: tuple[RenderActivities, str, GeometryIndex],
) -> None:
    acts, _, _ = geometry_acts
    source = acts.deps.paths.model_source("demo")
    source.write_text(SOURCE + 'color("#FF0000") cube();\n', encoding="utf-8")
    prepared = await ActivityEnvironment().run(acts.prepare, _request(width=12))
    assert prepared.geometry_key is None
