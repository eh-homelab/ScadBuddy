"""Point and triangle matching in numpy (#2146, spec 2026-10-11 §7, §9)."""

from __future__ import annotations

import math
import time

import numpy as np
import pytest
import trimesh

from scadbuddy.render.meshmatch import (
    NearPairsExceeded,
    match_points,
    nearest_triangles,
    point_triangle_distance,
)


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
            [0.5, -1.0, 0.0],  # off edge ab
            [-1.0, 0.5, 0.0],  # off edge ac
        ]
    )
    measured = point_triangle_distance(points, np.repeat(triangle, len(points), axis=0))
    expected = [1.0, math.sqrt(2), 1.0, 0.0, 1.0, 0.0, math.sqrt(2) / 2, 1.0, 1.0]
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


def test_many_identical_points_are_matched_quickly() -> None:
    source = np.tile([1.0, 2.0, 3.0], (20_000, 1))
    began = time.perf_counter()
    found = match_points(source, source.copy(), 1e-4)
    assert time.perf_counter() - began < 5
    assert (found == 0).all()


def test_a_cell_of_too_many_distinct_points_is_refused() -> None:
    source = np.zeros((200, 3))
    source[:, 0] = np.arange(200) * 1e-7
    with pytest.raises(ValueError, match="distinct points"):
        match_points(source, source, 1e-4)


def test_nearest_triangles_on_a_large_sphere_agrees_with_brute_force() -> None:
    sphere = trimesh.creation.icosphere(subdivisions=5)
    assert len(sphere.faces) == 20480
    triangles = np.asarray(sphere.triangles, dtype=np.float64)
    rng = np.random.default_rng(2)
    on = triangles[rng.integers(0, len(triangles), 10_000)].mean(axis=1)
    direction = rng.normal(size=(10_000, 3))
    direction /= np.linalg.norm(direction, axis=1)[:, None]
    points = on + direction * rng.uniform(0, 0.009, (10_000, 1))
    far = np.array([[50.0, 0.0, 0.0], [0.0, 0.0, 3.0], [-9.0, 9.0, 9.0]])
    began = time.perf_counter()
    index, distance = nearest_triangles(np.vstack([points, far]), triangles, 0.01)
    assert time.perf_counter() - began < 5
    assert index[-3:].tolist() == [-1, -1, -1]
    assert np.isinf(distance[-3:]).all()
    index, distance = index[:-3], distance[:-3]
    for i in range(0, 10_000, 20):
        every = point_triangle_distance(
            np.repeat(points[i : i + 1], len(triangles), axis=0), triangles
        )
        assert math.isclose(distance[i], every.min(), abs_tol=1e-9)
        assert math.isclose(every[index[i]], every.min(), abs_tol=1e-9)


def test_a_triangle_covering_many_cells_is_still_found() -> None:
    triangles = np.array(
        [
            [[0.0, 0.0, 0.0], [100.0, 0.0, 0.0], [0.0, 100.0, 0.0]],
            [[0.0, 0.0, 50.0], [0.1, 0.0, 50.0], [0.0, 0.1, 50.0]],
        ]
    )
    index, _ = nearest_triangles(np.array([[10.0, 10.0, 0.005], [0.0, 0.0, 50.0]]), triangles, 0.01)
    assert index.tolist() == [0, 1]


def test_a_dense_cluster_exceeds_the_pair_budget_without_measuring() -> None:
    rng = np.random.default_rng(3)
    corner = rng.uniform(0, 1, (3000, 1, 3))
    triangles = corner + rng.uniform(0, 1, (3000, 3, 3))
    points = rng.uniform(0, 1, (10_000, 3))
    began = time.perf_counter()
    with pytest.raises(NearPairsExceeded) as raised:
        nearest_triangles(points, triangles, 0.01, max_pairs=100_000)
    assert time.perf_counter() - began < 2
    assert raised.value.cap == 100_000 and raised.value.pairs > raised.value.cap


def test_mostly_large_triangles_exceed_the_pair_budget() -> None:
    rng = np.random.default_rng(4)
    tiny = rng.uniform(0, 100, (600, 1, 3)) + rng.uniform(0, 0.1, (600, 3, 3))
    big = rng.uniform(0, 100, (400, 1, 3)) + rng.uniform(0, 100, (400, 3, 3))
    triangles = np.vstack([tiny, big])
    points = rng.uniform(0, 100, (1000, 3))
    with pytest.raises(NearPairsExceeded):
        nearest_triangles(points, triangles, 0.01, max_pairs=1000)
