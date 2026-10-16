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


@pytest.mark.parametrize(
    "angles", [(10, 20, 30), (-75, 44, 170), (0, 90, 0), (0, -90, 45), (180, 0, 0)]
)
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
    assert np.isclose(
        np.dot(world - (10.0, 0.0, 5.0), np.asarray((1.0, 1.0, 0.0)) / np.sqrt(2)), 0.0
    )
    assert np.isclose(np.linalg.norm(normal), 1.0)


def test_normal_to_world_is_the_inverse_of_the_plane_mapping() -> None:
    rotate, scale = (0.0, 30.0, 0.0), (1.0, 3.0, 0.5)
    _, local = to_object_plane((0, 0, 0), rotate, scale, (0, 0, 0), (0.0, 0.6, 0.8))
    assert np.allclose(normal_to_world(rotate, scale, local), (0.0, 0.6, 0.8))


def test_a_zero_normal_is_refused() -> None:
    with pytest.raises(ValueError, match="zero"):
        to_object_plane((0, 0, 0), (0, 0, 0), (1, 1, 1), (0, 0, 0), (0, 0, 0))
