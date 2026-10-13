"""Point and triangle matching in numpy (#2146, spec 2026-10-11 §7, §9)."""

from __future__ import annotations

import math

import numpy as np
import trimesh

from scadbuddy.render.meshmatch import match_points, nearest_triangles, point_triangle_distance


def test_every_point_finds_its_source_within_the_tolerance() -> None:
    rng = np.random.default_rng(1)
    source = rng.uniform(-100, 100, (5000, 3))
    picked = rng.permutation(len(source))[:2000]
    # As OpenSCAD's export moves them: up to a few 1e-5 mm, across cell edges.
    query = source[picked] + rng.uniform(-3e-5, 3e-5, (2000, 3))
    found = match_points(source, query, 1e-4)
    assert (found == picked).all()


def test_a_point_further_than_the_tolerance_finds_nothing() -> None:
    source = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
    query = np.array([[0.0, 0.0, 2e-4], [10.0, 5e-5, 0.0]])
    assert match_points(source, query, 1e-4).tolist() == [-1, 1]


def test_repeated_source_points_still_match() -> None:
    source = np.array([[1.0, 2.0, 3.0], [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    found = match_points(source, np.array([[4.0, 5.0, 6.0], [1.0, 2.0, 3.0]]), 1e-4)
    assert found[0] == 2 and found[1] in (0, 1)


def test_empty_inputs_match_nothing() -> None:
    assert match_points(np.zeros((0, 3)), np.zeros((2, 3)), 1e-4).tolist() == [-1, -1]
    assert match_points(np.zeros((2, 3)), np.zeros((0, 3)), 1e-4).tolist() == []


def test_point_triangle_distance_by_region() -> None:
    triangle = np.array([[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]])
    points = np.array(
        [
            [0.2, 0.2, 1.0],  # above the face
            [-1.0, -1.0, 0.0],  # past corner a
            [2.0, 0.0, 0.0],  # past corner b
            [0.5, 0.5, 0.0],  # on edge bc
            [0.0, -1.0, 0.0],  # off a's corner along -y
            [0.3, 0.3, 0.0],  # in the face
            [1.0, 1.0, 0.0],  # off edge bc
        ]
    )
    measured = point_triangle_distance(points, np.repeat(triangle, len(points), axis=0))
    expected = [1.0, math.sqrt(2), 1.0, 0.0, 1.0, 0.0, math.sqrt(2) / 2]
    assert np.allclose(measured, expected)


def test_a_degenerate_triangle_is_measured_to_its_corners() -> None:
    line = np.array([[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]])
    assert np.allclose(point_triangle_distance(np.array([[1.0, 1.0, 0.0]]), line), [1.0])


def test_nearest_triangles_within_the_tolerance() -> None:
    box = trimesh.creation.box((10, 10, 10))
    points = np.array([[0.0, 0.0, 5.005], [0.0, 0.0, 0.0], [5.2, 0.0, 0.0]])
    index, distance = nearest_triangles(points, box.triangles, 0.01)
    assert index[0] >= 0 and box.face_normals[index[0]][2] == 1.0
    assert math.isclose(distance[0], 0.005, abs_tol=1e-9)
    assert index[1:].tolist() == [-1, -1]
    assert np.isinf(distance[1:]).all()
