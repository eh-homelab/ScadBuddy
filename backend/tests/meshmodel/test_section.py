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
