"""Sections of a mesh by a plane (spec 2026-10-11 §5, peg sizing), numpy only."""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
import pytest
import trimesh

from scadbuddy.meshmodel.section import CutSpec, cut_points, measure


def neck() -> trimesh.Trimesh:
    """A carrot-ish solid, one closed mesh (OpenSCAD imports it as one volume): body
    r=10 z 0..30, neck r=3 z 30..35, tuft r=6 z 35..45, 64-sided."""
    profile = np.array(
        [[0, 0], [10, 0], [10, 30], [3, 30], [3, 35], [6, 35], [6, 45], [0, 45]], dtype=float
    )
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


def test_a_hole_is_taken_out_of_the_section() -> None:
    cylinder = trimesh.creation.cylinder(radius=5, height=10, sections=64)
    through = trimesh.creation.cylinder(radius=2, height=20, sections=64)
    holed = measure(cylinder, (0, 0, 0), (0, 0, 1), holes=[through])
    assert holed is not None
    polygon = 0.5 * 64 * math.sin(2 * math.pi / 64)
    assert holed.area == pytest.approx(polygon * (25 - 4), rel=1e-6)
    assert not holed.inside  # the centre is in the hole: a ring
    beside = trimesh.creation.cylinder(radius=1, height=20, sections=64)
    beside.apply_translation((3, 0, 0))
    offset = measure(cylinder, (0, 0, 0), (0, 0, 1), holes=[beside])
    assert offset is not None and offset.inside
    # The hole's edge is a wall: the room is no more than the centre's distance to it
    # (its 64-gon's edges are within cos(pi/64) of its radius, hence the tolerance).
    assert offset.room <= float(np.linalg.norm(offset.centroid[:2] - (3, 0))) - 1 + 0.01


def test_a_ring_has_its_centroid_outside() -> None:
    ring = trimesh.creation.annulus(r_min=4, r_max=5, height=10, sections=64)
    section = measure(ring, (0, 0, 0), (0, 0, 1))
    assert section is not None
    assert not section.inside


def test_cut_points_are_the_kept_vertices_and_the_cut_rim() -> None:
    points = cut_points(neck(), [((0, 0, 32), (0, 0, 1), "above")])
    assert points[:, 2].min() == pytest.approx(32)
    assert points[:, 2].max() == pytest.approx(45)


def test_two_cuts_add_the_points_where_their_planes_meet_the_surface() -> None:
    vertices = 5 * np.vstack([np.eye(3), -np.eye(3)])
    faces = [(0, 1, 2), (1, 3, 2), (3, 4, 2), (4, 0, 2), (1, 0, 5), (3, 1, 5), (4, 3, 5), (0, 4, 5)]
    octahedron = trimesh.Trimesh(vertices, faces)
    assert octahedron.is_winding_consistent
    points = cut_points(
        octahedron, [((0, 0, 1), (0, 0, 1), "above"), ((0, 1, 0), (0, 1, 0), "above")]
    )
    assert points[:, 0].min() == pytest.approx(-3)
    assert points[:, 0].max() == pytest.approx(3)


def test_three_cuts_add_the_corner_they_share() -> None:
    box = trimesh.creation.box((10, 10, 10))
    cuts: list[
        tuple[tuple[float, float, float], tuple[float, float, float], Literal["above", "below"]]
    ] = [
        ((0, 0, 0), (1, 0, 0), "above"),
        ((0, 0, 0), (0, 1, 0), "above"),
        ((0, 0, 0), (0, 0, 1), "above"),
    ]
    points = cut_points(box, cuts)
    assert np.any(np.all(np.isclose(points, 0), axis=1))


def test_a_cut_clips_the_section_to_what_it_keeps() -> None:
    box = trimesh.creation.box((10, 10, 10))
    cut: CutSpec = ((2.0, 0.0, 0.0), (1.0, 0.0, 0.0), "above")
    section = measure(box, (0, 0, 0), (0, 0, 1), cuts=[cut])
    assert section is not None
    assert section.area == pytest.approx(30.0)
    assert np.allclose(section.centroid, (3.5, 0, 0), atol=1e-9)
    # The cut line is a wall, 1.5 mm from the centroid: nearer than the box's 5 mm edges.
    assert section.room == pytest.approx(1.5)
    assert section.inside


def test_two_cuts_clip_one_after_another() -> None:
    box = trimesh.creation.box((10, 10, 10))
    cuts: list[CutSpec] = [
        ((1.0, 0.0, 0.0), (1.0, 0.0, 0.0), "above"),
        ((0.0, -1.0, 0.0), (0.0, 1.0, 0.0), "below"),
    ]
    section = measure(box, (0, 0, 0), (0, 0, 1), cuts=cuts)
    assert section is not None
    assert section.area == pytest.approx(16.0)
    assert np.allclose(section.centroid, (3, -3, 0), atol=1e-9)


def test_a_cut_that_removes_the_whole_section_leaves_nothing() -> None:
    box = trimesh.creation.box((10, 10, 10))
    cut: CutSpec = ((20.0, 0.0, 0.0), (1.0, 0.0, 0.0), "above")
    assert measure(box, (0, 0, 0), (0, 0, 1), cuts=[cut]) is None


def test_a_cut_through_a_ring_closes_each_stretch() -> None:
    ring = trimesh.creation.annulus(r_min=4, r_max=10, height=10, sections=256)

    def cap(r: float, a: float) -> tuple[float, float]:
        """Area and first moment about x of the disc of radius r beyond x = a."""
        return (
            r * r * math.acos(a / r) - a * math.sqrt(r * r - a * a),
            (2 / 3) * (r * r - a * a) ** 1.5,
        )

    (area_out, moment_out), (area_in, moment_in) = cap(10, 2), cap(4, 2)
    cut: CutSpec = ((2.0, 0.0, 0.0), (1.0, 0.0, 0.0), "above")
    section = measure(ring, (0, 0, 0), (0, 0, 1), cuts=[cut])
    assert section is not None
    assert section.area == pytest.approx(area_out - area_in, rel=1e-3)
    assert section.centroid[0] == pytest.approx(
        (moment_out - moment_in) / (area_out - area_in), rel=1e-3
    )
    assert section.centroid[1] == pytest.approx(0, abs=1e-9)


def two_boxes_sharing_a_wall() -> trimesh.Trimesh:
    left = trimesh.creation.box((6, 10, 10))
    left.apply_translation((-2, 0, 0))  # x from -5 to 1
    right = trimesh.creation.box((4, 10, 10))
    right.apply_translation((3, 0, 0))  # x from 1 to 5
    merged = trimesh.util.concatenate([left, right])
    assert isinstance(merged, trimesh.Trimesh)
    return merged


@pytest.mark.parametrize(
    ("keep", "area", "centroid_y"), [("above", 40.0, 3.0), ("below", 60.0, -2.0)]
)
def test_parts_that_touch_are_clipped_with_every_crossing_closed(
    keep: Literal["above", "below"], area: float, centroid_y: float
) -> None:
    cut: CutSpec = ((0.0, 1.0, 0.0), (0.0, 1.0, 0.0), keep)
    section = measure(two_boxes_sharing_a_wall(), (0, 0, 0), (0, 0, 1), cuts=[cut])
    assert section is not None
    assert section.area == pytest.approx(area)
    assert section.centroid[0] == pytest.approx(0, abs=1e-9)
    assert section.centroid[1] == pytest.approx(centroid_y)
