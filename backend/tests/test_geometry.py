from __future__ import annotations

import math
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import trimesh
from pydantic import TypeAdapter

from scadbuddy.core.paths import DataPaths
from scadbuddy.meshmodel import section
from scadbuddy.meshmodel.edits import Edit
from scadbuddy.render import geometry
from scadbuddy.render.bambu3mf import PlateParts, write_bambu_3mf, write_plates_3mf
from scadbuddy.render.geometry import (
    MAX_REPORTED_EDGES,
    WALL_MIN_SAMPLES,
    WALL_SAMPLES,
    GeometryAnalysis,
    NoSuchPlateError,
    OverhangBucket,
    Unreadable3MFError,
    analyze_3mf,
    analyze_geometry,
    parts_from_3mf,
    split_colours,
    wall_samples,
)
from scadbuddy.render.jobs import UNCOLOURED_WARNING
from scadbuddy.render.split import ColourPart
from tests.conftest import render_once

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
    # Built from three 10 mm segments rather than one box so the middle segment's
    # triangles sit entirely over the pillar and the outer two entirely off it --
    # a single 30 mm box's bottom is just two triangles spanning the whole length,
    # too coarse for the support test below to resolve per-region.
    pillar = _cube()
    segments = [trimesh.creation.box(extents=(10, 10, 2)) for _ in range(3)]
    for segment, x in zip(segments, (-5, 5, 15), strict=True):
        segment.apply_translation((x, 5, 11))
    slab = cast(trimesh.Trimesh, trimesh.util.concatenate(segments))
    model = [_part(pillar, "#FF0000", 1), _part(slab, "#0000FF", 2)]

    analysis = analyze_geometry(model)

    # The slab's whole underside is a 90 degree ceiling, but the 10x10 mm middle
    # rests flush on the pillar -- support, not overhang (#756) -- so only the
    # 10 mm overhang on each side (100 mm^2 apiece) fills every bucket.
    for angle in (45, 60, 75):
        bucket = _bucket(analysis, angle)
        assert bucket.area_mm2 == pytest.approx(200.0)
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


def test_a_part_resting_on_another_part_is_not_reported_as_overhang() -> None:
    # #756: a second part stacked flush on a base, z 0..4 then 4..6.8 -- the
    # keychain's letters sitting on its base plate -- is supported by the part
    # below, not overhanging air.
    base = trimesh.creation.box(extents=(10, 10, 4))
    base.apply_translation((5, 5, 2))
    top = trimesh.creation.box(extents=(10, 10, 2.8))
    top.apply_translation((5, 5, 4 + 2.8 / 2))
    model = [_part(base, "#FF0000", 1), _part(top, "#0000FF", 2)]

    analysis = analyze_geometry(model)

    assert all(bucket.area_mm2 == 0 and bucket.bbox is None for bucket in analysis.overhangs)


def test_a_slab_five_mm_above_a_pillar_still_overhangs() -> None:
    # A true overhang must keep reporting even with the support test in place: the
    # slab floats 5 mm clear of the pillar beneath it -- well past BED_TOLERANCE_MM
    # -- so nothing actually supports its underside.
    pillar = trimesh.creation.box(extents=(2, 2, 5))
    pillar.apply_translation((5, 5, 2.5))
    slab = trimesh.creation.box(extents=(10, 10, 2))
    slab.apply_translation((5, 5, 11))  # pillar top at z=5, slab bottom at z=10
    model = [_part(pillar, "#FF0000", 1), _part(slab, "#0000FF", 2)]

    analysis = analyze_geometry(model)

    assert _bucket(analysis, 60).area_mm2 == pytest.approx(100.0)


def test_a_smaller_part_fully_inside_its_base_is_not_overhang() -> None:
    # Mirrors the keychain shape from #756: a raised part whose footprint sits
    # entirely inside the part beneath it, so nothing pokes out over open air.
    base = trimesh.creation.box(extents=(60, 25, 4))
    base.apply_translation((30, 12.5, 2))
    raised = trimesh.creation.box(extents=(40, 10, 2.8))
    raised.apply_translation((30, 12.5, 4 + 2.8 / 2))
    model = [_part(base, "#0047BB", 1), _part(raised, "#FF1493", 2)]

    analysis = analyze_geometry(model)

    assert all(bucket.area_mm2 == 0 and bucket.bbox is None for bucket in analysis.overhangs)


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


def _two_plates_sharing_a_colour(path: Path) -> Path:
    """Plate 1: a red 10 mm cube and a blue one beside it. Plate 2: a red 30 mm cube.
    Both plates are drawn at the origin, so their meshes overlap in model space."""
    write_plates_3mf(
        [
            PlateParts(
                (_part(_cube(), "#FF0000", 1), _part(_cube(at=(20, 0, 0)), "#0000FF", 2)), (1, 2)
            ),
            PlateParts((_part(_cube(30), "#FF0000", 1),), (1,)),
        ],
        ["#FF0000", "#0000FF"],
        path,
        thumbnails=None,
    )
    return path


def test_each_plate_is_measured_on_its_own(tmp_path: Path) -> None:
    path = _two_plates_sharing_a_colour(tmp_path / "model.3mf")

    first = analyze_3mf(path)
    assert (first.plate, first.plates) == (1, 2)
    assert [(p.part, p.colour) for p in first.parts] == [(1, "#FF0000"), (2, "#0000FF")]
    assert first.bbox.max == pytest.approx((30, 10, 10))

    second = analyze_3mf(path, plate=2)
    assert (second.plate, second.plates) == (2, 2)
    # The red part is extruder 1 on plate 2 too, and only plate 2's cube is measured.
    assert [(p.part, p.colour) for p in second.parts] == [(1, "#FF0000")]
    assert second.bbox.max == pytest.approx((30, 30, 30))
    assert second.thinnest_wall is not None
    assert second.thinnest_wall.thickness_mm == pytest.approx(30)
    assert [p.material_index for p in parts_from_3mf(path, 2)] == [1]


def test_a_plate_the_3mf_does_not_have_is_its_own_error(tmp_path: Path) -> None:
    path = _two_plates_sharing_a_colour(tmp_path / "model.3mf")
    with pytest.raises(NoSuchPlateError, match="no plate 3; the 3MF has 2"):
        analyze_3mf(path, plate=3)

    single = tmp_path / "single.3mf"
    write_bambu_3mf([_part(_cube())], single, thumbnails=None)
    assert (analyze_3mf(single).plate, analyze_3mf(single).plates) == (1, 1)
    with pytest.raises(NoSuchPlateError):
        parts_from_3mf(single, 2)


def test_nothing_to_analyse_is_an_error() -> None:
    with pytest.raises(ValueError, match="no geometry"):
        analyze_geometry([])


def test_an_empty_part_is_still_reported() -> None:
    empty = trimesh.Trimesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), process=False)

    analysis = analyze_geometry([_part(_cube(), "#FF0000", 1), _part(empty, "#0000FF", 2)])

    assert [(p.part, p.colour, p.triangles) for p in analysis.parts] == [
        (1, "#FF0000", 12),
        (2, "#0000FF", 0),
    ]
    empty_part = analysis.parts[1]
    assert empty_part.bbox is None
    assert not empty_part.edges_checked
    assert empty_part.open_edges is None
    assert analysis.bbox.size == pytest.approx((10, 10, 10))


def _rewrite(source: Path, target: Path, entry: str, payload: bytes) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as out:
        for info in original.infolist():
            out.writestr(info, payload if info.filename == entry else original.read(info))


@pytest.mark.parametrize(
    ("entry", "payload"),
    [
        ("3D/Objects/object_1.model", b"<model><resources><object"),
        ("Metadata/project_settings.config", b"{not json"),
    ],
)
def test_a_damaged_3mf_is_unreadable_not_a_crash(
    tmp_path: Path, entry: str, payload: bytes
) -> None:
    good = tmp_path / "good.3mf"
    write_bambu_3mf([_part(_cube())], good, thumbnails=None)
    bad = tmp_path / "bad.3mf"
    _rewrite(good, bad, entry, payload)

    with pytest.raises(Unreadable3MFError):
        analyze_3mf(bad)


def test_a_file_that_is_not_a_zip_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "model.3mf"
    path.write_bytes(b"not a zip")
    with pytest.raises(Unreadable3MFError):
        analyze_3mf(path)


def test_the_wall_ray_count_shrinks_with_the_part_so_the_work_is_bounded() -> None:
    assert wall_samples(0) == 0
    assert wall_samples(12) == WALL_SAMPLES
    for triangles in (10_000, 200_000, 1_000_000, 5_000_000):
        rays = wall_samples(triangles)
        assert WALL_MIN_SAMPLES <= rays <= WALL_SAMPLES
        assert rays == WALL_MIN_SAMPLES or rays * triangles <= geometry.WALL_PAIR_BUDGET


def test_a_large_part_casts_fewer_rays(monkeypatch: pytest.MonkeyPatch) -> None:
    sphere = trimesh.creation.icosphere(subdivisions=3)  # 1280 faces
    monkeypatch.setattr(geometry, "WALL_PAIR_BUDGET", 100 * len(sphere.faces))

    analysis = analyze_geometry([_part(sphere)])

    assert analysis.thinnest_wall is not None
    assert analysis.thinnest_wall.samples == 100


@pytest.mark.requires_openscad
@pytest.mark.skipif(not STORAGE_BOX.is_file(), reason="models/storage-box is not present")
async def test_a_bundled_model_analyses_as_closed_and_printable(tmp_path: Path) -> None:
    """The shipped storage box, lidless: one closed colour, 2 mm walls on a 1.6 mm
    floor, flat on the bed and nothing overhanging."""
    paths = DataPaths(tmp_path)
    paths.ensure()
    paths.model_dir("storage-box").mkdir(parents=True)
    shutil.copy(STORAGE_BOX, paths.model_source("storage-box"))

    done, _ = await render_once(paths, "storage-box", {"lid_type": "none"})
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


EDITS = TypeAdapter(list[Edit])


def revolved(profile: list[tuple[float, float]]) -> trimesh.Trimesh:
    return trimesh.creation.revolve(np.array(profile, dtype=float), sections=64)


def carrot() -> trimesh.Trimesh:
    """Body r 10 to z 30, neck r 3 to 35, tuft r 7 to 45 (r 6 puts the neck's top layers
    exactly on the 25 % rule, which float noise flips)."""
    return revolved([(0, 0), (10, 0), (10, 30), (3, 30), (3, 35), (7, 35), (7, 45), (0, 45)])


def test_a_carrots_neck_is_a_weak_joint_with_a_split_that_fits() -> None:
    """Spec 2026-10-11 section 9 on the test carrot: the neck is under a quarter of the
    body below it, with more than 1 cm3 above."""
    [joint] = analyze_geometry([_part(carrot())]).weak_joints
    assert 30 < joint.z < 35
    assert joint.area_mm2 < 0.25 * joint.neighbour_area_mm2
    assert joint.volume_above_mm3 >= 1000
    assert joint.room_mm == pytest.approx(3 * math.cos(math.pi / 64), rel=1e-6)
    suggestion = joint.suggestion
    assert suggestion.object is None
    assert suggestion.point == pytest.approx((0, 0, joint.z), abs=1e-6)
    assert suggestion.normal == (0, 0, 1)
    assert suggestion.joint is not None
    assert suggestion.joint.diameter == round(1.2 * joint.room_mm, 2)
    assert suggestion.joint.peg_on == "above"  # the socket goes into the wide body
    # The suggestion is an edit_scene edit once it names its object.
    [edit] = EDITS.validate_python([{**suggestion.model_dump(), "object": "o1"}])
    assert edit.edit == "split_at_plane"


def test_two_colours_printed_as_one_piece_are_measured_together() -> None:
    """The demo's carrot: an orange body and a green neck and tuft sharing the z = 30
    face. Apart, neither has a weak section; together the neck is one."""
    body = revolved([(0, 0), (10, 0), (10, 30), (3, 30), (0, 30)])
    leaves = revolved([(0, 30), (3, 30), (3, 35), (7, 35), (7, 45), (0, 45)])
    analysis = analyze_geometry([_part(body, "#F08A24", 1), _part(leaves, "#3A7D2C", 2)])
    [joint] = analysis.weak_joints
    assert joint.parts == [1, 2]
    assert 30 < joint.z < 35


def test_two_carrots_are_two_weak_joints() -> None:
    other = carrot()
    other.apply_translation((40, 0, 0))
    both = trimesh.util.concatenate([carrot(), other])
    assert isinstance(both, trimesh.Trimesh)
    joints = analyze_geometry([_part(both)]).weak_joints
    assert sorted(round(j.centroid[0]) for j in joints) == [0, 40]


def test_a_straight_cylinder_has_no_weak_joint() -> None:
    assert (
        analyze_geometry([_part(trimesh.creation.cylinder(radius=10, height=40))]).weak_joints == []
    )


def test_a_small_tip_is_no_weak_joint() -> None:
    """A cone narrows to nothing, but under 1 cm3 is above any narrow layer."""
    cone = revolved([(0, 0), (10, 0), (0, 30)])
    assert analyze_geometry([_part(cone)]).weak_joints == []


def _bed_to_z(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    mesh.apply_translation((0, 0, -mesh.bounds[0][2]))
    return mesh


@pytest.mark.parametrize(
    "mesh",
    [
        trimesh.creation.icosphere(subdivisions=4, radius=20),
        trimesh.creation.capsule(height=30, radius=10),
        revolved([(0, 0), (20, 0), (0, 40)]),
    ],
    ids=["sphere", "capsule", "cone-tip-up"],
)
def test_a_curved_end_is_no_weak_joint(mesh: trimesh.Trimesh) -> None:
    """The layers within a window of an end have no full window on one side, so a
    tapering end is not a narrow layer (measured: a sphere reported z=0.1)."""
    assert analyze_geometry([_part(_bed_to_z(mesh))]).weak_joints == []


def test_a_cone_tip_down_is_no_weak_joint() -> None:
    cone = revolved([(0, 0), (0, 40), (20, 40)])
    assert analyze_geometry([_part(_bed_to_z(cone))]).weak_joints == []


def test_a_thin_wall_on_a_floor_is_no_weak_joint() -> None:
    """A cup: its walls are an eighth of the floor below them, but the section is a
    ring, whose centre is outside it (measured: a planter in file 688)."""
    cup = revolved([(0, 0), (30, 0), (30, 30), (28, 30), (28, 2), (0, 2)])
    assert analyze_geometry([_part(cup)]).weak_joints == []


def test_a_neck_too_thin_for_a_peg_suggests_no_joint() -> None:
    """Room 1.5 mm: a 1.8 mm peg needs 0.9 + 0.2 + 0.8 = 1.9 mm."""
    thin = revolved([(0, 0), (10, 0), (10, 30), (1.5, 30), (1.5, 35), (8, 35), (8, 45), (0, 45)])
    [joint] = analyze_geometry([_part(thin)]).weak_joints
    assert joint.suggestion.joint is None


def test_an_open_split_part_is_not_sectioned() -> None:
    assert analyze_geometry([_part(carrot())], split={"#FF0000"}).weak_joints == []


def test_thousands_of_islands_are_sectioned_quickly() -> None:
    boxes = []
    for i in range(3000):
        box = trimesh.creation.box((1, 1, 1))
        box.apply_translation(((i % 60) * 2.0, (i // 60) * 2.0, 0))
        boxes.append(box)
    many = trimesh.util.concatenate(boxes)
    assert isinstance(many, trimesh.Trimesh)
    part = _part(many)
    welded = [geometry._welded(part.mesh)]
    started = time.monotonic()
    assert geometry.weak_joints([part], welded, [1], ()) == []
    assert time.monotonic() - started < 3
    assert analyze_geometry([part]).weak_joints == []


def test_a_stack_of_necks_measures_no_more_than_the_attempt_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One piece (each carrot's tuft touches the next body), many weak runs."""
    calls = []
    real = section.measure

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr("scadbuddy.render.geometry.measure", counting)
    stack = revolved([(0, 0), *[p for k in range(30) for p in _stage(k * 45.0)], (0, 30 * 45.0)])
    joints = analyze_geometry([_part(stack)]).weak_joints
    assert 0 < len(joints) <= geometry.MAX_WEAK_JOINTS
    assert len(calls) <= geometry.MAX_WEAK_ATTEMPTS


def _stage(base: float) -> list[tuple[float, float]]:
    return [
        (10, base),
        (10, base + 30),
        (3, base + 30),
        (3, base + 35),
        (7, base + 35),
        (7, base + 45),
    ]


def test_parts_match_points_refuses_are_not_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("too many points in one cell")

    monkeypatch.setattr("scadbuddy.render.geometry.match_points", refuse)
    body = revolved([(0, 0), (10, 0), (10, 30), (3, 30), (0, 30)])
    leaves = revolved([(0, 30), (3, 30), (3, 35), (7, 35), (7, 45), (0, 45)])
    analysis = analyze_geometry([_part(body, "#F08A24", 1), _part(leaves, "#3A7D2C", 2)])
    assert [j.parts for j in analysis.weak_joints] == [[2]]  # the leaves alone, not the pair


def test_analysing_geometry_does_not_load_the_scene_edits() -> None:
    """`render.geometry` needs the joint spec and the section maths, not `edits` (the
    scene's whole edit vocabulary), so a plain render's analysis stays off that stack."""
    code = (
        "import sys, scadbuddy.render.geometry; print('scadbuddy.meshmodel.edits' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "False"
