from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import cast

import numpy as np
import pytest
import trimesh

from scadbuddy.core.config import Config, load_config
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.geometry import (
    MAX_REPORTED_EDGES,
    GeometryAnalysis,
    OverhangBucket,
    analyze_3mf,
    analyze_geometry,
    parts_from_3mf,
    split_colours,
)
from scadbuddy.render.jobs import UNCOLOURED_WARNING, RenderQueue
from scadbuddy.render.split import ColourPart

STORAGE_BOX = Path(__file__).resolve().parents[2] / "models" / "storage-box" / "model.scad"


def _part(mesh: trimesh.Trimesh, colour: str = "#FF0000", index: int = 1) -> ColourPart:
    return ColourPart(material_index=index, name=f"Color {index}", colour=colour, mesh=mesh)


def _cube(size: float = 10.0, at: tuple[float, float, float] = (0, 0, 0)) -> trimesh.Trimesh:
    """An axis-aligned cube with its lower corner at ``at``."""
    mesh: trimesh.Trimesh = trimesh.creation.box(extents=(size, size, size))
    mesh.apply_translation(np.array(at) + size / 2)
    return mesh


def _bucket(analysis: GeometryAnalysis, angle: int) -> OverhangBucket:
    return next(b for b in analysis.overhangs if b.min_angle_deg == angle)


def test_a_closed_cube_is_clean_and_measured() -> None:
    analysis = analyze_geometry([_part(_cube())])

    [part] = analysis.parts
    assert part.source == "solid"
    assert part.edges_checked
    assert (part.open_edges, part.non_manifold_edges) == (0, 0)
    assert part.volume_mm3 == pytest.approx(1000.0)
    assert analysis.edges == []

    assert analysis.bbox.size == pytest.approx((10, 10, 10))
    assert analysis.bed_z == pytest.approx(0.0)
    assert analysis.height_mm == pytest.approx(10.0)
    assert analysis.bed_contact_area_mm2 == pytest.approx(100.0)
    assert analysis.footprint is not None
    assert analysis.footprint.size[:2] == pytest.approx((10, 10))
    assert analysis.height_to_base_ratio == pytest.approx(1.0)
    # The bottom face is bed contact, not an overhang.
    assert all(bucket.area_mm2 == 0 and bucket.bbox is None for bucket in analysis.overhangs)

    assert analysis.thinnest_wall is not None
    assert analysis.thinnest_wall.thickness_mm == pytest.approx(10.0)
    assert analysis.smallest_feature is not None
    assert analysis.smallest_feature.islands == 1
    assert analysis.smallest_feature.min_extent_mm == pytest.approx(10.0)


def test_a_deleted_face_is_reported_as_open_edges_where_it_was() -> None:
    cube = _cube()
    # Drop both triangles of the top face (z == 10).
    top = np.flatnonzero((cube.vertices[cube.faces][:, :, 2] == 10).all(axis=1))
    assert len(top) == 2
    keep = np.setdiff1d(np.arange(len(cube.faces)), top)
    opened = trimesh.Trimesh(cube.vertices, cube.faces[keep], process=False)

    analysis = analyze_geometry([_part(opened, colour="#00FF00")])

    [part] = analysis.parts
    assert part.open_edges == 4
    assert part.non_manifold_edges == 0
    assert part.volume_mm3 is None
    assert len(analysis.edges) == 4
    for edge in analysis.edges:
        assert edge.kind == "open"
        assert (edge.part, edge.colour, edge.faces) == (1, "#00FF00", 1)
        # Every open edge is a rim of the missing top face.
        assert edge.start[2] == pytest.approx(10.0)
        assert edge.end[2] == pytest.approx(10.0)
        assert math.dist(edge.start, edge.end) == pytest.approx(10.0)


def test_cubes_sharing_an_edge_are_non_manifold_there() -> None:
    touching = cast(trimesh.Trimesh, trimesh.util.concatenate([_cube(), _cube(at=(10, 10, 0))]))

    analysis = analyze_geometry([_part(touching)])

    [part] = analysis.parts
    assert part.open_edges == 0
    assert part.non_manifold_edges == 1
    [edge] = analysis.edges
    assert edge.kind == "non_manifold"
    assert edge.faces == 4
    assert sorted([edge.start, edge.end]) == [
        pytest.approx((10, 10, 0)),
        pytest.approx((10, 10, 10)),
    ]


def test_an_overhanging_slab_fills_every_bucket() -> None:
    # A 10 mm pillar with a 30 x 10 x 2 slab on top, overhanging 10 mm each side.
    pillar = _cube()
    slab = trimesh.creation.box(extents=(30, 10, 2))
    slab.apply_translation((5, 5, 11))
    model = [_part(pillar, "#FF0000", 1), _part(slab, "#0000FF", 2)]

    analysis = analyze_geometry(model)

    # The slab's whole underside is a 90 degree ceiling (the middle rests on the
    # pillar, which this measurement does not know), so every bucket holds it.
    for angle in (45, 60, 75):
        bucket = _bucket(analysis, angle)
        assert bucket.area_mm2 == pytest.approx(300.0)
        assert bucket.bbox is not None
        assert bucket.bbox.min[2] == pytest.approx(10.0)
    assert analysis.bed_contact_area_mm2 == pytest.approx(100.0)
    assert analysis.height_mm == pytest.approx(12.0)
    assert analysis.height_to_base_ratio == pytest.approx(1.2)
    assert analysis.thinnest_wall is not None
    assert analysis.thinnest_wall.thickness_mm == pytest.approx(2.0)
    assert (analysis.thinnest_wall.part, analysis.thinnest_wall.colour) == (2, "#0000FF")
    assert analysis.smallest_feature is not None
    assert analysis.smallest_feature.min_extent_mm == pytest.approx(2.0)
    assert analysis.smallest_feature.islands == 2


def test_overhang_buckets_follow_the_angle_below_horizontal() -> None:
    # A block whose underside slopes up 25 degrees: its normal tips 65 degrees below
    # horizontal, so it is a 65 degree overhang -- in the 45 and 60 buckets only.
    rise = math.tan(math.radians(25)) * 10
    wedge = trimesh.creation.box(extents=(10, 10, 20))
    wedge.apply_translation((5, 5, 10))
    vertices = wedge.vertices.copy()
    vertices[(vertices[:, 0] == 10) & (vertices[:, 2] == 0), 2] = rise
    wedge = trimesh.Trimesh(vertices, wedge.faces, process=False)
    underside = 10 * math.hypot(10, rise)

    analysis = analyze_geometry([_part(wedge)])

    assert _bucket(analysis, 45).area_mm2 == pytest.approx(underside)
    assert _bucket(analysis, 60).area_mm2 == pytest.approx(underside)
    assert _bucket(analysis, 75).area_mm2 == pytest.approx(0.0)
    # It touches the bed only along one edge, so there is no contact area.
    assert analysis.bed_contact_area_mm2 == 0
    assert analysis.height_to_base_ratio is None


def test_split_parts_are_not_edge_checked() -> None:
    opened = trimesh.Trimesh(_cube().vertices, _cube().faces[:-2], process=False)

    analysis = analyze_geometry([_part(opened, "#123456")], split={"#123456"})

    [part] = analysis.parts
    assert part.source == "split"
    assert not part.edges_checked
    assert part.open_edges is None
    assert analysis.edges == []


def test_located_edges_are_capped_but_counted() -> None:
    # A triangle soup: every edge is open.
    count = MAX_REPORTED_EDGES  # three edges each, so well over the cap
    vertices = np.array([[i * 3.0, 0, 0] for i in range(count)] * 3, dtype=np.float64)
    vertices[count : 2 * count, 0] += 1
    vertices[2 * count :, 1] += 1
    faces = np.array([[i, i + count, i + 2 * count] for i in range(count)])
    soup = trimesh.Trimesh(vertices, faces, process=False)

    analysis = analyze_geometry([_part(soup)])

    assert analysis.parts[0].open_edges == 3 * count
    assert len(analysis.edges) == MAX_REPORTED_EDGES
    assert analysis.edges_truncated


def test_split_colours_reads_the_render_warnings() -> None:
    colours = ["#FF0000", "#00FF00"]
    assert split_colours([UNCOLOURED_WARNING], colours) == set(colours)
    assert split_colours(
        ["#00FF00: no closed solid (boom); used the split mesh", "unrelated"], colours
    ) == {"#00FF00"}
    assert split_colours(["plate thumbnail timed out"], colours) == set()


def test_the_3mf_round_trips_to_the_same_parts(tmp_path: Path) -> None:
    path = tmp_path / "model.3mf"
    write_bambu_3mf(
        [_part(_cube(), "#FF0000", 1), _part(_cube(at=(20, 0, 0)), "#0000FF", 2)],
        path,
        thumbnails=None,
    )

    parts = parts_from_3mf(path)
    assert [(p.name, p.colour) for p in parts] == [("Color 1", "#FF0000"), ("Color 2", "#0000FF")]
    analysis = analyze_3mf(
        path, warnings=["#0000FF: the solid render was empty; used the split mesh"]
    )
    assert [p.source for p in analysis.parts] == ["solid", "split"]
    # Model coordinates, not the plate placement the build item carries.
    assert analysis.bbox.min == pytest.approx((0, 0, 0))
    assert analysis.bbox.max == pytest.approx((30, 10, 10))


def test_nothing_to_analyse_is_an_error() -> None:
    with pytest.raises(ValueError, match="no geometry"):
        analyze_geometry([])


@pytest.mark.requires_openscad
@pytest.mark.skipif(not STORAGE_BOX.is_file(), reason="models/storage-box is not present")
async def test_a_bundled_model_analyses_as_closed_and_printable(tmp_path: Path) -> None:
    """The shipped storage box, lidless: one closed colour, 2 mm walls on a 1.6 mm
    floor, flat on the bed and nothing overhanging."""
    paths = DataPaths(tmp_path)
    paths.ensure()
    paths.model_dir("storage-box").mkdir(parents=True)
    shutil.copy(STORAGE_BOX, paths.model_source("storage-box"))

    queue = RenderQueue(Config(openscad=load_config().openscad, data_dir=paths.root), paths)
    await queue.start()
    try:
        job = await queue.submit("storage-box", {"lid_type": "none"})
        await queue.join()
    finally:
        await queue.aclose()
    done = queue.store.read(job.id)
    assert done.state == "done", done.error
    assert done.result is not None

    analysis = analyze_3mf(paths.root / done.result.model_3mf, warnings=done.result.warnings)

    assert analysis.parts
    for part in analysis.parts:
        assert part.source == "solid"
        assert (part.open_edges, part.non_manifold_edges) == (0, 0)
        assert part.volume_mm3 is not None and part.volume_mm3 > 0
    assert analysis.edges == []
    assert analysis.height_mm == pytest.approx(done.result.bbox_mm.size[2], abs=0.01)
    assert analysis.bed_contact_area_mm2 > 3000
    assert _bucket(analysis, 45).area_mm2 == pytest.approx(0.0, abs=1.0)
    assert analysis.thinnest_wall is not None
    assert 1.0 < analysis.thinnest_wall.thickness_mm <= 2.05
